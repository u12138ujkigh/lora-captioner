# -*- coding: utf-8 -*-
"""Provider 层：Ollama + DSH 网关（读 ~/.dsh 四层配置链）。

DSH 渠道发现逻辑对齐 local_account_providers.py（ComfyUI 增强节点同款）：
  1. ~/.dsh/settings.yaml
  2. ~/.dsh/profiles/desktop/cordis.patch.yml
  3. ~/.dsh/profiles/desktop/cordis.yml
  4. ~/.dsh/settings.yaml.imported
密钥：~/.dsh/.credentials.yaml 的 refs.<apiKeyEnv>，取不到时回退环境变量。
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

DSH_HOME_ENV = "DSH_HOME"
DSH_PROVIDER_CONFIG_KEY = "llm-pi-ai"
DSH_BUILTIN_MODELS_KEY = "llm-deepseek"
DSH_OPENAI_COMPATIBLE_API = "openai-completions"
DSH_BUILTIN_PROVIDERS = {
    "deepseek": ("DeepSeek 官方", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
}
OLLAMA_DEFAULT_BASE = "http://127.0.0.1:11434"

# 渠道缓存（all_channels / list_ollama_channel 用，避免每张图重读配置）
_DSH_CACHE: dict[str, ProviderChannel] = {}
_DSH_CACHE_AT: float = 0.0
_OLLAMA_CACHE: ProviderChannel | None = None
_OLLAMA_CACHE_AT: float = 0.0


class ProviderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# DSH 配置链
# --------------------------------------------------------------------------- #
def _dsh_home() -> Path:
    import os
    explicit = os.environ.get(DSH_HOME_ENV)
    return Path(explicit).expanduser() if explicit else Path.home() / ".dsh"


def _load_yaml(path: Path) -> Any:
    try:
        loader = getattr(yaml, "SafeLoader", None)
        if loader is not None and not getattr(loader, "__dsh_js_registered__", False):
            loader.add_constructor(
                "tag:yaml.org,2002:js",
                lambda l, node: l.construct_scalar(node),
            )
            loader.__dsh_js_registered__ = True
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ProviderError(f"{path} 不存在") from e
    except (OSError, ValueError) as e:
        raise ProviderError(f"{path} 无法解析: {type(e).__name__}") from e


def _dsh_config_candidates() -> list[Path]:
    home = _dsh_home()
    return [
        home / "settings.yaml",
        home / "profiles" / "desktop" / "cordis.patch.yml",
        home / "profiles" / "desktop" / "cordis.yml",
        home / "settings.yaml.imported",
    ]


def _extract_pi_ai_document(document: Any) -> Any:
    if not isinstance(document, dict):
        return document
    if DSH_PROVIDER_CONFIG_KEY in document:
        return document
    entries = document.get("services") or document.get("plugins") or document.get("list")
    if entries is None and isinstance(document, list):
        entries = document
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or "") != DSH_PROVIDER_CONFIG_KEY:
                continue
            config = entry.get("config")
            if isinstance(config, dict) and config:
                return {DSH_PROVIDER_CONFIG_KEY: config}
    return document


def _load_dsh_settings() -> dict:
    errors: list[str] = []
    for path in _dsh_config_candidates():
        if not path.is_file():
            continue
        try:
            raw = _load_yaml(path)
        except ProviderError as e:
            errors.append(str(e))
            continue
        document = _extract_pi_ai_document(raw)
        if not isinstance(document, dict):
            continue
        providers = ((document.get(DSH_PROVIDER_CONFIG_KEY) or {}).get("providers") or {})
        if isinstance(providers, dict) and providers:
            return document
        errors.append(f"{path} 无 {DSH_PROVIDER_CONFIG_KEY}.providers")
    raise ProviderError("找不到 DSH provider 配置；已尝试: " + "；".join(errors or ["（候选文件均不存在）"]))


@dataclass
class ModelInfo:
    id: str
    name: str = ""
    vision: bool = True  # DSH 渠道模型默认视为可看图；Ollama 由探测决定


@dataclass
class ProviderChannel:
    provider_id: str          # "dsh:<id>" 或 "ollama"
    label: str                # 显示名
    kind: str                 # "dsh" | "ollama"
    base_url: str
    api_key: str = ""         # 仅 dsh
    models: list[ModelInfo] = field(default_factory=list)


def _channel_models(provider_cfg: dict) -> list[str]:
    models = provider_cfg.get("models")
    out: list[str] = []
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict):
                mid = str(m.get("id") or "").strip()
                if mid and mid not in out:
                    out.append(mid)
    return out


def list_dsh_channels() -> list[ProviderChannel]:
    document = _load_dsh_settings()
    channels: list[ProviderChannel] = []
    seen: set[str] = set()
    providers = ((document.get(DSH_PROVIDER_CONFIG_KEY) or {}).get("providers") or {})
    for raw_id, raw_cfg in providers.items():
        if not isinstance(raw_cfg, dict):
            continue
        channel_id = str(raw_id or "").strip()
        base_url = str(raw_cfg.get("baseURL") or "").strip()
        api = str(raw_cfg.get("api") or "").strip()
        if not channel_id or channel_id in seen or not base_url.startswith("http"):
            continue
        if api and api != DSH_OPENAI_COMPATIBLE_API:
            continue
        key = _read_dsh_key(str(raw_cfg.get("apiKeyEnv") or "").strip())
        label = str(raw_cfg.get("displayName") or "").strip() or channel_id
        models = [ModelInfo(id=mid) for mid in _channel_models(raw_cfg)]
        channels.append(ProviderChannel(
            provider_id=f"dsh:{channel_id}", label=label, kind="dsh",
            base_url=base_url.rstrip("/"), api_key=key, models=models,
        ))
        seen.add(channel_id)
    builtin_models = [
        str((m or {}).get("id") or "").strip() if isinstance(m, dict) else str(m)
        for m in ((document.get(DSH_BUILTIN_MODELS_KEY) or {}).get("models") or [])
    ]
    builtin_models = [m for m in builtin_models if m]
    for channel_id, (display, base_url, env) in DSH_BUILTIN_PROVIDERS.items():
        if channel_id in seen or not builtin_models:
            continue
        key = _read_dsh_key(env)
        channels.append(ProviderChannel(
            provider_id=f"dsh:{channel_id}", label=display, kind="dsh",
            base_url=base_url, api_key=key,
            models=[ModelInfo(id=m) for m in builtin_models],
        ))
        seen.add(channel_id)
    return channels


def _read_dsh_key(api_key_env: str) -> str:
    if not api_key_env:
        return ""
    try:
        credentials = _load_yaml(_dsh_home() / ".credentials.yaml") or {}
        refs = credentials.get("refs") or {}
        value = str(refs.get(api_key_env) or "").strip()
        if value:
            return value
    except ProviderError:
        pass
    import os
    return os.environ.get(api_key_env, "").strip()


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #
def list_ollama_channel(refresh: bool = False) -> ProviderChannel | None:
    """探测本地 Ollama（结果缓存 5 分钟）。返回 None 表示不可达。"""
    global _OLLAMA_CACHE, _OLLAMA_CACHE_AT
    import time as _t
    if not refresh and _OLLAMA_CACHE is not None and (_t.time() - _OLLAMA_CACHE_AT) < 300:
        return _OLLAMA_CACHE
    try:
        with httpx.Client(timeout=6) as client:
            r = client.get(f"{OLLAMA_DEFAULT_BASE}/api/tags")
            r.raise_for_status()
            data = r.json()
    except Exception:
        _OLLAMA_CACHE = None
        return None
    models: list[ModelInfo] = []
    for m in data.get("models") or []:
        mid = str(m.get("name") or "").strip()
        if not mid:
            continue
        # 用 show 接口探测视觉能力（cap: vision）
        vision = False
        try:
            with httpx.Client(timeout=10) as client:
                rs = client.post(f"{OLLAMA_DEFAULT_BASE}/api/show",
                                 json={"name": mid})
                if rs.status_code == 200:
                    caps = rs.json().get("capabilities") or []
                    vision = "vision" in caps
        except Exception:
            pass
        models.append(ModelInfo(id=mid, name=mid, vision=vision))
    if not models:
        models = [ModelInfo(id="", name="（无已安装模型）", vision=False)]
    ch = ProviderChannel(
        provider_id="ollama", label="Ollama（本地）", kind="ollama",
        base_url=OLLAMA_DEFAULT_BASE, models=models,
    )
    _OLLAMA_CACHE = ch
    _OLLAMA_CACHE_AT = _t.time()
    return ch


_OLLAMA_CACHE: ProviderChannel | None = None
_OLLAMA_CACHE_AT: float = 0.0

_DSH_CACHE: dict[str, ProviderChannel] = {}
_DSH_CACHE_AT: float = 0.0


def all_channels(refresh: bool = False) -> dict[str, ProviderChannel]:
    """{provider_id: channel}（结果缓存 60s，打标循环内不重复读配置）。Ollama 不可达时不含 ollama 键。"""
    global _DSH_CACHE, _DSH_CACHE_AT
    import time as _t
    if not refresh and (_t.time() - _DSH_CACHE_AT) < 60 and _DSH_CACHE:
        out = dict(_DSH_CACHE)
        ol = list_ollama_channel()
        if ol is not None:
            out["ollama"] = ol
        return out
    out: dict[str, ProviderChannel] = {}
    try:
        for ch in list_dsh_channels():
            out[ch.provider_id] = ch
        _DSH_CACHE = dict(out)
        _DSH_CACHE_AT = _t.time()
    except ProviderError:
        pass
    ol = list_ollama_channel(refresh=refresh)
    if ol is not None:
        out["ollama"] = ol
    return out


# --------------------------------------------------------------------------- #
# 打标调用
# --------------------------------------------------------------------------- #
def _clean_reply(text: str) -> str:
    """剥掉 <think>…</think>、代码围栏、引号包裹、多余空白。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"<\|.*?\|>", "", text, flags=re.S)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    text = text.strip().strip('"').strip("'").strip()
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def _extract_reply_text(data: dict, kind: str) -> str:
    """从 chat 响应里取正文文本。content 为 None/空时走 reasoning_content 尾段兜底（思考型模型）。
    绝不把字面 None 写进结果：返回空串表示无效。"""
    if kind == "ollama":
        msg = (data.get("message") or {}).get("content") or ""
        return str(msg)
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    msg = message.get("content")
    if isinstance(msg, list):
        # 分段 content（多模态数组形态）
        parts = [seg.get("text", "") for seg in msg
                 if isinstance(seg, dict) and seg.get("type") == "text"]
        return " ".join(p for p in parts if p)
    if msg is None or not str(msg).strip():
        # content 为 null：思考型模型可能把结论留在 reasoning_content 尾段
        rc = message.get("reasoning_content") or ""
        if rc and str(rc).strip():
            return str(rc).strip().splitlines()[-1]
        return ""
    return str(msg)


_JUNK_REPLIES = {"none", "null", "n/a", "nan", "''", '""', "(none)"}


def caption_image(
    provider_id: str,
    model: str,
    image_path: Path,
    system_prompt: str,
    timeout: float = 180.0,
) -> str:
    """单图打标：返回清洗后的 caption 文本。失败抛 ProviderError。"""
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    mime = "image/jpeg"
    suffix = Path(image_path).suffix.lower()
    if suffix == ".png":
        mime = "image/png"
    elif suffix == ".webp":
        mime = "image/webp"

    channels = all_channels()
    ch = channels.get(provider_id)
    if ch is None:
        raise ProviderError(f"Provider {provider_id} 不可用（可能已下线，刷新 Provider 列表）")
    # 非视觉模型前置拦截（Ollama capabilities 探测结果），给出明确报错
    if ch.kind == "ollama" and model:
        m = next((x for x in ch.models if x.id == model), None)
        if m is not None and not m.vision:
            raise ProviderError(
                f"模型 {model} 不支持看图（Ollama capabilities 无 vision）。"
                "请换视觉模型，如 ollama pull qwen3-vl:8b")

    started = time.time()
    if ch.kind == "ollama":
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Caption this image.", "images": [b64]},
            ],
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": 400},
        }
        url = f"{ch.base_url}/api/chat"
        headers = {"Content-Type": "application/json"}
    else:
        if not ch.api_key:
            raise ProviderError(f"渠道 {ch.label} 没有可用密钥（{ch.provider_id}）")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": "Caption this image."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]},
            ],
            "max_tokens": 600,
            "temperature": 0.4,
        }
        url = f"{ch.base_url}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {ch.api_key}"}

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise ProviderError(f"网络错误: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    text = _clean_reply(_extract_reply_text(data, ch.kind))
    if not text or text.lower().strip(" .!\"'") in _JUNK_REPLIES:
        raise ProviderError(
            "模型返回空/None 内容（该模型可能不支持看图或走了纯思考通道，"
            "换用带「看图」标注的模型，或点🔄刷新后重选）")
    return text


# --------------------------------------------------------------------------- #
# Bing 免费翻译（无需密钥）
# --------------------------------------------------------------------------- #
import threading as _threading
import re as _re
import json as _json
import urllib.parse as _urlparse
import urllib.request as _urlreq
import http.cookiejar as _cookiejar

_BING_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0")
_bing_lock = _threading.Lock()
_bing_state = {"opener": None, "ig": None, "iid": None, "key": None,
               "token": None, "ts": 0.0}


def _system_proxy() -> str:
    """读 Windows 系统代理（urllib 不像 curl 那样自动读）。"""
    import os
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enable:
            return ""
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        key.Close()
        server = server.strip()
        if "=" in server:                      # 形如 http=host:port;https=host:port
            for part in server.split(";"):
                if part.lower().startswith("https="):
                    return "http://" + part.split("=", 1)[1]
            for part in server.split(";"):
                if part.lower().startswith("http="):
                    return "http://" + part.split("=", 1)[1]
            return ""
        return "http://" + server
    except Exception:
        return ""


def _bing_session(force: bool = False):
    """建立/复用 Bing 翻译会话（IG/IID/abuse 参数，约 5 分钟有效）。"""
    import time
    with _bing_lock:
        st = _bing_state
        if not force and st["opener"] and (time.time() - st["ts"] < 240):
            return st

        proxy = _system_proxy()
        handlers = []
        if proxy:
            handlers.append(_urlreq.ProxyHandler({"http": proxy, "https": proxy}))
        cj = _cookiejar.CookieJar()
        handlers.append(_urlreq.HTTPCookieProcessor(cj))
        opener = _urlreq.build_opener(*handlers)

        hdr = {"User-Agent": _BING_UA, "Accept": "*/*",
               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        html = opener.open(
            _urlreq.Request("https://cn.bing.com/translator", headers=hdr),
            timeout=25).read().decode("utf-8", "ignore")

        ig = _re.search(r'IG:"([^"]+)"', html)
        iid = _re.search(r'data-iid="([^"]+)"', html)
        ab = _re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),\s*"([^"]+)"', html)
        if not (ig and ab):
            raise ProviderError("Bing 会话建立失败（拿不到 IG/abuse 参数）")

        st.update({"opener": opener, "ig": ig.group(1),
                   "iid": iid.group(1) if iid else "translator.5023",
                   "key": ab.group(1), "token": ab.group(2), "ts": time.time()})
        return st


def bing_translate(text: str, to_lang: str = "zh-Hans", from_lang: str = "en") -> str:
    """用 Bing 免费接口翻译。失败抛 ProviderError。"""
    if not text.strip():
        return ""
    st = _bing_session()
    for attempt in (1, 2):
        try:
            url = ("https://cn.bing.com/ttranslatev3?isVertical=1"
                   f"&&IG={st['ig']}&IID={st['iid']}")
            body = _urlparse.urlencode({
                "fromLang": from_lang, "text": text, "to": to_lang,
                "token": st["token"], "key": st["key"],
            }).encode()
            req = _urlreq.Request(url, data=body, headers={
                "User-Agent": _BING_UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://cn.bing.com/translator",
                "Origin": "https://cn.bing.com",
                "X-Requested-With": "XMLHttpRequest",
            })
            raw = st["opener"].open(req, timeout=25).read().decode("utf-8", "ignore")
            data = _json.loads(raw)
            if isinstance(data, list) and data:
                return data[0]["translations"][0]["text"]
            raise ProviderError(f"Bing 返回异常：{raw[:120]}")
        except Exception as e:
            if attempt == 2:
                raise ProviderError(f"Bing 翻译失败：{e}")
            st = _bing_session(force=True)      # 会话过期，重建再试一次
    return ""


def translate_text(provider_id: str, model: str, text: str, timeout: float = 90.0) -> str:
    """把英文 caption 译成中文（展示用）。失败抛 ProviderError。"""
    channels = all_channels()
    ch = channels.get(provider_id)
    if ch is None:
        raise ProviderError(f"Provider {provider_id} 不可用")
    if ch.kind == "ollama":
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是翻译引擎。把用户给的英文图像描述翻成流畅自然的简体中文，只输出译文。"},
                {"role": "user", "content": text},
            ],
            "stream": False, "options": {"temperature": 0.2, "num_predict": 500},
        }
        url = f"{ch.base_url}/api/chat"
        headers = {"Content-Type": "application/json"}
    else:
        if not ch.api_key:
            raise ProviderError(f"渠道 {ch.label} 没有可用密钥")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是翻译引擎。把用户给的英文图像描述翻成流畅自然的简体中文，只输出译文。"},
                {"role": "user", "content": text},
            ],
            "max_tokens": 600, "temperature": 0.2,
        }
        url = f"{ch.base_url}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {ch.api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise ProviderError(f"网络错误: {type(e).__name__}") from e
    if r.status_code != 200:
        raise ProviderError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    out = _clean_reply(_extract_reply_text(data, ch.kind))
    if not out or out.lower().strip(" .!\"'") in _JUNK_REPLIES:
        raise ProviderError("翻译返回为空/None（该模型可能走了思考通道，换个非思考模型试试）")
    return out
