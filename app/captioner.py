# -*- coding: utf-8 -*-
"""扫描图片目录、组装系统提示词、caption 后处理与落盘。"""
from __future__ import annotations

import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "prompt_templates"

# 风格词黑名单（风格 LoRA 输出二次清洗，沿用 arc_raiders 原则）
STYLE_BLOCKLIST = [
    # 流派/渲染
    "retro-futuristic", "retrofuturistic", "retrofuturism", "post-apocalyptic",
    "cyberpunk", "sci-fi", "scifi", "science fiction", "steampunk", "dieselpunk",
    "anime", "cartoon", "photorealistic", "hyperrealistic", "concept art",
    "digital painting", "matte painting", "illustration", "3d render", "render",
    "game art", "artstation", "trending on artstation", "digital art", "painting",
    # 氛围/品质
    "atmospheric", "cinematic", "moody", "dramatic", "epic", "masterpiece",
    "best quality", "8k", "4k", "uhd", "hdr", "sharp focus", "octane",
    "unreal engine", "award winning", "vintage", "nostalgic", "grunge",
    "film grain", "dreamy", "artistic", "aesthetic",
]
_BLOCK_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(set(STYLE_BLOCKLIST), key=len, reverse=True)) + r")\b",
    re.I,
)

# 体型词黑名单（身材 LoRA 输出二次清洗）
BODY_BLOCKLIST = [
    "hourglass figure", "hourglass", "curvy", "curves", "slim waist", "narrow waist",
    "tiny waist", "voluptuous", "full-figured", "full figured", "plus-size", "plus size",
    "petite frame", "statuesque", "willowy", "athletic build", "toned body", "toned abs",
    "flat stomach", "wide hips", "thick thighs", "slender arms", "long legs", "long neck",
    "shapely legs", "perfect figure", "great figure", "well-proportioned body",
]
_BODY_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(set(BODY_BLOCKLIST), key=len, reverse=True)) + r")\b",
    re.I,
)


def scan_images(directory: str, recursive: bool = False) -> list[dict]:
    """扫描目录内图片（按文件名排序）。返回 [{path, name, size, mtime, has_caption}]。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    it = root.rglob("*") if recursive else root.iterdir()
    items: list[dict] = []
    for p in it:
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        caption = p.with_suffix(".txt")
        items.append({
            "path": str(p),
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": int(p.stat().st_mtime),
            "has_caption": caption.is_file(),
        })
    items.sort(key=lambda x: x["name"].lower())
    return items


# --------------------------------------------------------------------------- #
# 系统提示词组装
# --------------------------------------------------------------------------- #
def _load_template_section(md_path: Path, heading: str) -> str:
    """从模板 markdown 里取 '## <heading>' 到下一个 '## ' 之间的正文。"""
    text = md_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^##\s*{re.escape(heading)}\s*$", re.M)
    m = pattern.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


def build_system_prompt(
    lora_type: str,
    person_name: str = "",
    generic_subject: str = "",
    trigger_word: str = "",
    style_hint: str = "",
    extra_instructions: str = "",
) -> str:
    """根据打标类型和参数组装系统提示词。"""
    person_name = (person_name or "").strip()
    generic_subject = (generic_subject or "").strip()
    trigger_word = (trigger_word or "").strip()
    style_hint = (style_hint or "").strip()
    extra_instructions = (extra_instructions or "").strip()

    if lora_type == "person":
        tpl_file = TEMPLATE_DIR / "person.md"
        if person_name:
            body = _load_template_section(tpl_file, "系统提示词（人物名指定时）")
            body = body.replace("{PERSON_NAME}", person_name)
        else:
            body = _load_template_section(tpl_file, "系统提示词（不指定人物名，用触发词模式）")
            subject = generic_subject or "a woman"
            body = body.replace("{GENERIC_SUBJECT}", subject)
    elif lora_type == "clothing":
        tpl_file = TEMPLATE_DIR / "clothing.md"
        body = _load_template_section(tpl_file, "系统提示词")
        body = body.replace("{TRIGGER_WORD}", trigger_word or "the outfit")
    elif lora_type == "style":
        tpl_file = TEMPLATE_DIR / "style.md"
        body = _load_template_section(tpl_file, "系统提示词")
    elif lora_type == "body":
        tpl_file = TEMPLATE_DIR / "body.md"
        body = _load_template_section(tpl_file, "系统提示词")
    else:
        raise ValueError(f"未知打标类型: {lora_type}")

    style_line = f"STYLE BIAS: {style_hint}" if style_hint else ""
    extra_line = f"ADDITIONAL INSTRUCTIONS: {extra_instructions}" if extra_instructions else ""
    body = body.replace("{STYLE_HINT}", style_line).replace("{EXTRA_INSTRUCTIONS}", extra_line)
    # 清掉模板残留占位符
    body = re.sub(r"\{\w+\}", "", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


# --------------------------------------------------------------------------- #
# 后处理
# --------------------------------------------------------------------------- #
def _normalize_segment(seg: str) -> str:
    seg = re.sub(r"\s{2,}", " ", seg).strip(" .;，；")
    return seg


def dedupe_clean(text: str) -> str:
    """逗号分段去重 + 空白规整。保留原顺序。"""
    parts = re.split(r"\s*[,，]\s*", text)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        p = _normalize_segment(p)
        if not p or len(p) < 2:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return ", ".join(out)


def strip_style_words(text: str) -> tuple[str, int]:
    """剔除风格词黑名单。返回 (清洗后文本, 剔除数)。"""
    hits: list[str] = []

    def _sub(m: re.Match) -> str:
        hits.append(m.group(0))
        return " "

    out = _BLOCK_RE.sub(_sub, text)
    return out, len(hits)


def strip_body_words(text: str) -> tuple[str, int]:
    """剔除体型词黑名单（身材 LoRA 专用）。返回 (清洗后文本, 剔除数)。"""
    hits: list[str] = []
    segs = re.split(r"(\s*,\s*)", text)   # 保留逗号分隔符
    for i in range(0, len(segs), 2):
        segs[i] = _BODY_RE.sub(lambda m: (hits.append(m.group(0)) or " "), segs[i])
    out = "".join(segs)
    # 段内清理：剥掉替换后残留的悬空短语（"with an hourglass figure"→删整段 "with ..."）
    # 循环直到稳定：先删 "with <词> and" 这类半截结构，再删孤立冠词/介词
    prev = None
    while prev != out:
        prev = out
        # "with X and Y in" → 整个 with 短语连同并列结构一起删
        out = re.sub(r"\b(?:with|having)\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,3}(?:and\s+)?(?=[,\s]*(?:in|on|at|by|beside|near|wearing|stands?|sits?|kneels?|bends?|poses?|holds?|leans?|raised)\b)", "", out, flags=re.I)
        out = re.sub(r"\bwith\s+(?:and\s+)?(?=\s*,)", "", out, flags=re.I)
        # 段首悬空连接词+冠词（"and a in" / "with an in"）
        out = re.sub(r"(?:^|(?<=, ))(?:and|with)\s+an?\s+(?=(?:in|on|at|by|beside|near|wearing|poses?|stands?|sits?)\b)", "", out, flags=re.I)
        out = re.sub(r"(?<= )an?\s+(?=(?:in|on|at|by|beside|near|wearing)\b)", "", out, flags=re.I)
        # 段尾孤立形容词（", visible" / ", toned"）——删掉体型词后剩下的尾巴
        out = re.sub(r",\s*(?:visible|apparent|evident|on display|showing|noticeable)\b", "", out, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,")
    return out, len(hits)


def assemble_caption(
    raw: str,
    lora_type: str,
    trigger_word: str,
    trigger_position: str = "prefix",
) -> tuple[str, dict]:
    """模型原始输出 → 最终 caption。返回 (caption, 后处理元信息)。"""
    meta: dict[str, Any] = {"style_words_removed": []}
    text = raw.strip()
    if lora_type == "style":
        text, n = strip_style_words(text)
        meta["style_words_removed"] = n
    if lora_type == "body":
        text, n = strip_body_words(text)
        meta["body_words_removed"] = n
    text = dedupe_clean(text)
    tw = (trigger_word or "").strip()
    if tw:
        # 已含触发词则不重复加
        if not text.lower().startswith(tw.lower()):
            if trigger_position == "prefix":
                text = f"{tw}, {text}"
            else:  # suffix
                text = f"{text}, {tw}"
    return text, meta


def backup_and_write(caption_path: Path, content: str) -> bool:
    """写 caption 文件；已存在则先备份为 .bak（保留最近一份）。返回是否做了备份。"""
    did_backup = False
    if caption_path.exists():
        bak = caption_path.with_suffix(caption_path.suffix + ".bak")
        shutil.copy2(caption_path, bak)
        did_backup = True
    caption_path.write_text(content + "\n", encoding="utf-8")
    return did_backup


# --------------------------------------------------------------------------- #
# 文件名构图标签联动（caiyaer50 惯例：001_head / 017_half / 033_full）
# --------------------------------------------------------------------------- #
SHOT_TAG_MAP = {
    "head": ("head", "close-up head shot", "headshot"),
    "half": ("half", "half-body shot", "waist-up"),
    "full": ("full", "full-body shot", "head-to-toe"),
    "closeup": ("closeup", "close-up shot"),
    "close": ("close", "close-up shot"),
    "upper": ("upper", "upper-body shot"),
    "bust": ("bust", "bust shot"),
}


def shot_tag_from_name(filename: str) -> str:
    """从文件名里识别构图档位词（_head/_half/_full 等）。返回如 'close-up head shot'；无则空串。"""
    stem = Path(filename).stem.lower()
    for key, phrases in SHOT_TAG_MAP.items():
        # 匹配 _head / -head / head 结尾或独立段
        if re.search(rf"(?:^|[_\-]){re.escape(key)}(?:$|[_\-0-9])", stem):
            return phrases[1]
    return ""


def ensure_shot_tag(caption: str, shot_tag: str) -> str:
    """若 caption 未含构图描述则补上（去重：已有 close-up/half-body/full-body 等词就不加）。"""
    if not shot_tag:
        return caption
    low = caption.lower()
    markers = ("close-up", "closeup", "half-body", "waist-up", "full-body", "head-to-toe",
               "bust shot", "head shot", "headshot", "upper-body")
    if any(m in low for m in markers):
        return caption
    if not caption.rstrip().endswith((".", "!", "?")):
        caption = caption.rstrip(" ,") + ", "
    else:
        caption = caption.rstrip() + " "
    return caption + shot_tag


# --------------------------------------------------------------------------- #
# 任务状态
# --------------------------------------------------------------------------- #
@dataclass
class FileTask:
    path: str
    name: str
    status: str = "pending"          # pending|running|done|error|skipped|edited
    caption: str = ""
    error: str = ""
    duration_ms: int = 0
    backup: bool = False
    meta: dict = field(default_factory=dict)


@dataclass
class BatchState:
    directory: str = ""
    lora_type: str = "person"
    provider_id: str = ""
    model: str = ""
    trigger_word: str = ""
    skip_existing: bool = True
    started_at: float = 0.0
    finished_at: float = 0.0
    tasks: list[FileTask] = field(default_factory=list)
    running: bool = False
    cancel_requested: bool = False

    def stats(self) -> dict:
        total = len(self.tasks)
        done = sum(1 for t in self.tasks if t.status in ("done", "edited", "skipped"))
        errs = sum(1 for t in self.tasks if t.status == "error")
        elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0
        durations = [t.duration_ms for t in self.tasks if t.duration_ms > 0]
        avg = sum(durations) / len(durations) / 1000 if durations else 0
        return {
            "total": total, "done": done, "errors": errs,
            "elapsed_s": round(elapsed, 1),
            "avg_s": round(avg, 2),
            "success_rate": round(done / total * 100, 1) if total else 0,
        }


# --------------------------------------------------------------------------- #
# 质量抽检
# --------------------------------------------------------------------------- #
def audit_captions(
    items: list[dict],
    lora_type: str,
    trigger_word: str,
) -> dict:
    """对扫描结果做质量抽检。items 来自 scan_images 输出。

    检查项：无caption / 过短 / 过长 / 触发词丢失 / 风格词漏网 / 完全重复 / 高相似对。
    """
    import difflib

    caps: list[tuple[str, str, str]] = []   # (path, name, caption)
    missing: list[dict] = []
    for it in items:
        cp = Path(it["path"]).with_suffix(".txt")
        if not cp.is_file():
            missing.append({"path": it["path"], "name": it["name"], "caption": ""})
            continue
        caps.append((it["path"], it["name"],
                     cp.read_text(encoding="utf-8", errors="ignore").strip()))

    issues: list[dict] = []
    if missing:
        issues.append({"type": "no_caption", "label": "缺少 caption 文件",
                       "files": missing})

    def _entry(p, n, c, extra=None):
        d = {"path": p, "name": n, "caption": c[:400]}
        if extra:
            d.update(extra)
        return d

    # 过短 / 过长（词数阈值：人物60-120，风格最长150；统一警告线 30/160）
    short = [_entry(*e) for e in caps if len(e[2].split()) < 30 or len(e[2]) < 80]
    if short:
        issues.append({"type": "short", "label": "过短（<30词）", "files": short})
    longs = [_entry(*e) for e in caps if len(e[2].split()) > 160 or len(e[2]) > 1200]
    if longs:
        issues.append({"type": "long", "label": "过长（>160词）", "files": longs})

    # 触发词丢失
    tw = (trigger_word or "").strip().lower()
    if tw:
        lost = [_entry(*e) for e in caps if tw not in e[2].lower()]
        if lost:
            issues.append({"type": "missing_trigger",
                           "label": f"触发词丢失（{trigger_word}）", "files": lost})

    # 风格词漏网（风格类型专用）
    if lora_type == "style":
        leaks = []
        for p, n, c in caps:
            hits = sorted({m.group(0).lower() for m in _BLOCK_RE.finditer(c)})
            if hits:
                leaks.append(_entry(p, n, c, {"style_words": hits}))
        if leaks:
            issues.append({"type": "style_leak", "label": "风格词漏网", "files": leaks})

    # 体型词漏网（身材类型专用）
    if lora_type == "body":
        leaks = []
        for p, n, c in caps:
            hits = sorted({m.group(0).lower() for m in _BODY_RE.finditer(c)})
            if hits:
                leaks.append(_entry(p, n, c, {"style_words": hits}))
        if leaks:
            issues.append({"type": "body_leak", "label": "体型词漏网", "files": leaks})

    # 完全重复（归一化后相同）
    groups: dict[str, list] = {}
    for p, n, c in caps:
        k = re.sub(r"\s+", " ", c.lower()).strip(" .,")
        groups.setdefault(k, []).append(_entry(p, n, c))
    dups = [{"type": "duplicate", "label": f"完全重复（{len(v)} 张同文）",
             "files": v} for v in groups.values() if len(v) >= 2]
    issues.extend(dups)

    # 高相似对（≤300 张时两两比较；排除已算完全重复的）
    uniq_caps = [(p, n, c) for (p, n, c) in caps
                 if len(groups[re.sub(r"\s+", " ", c.lower()).strip(" .,")]) < 2]
    if 2 <= len(uniq_caps) <= 300:
        sims: list[dict] = []
        texts = [(p, n, re.sub(r"\s+", " ", c.lower())) for p, n, c in uniq_caps]
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if difflib.SequenceMatcher(None, texts[i][2], texts[j][2]).quick_ratio() < 0.85:
                    continue
                ratio = difflib.SequenceMatcher(None, texts[i][2], texts[j][2]).ratio()
                if ratio >= 0.88:
                    sims.append({
                        "type": "similar", "label": f"高相似对（{ratio:.0%}）",
                        "files": [_entry(p, n, c) for (p, n, c) in (uniq_caps[i], uniq_caps[j])],
                        "ratio": round(ratio, 3),
                    })
        # 合并互相相似的对成簇（传递闭包）
        if sims:
            merged: list[dict] = []
            for s in sims:
                placed = False
                for m in merged:
                    if {f["path"] for f in s["files"]} & {f["path"] for f in m["files"]}:
                        for f in s["files"]:
                            if f["path"] not in {x["path"] for x in m["files"]}:
                                m["files"].append(f)
                        m["label"] = f"高相似簇（{len(m['files'])} 张）"
                        placed = True
                        break
                if not placed:
                    merged.append(s)
            issues.extend(merged)

    total_issues = sum(len(i["files"]) for i in issues)
    # 长度分布（词数直方，10 词一档）
    word_counts = [len(c.split()) for _, _, c in caps]
    hist: dict[str, int] = {}
    for wc in word_counts:
        bucket = f"{(wc // 10) * 10}-{(wc // 10) * 10 + 9}"
        hist[bucket] = hist.get(bucket, 0) + 1
    return {
        "total_images": len(items),
        "audited": len(caps),
        "issue_files": total_issues,
        "issues": issues,
        "length_dist": {
            "min": min(word_counts) if word_counts else 0,
            "max": max(word_counts) if word_counts else 0,
            "avg": round(sum(word_counts) / len(word_counts), 1) if word_counts else 0,
            "hist": dict(sorted(hist.items(), key=lambda x: int(x[0].split("-")[0]))),
        },
    }
