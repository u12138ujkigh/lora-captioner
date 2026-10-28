# -*- coding: utf-8 -*-
"""SeedVR2 独立放大引擎（不启动 ComfyUI 服务、不读工作流 JSON）。

从 D:\\sucai\\▶▷SeedVR2图像放大.json 提取的参数：
  UNET      seedvr2_3b_int8_convrot.safetensors
  VAE       seedvr2_ema_vae_fp16.safetensors
  放大倍数   3（PrimitiveFloat）
  缩放方式   lanczos（ResizeImageMaskNode）
  采样       euler / simple / steps=1 / cfg=1 / denoise=1
  VAE tile   512 / overlap 128 / temporal 4096 / tmp_overlap 8
  后处理     adain 色彩校正
"""
import os
import sys
import time
import threading
from pathlib import Path

from PIL import Image

# ComfyUI 安装目录：可用环境变量覆盖。只借用它的模型定义代码，
# 不启动服务、不依赖工作流 JSON。
COMFY_ROOT = os.environ.get("CAPTIONER_COMFY_ROOT", r"E:\ComfyUI-WorkFisher-V2\ComfyUI")
MODELS = os.path.join(COMFY_ROOT, "models")
if os.path.isdir(COMFY_ROOT) and COMFY_ROOT not in sys.path:
    sys.path.insert(0, COMFY_ROOT)

# torch 只在真正放大时才导入（项目 venv 里没装，缺失时前端只提示不可用）
torch = None


def _torch():
    global torch
    if torch is None:
        import torch as _t
        torch = _t
    return torch


CFG = {
    "unet": os.path.join(MODELS, "diffusion_models", "seedvr2_3b_int8_convrot.safetensors"),
    "vae": os.path.join(MODELS, "vae", "seedvr2_ema_vae_fp16.safetensors"),
    "multiplier": 3.0,
    "sampler": "euler",
    "scheduler": "simple",
    "steps": 1,
    "cfg": 1.0,
    "denoise": 1.0,
}


class SeedVR2Engine:
    """懒加载、可复用的放大引擎。"""

    def __init__(self, cfg=None):
        self.cfg = dict(CFG)
        if cfg:
            self.cfg.update(cfg)
        self._model = None
        self._vae = None
        self._device = None

    # ---------- 加载 ----------
    def _ensure_loaded(self):
        if self._model is not None:
            return
        import comfy.model_detection as md
        import comfy.model_base as model_base
        import comfy.model_management as mm
        import comfy.model_patcher as model_patcher
        import comfy.sd
        from safetensors import safe_open
        from safetensors.torch import load_file

        self._device = mm.get_torch_device()

        with safe_open(self.cfg["unet"], framework="pt", device="cpu") as f:
            keys = list(f.keys())
        torch = _torch()
        mc = md.model_config_from_unet(dict.fromkeys(keys, None), "")
        mc.set_inference_dtype(torch.float16, None)

        sd = load_file(self.cfg["unet"])
        sd = {"diffusion_model." + k: v for k, v in sd.items()}
        model = model_base.SeedVR2(mc, device=self._device)
        model.load_state_dict(sd, strict=False)
        model.eval()

        # 用 ModelPatcher 包装：comfy 的 KSampler 需要 load_device / model_options
        patcher = model_patcher.ModelPatcher(
            model, load_device=self._device, offload_device=torch.device("cpu"))
        self._model = patcher

        # VAE：用 comfy 的 VAE 体系加载权重
        from safetensors.torch import load_file as _lf
        vae_sd = _lf(self.cfg["vae"])
        self._vae = comfy.sd.VAE(sd=vae_sd, device=self._device)

    # ---------- 推理 ----------
    def upscale(self, img, multiplier=None):
        """放大一张 PIL 图（内部加 no_grad）。"""
        """放大一张 PIL 图。先按倍数用 lanczos 放大，再交给 SeedVR2 精修。"""
        self._ensure_loaded()
        torch = _torch()
        import torchvision.transforms.functional as TVF
        with torch.no_grad():
            return self._upscale_inner(img, multiplier, torch, TVF)

    def _upscale_inner(self, img, multiplier, torch, TVF):
        from torchvision.transforms.functional import InterpolationMode  # noqa: F401

        mult = self.cfg["multiplier"] if multiplier is None else multiplier

        # PIL -> tensor B,H,W,C [0,1]
        t = TVF.pil_to_tensor(img.convert("RGB")).float() / 255.0
        t = t.permute(1, 2, 0).unsqueeze(0)                 # 1,H,W,3

        # 1) 按倍数 lanczos 放大（用 PIL，质量稳定且与工作流一致）
        w, h = img.size
        nw, nh = int(round(w * mult)), int(round(h * mult))
        big = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
        t = TVF.pil_to_tensor(big).float().div(255.0)       # 3,H,W
        resized = t.permute(1, 2, 0).unsqueeze(0).to(self._device)   # 1,H,W,3
        reference = resized.clone()

        # 2) 预处理：补到 16 的倍数，转 5D (B,T,H,W,C)
        pad_h = (16 - (nh % 16)) % 16
        pad_w = (16 - (nw % 16)) % 16
        if pad_h or pad_w:
            resized = torch.nn.functional.pad(resized, (0, 0, 0, pad_w, 0, pad_h),
                                              mode="constant", value=0.0)
        _, ph, pw, _ = resized.shape
        video = resized.unsqueeze(1)                        # (1,1,H,W,C)

        # 3) VAE 编码 -> 5D latent
        encoded = self._vae.encode(video.to(self._device))
        samples = encoded["samples"] if isinstance(encoded, dict) else encoded

        # 4) 构造 conditioning（与 SeedVR2Conditioning 节点一致）
        dm = self._model.model.diffusion_model
        pos = dm.positive_conditioning.unsqueeze(0)
        neg = dm.negative_conditioning.unsqueeze(0)
        vc = samples.movedim(1, -1).contiguous()
        mask = vc.new_ones(vc.shape[:-1] + (1,))
        cond = torch.cat((vc, mask), dim=-1).movedim(-1, 1)
        positive = [[pos, {"condition": cond}]]
        negative = [[neg, {"condition": cond}]]

        # 5) 采样：走 comfy 的标准 sample()，condition 由 extra_conds 注入
        import comfy.sample as comfy_sample
        out_latent = comfy_sample.sample(
            self._model,
            noise=torch.zeros_like(samples),
            steps=int(self.cfg["steps"]),
            cfg=float(self.cfg["cfg"]),
            sampler_name=self.cfg["sampler"],
            scheduler=self.cfg["scheduler"],
            positive=positive,
            negative=negative,
            latent_image=samples,
            denoise=float(self.cfg["denoise"]),
            seed=0,
        )

        # 6) 解码
        decoded = self._vae.decode(out_latent)
        if decoded.dim() == 5:
            decoded = decoded[:, 0]                          # 取第一帧
        decoded = decoded[:1, :ph, :pw, :3].clamp(0, 1)

        # 7) adain 色彩校正（对齐到 lanczos 参考图）
        try:
            from comfy.ldm.seedvr.color_fix import adain_color_transfer
            a = decoded.permute(0, 3, 1, 2)                  # B,C,H,W
            r = reference[..., :3].permute(0, 3, 1, 2)
            a = a.to(self._device); r = r.to(self._device)
            fixed = adain_color_transfer(a, r)
            decoded = fixed.permute(0, 2, 3, 1)
        except Exception as e:
            print("  色彩校正跳过:", e)

        # 8) 裁掉 padding，转回 PIL
        out = decoded[0, :nh, :nw, :].clamp(0, 1).mul(255).byte().cpu().numpy()
        return Image.fromarray(out, "RGB")


def _probe():
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src or not os.path.isfile(src):
        # 造一张测试图
        img = Image.new("RGB", (256, 384), (40, 60, 90))
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.ellipse([80, 60, 176, 156], fill=(220, 200, 190))
        d.ellipse([40, 190, 216, 384], fill=(60, 90, 130))
        src = "_probe_in.jpg"
        img.save(src, quality=92)
        print("用合成图:", src)

    img = Image.open(src).convert("RGB")
    print(f"输入: {img.size[0]}x{img.size[1]}")

    t0 = time.time()
    eng = SeedVR2Engine()
    eng._ensure_loaded()
    t_load = time.time() - t0
    print(f"加载耗时: {t_load:.1f}s")

    t1 = time.time()
    out = eng.upscale(img)
    t_inf = time.time() - t1
    print(f"推理耗时: {t_inf:.1f}s")
    print(f"输出: {out.size[0]}x{out.size[1]}  (倍数 {out.size[0]/img.size[0]:.2f}x)")

    out.save("_probe_out.jpg", quality=95)
    print("已保存 _probe_out.jpg")




# --------------------------------------------------------------------------- #
# 子进程 worker：常驻，逐条处理 JSON 请求
# --------------------------------------------------------------------------- #
def _worker_mode() -> None:
    """从 stdin 读 JSON 行，处理放大，把结果写 stdout。

    请求: {"cmd":"upscale","src":"路径","dst":"路径","multiplier":3,"sharpen":false}
    响应: {"ok":true,"width":W,"height":H} 或 {"ok":false,"error":"..."}
    特例: {"cmd":"exit"} / {"cmd":"release"}
    """
    import json as _json
    out = sys.stdout
    eng = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = _json.loads(line)
        except Exception as e:
            out.write(_json.dumps({"ok": False, "error": "bad json: %s" % e}) + "\n")
            out.flush()
            continue

        cmd = req.get("cmd", "upscale")
        if cmd == "exit":
            out.write(_json.dumps({"ok": True, "bye": True}) + "\n")
            out.flush()
            break
        if cmd == "release":
            release()
            out.write(_json.dumps({"ok": True}) + "\n")
            out.flush()
            continue
        if cmd != "upscale":
            out.write(_json.dumps({"ok": False, "error": "unknown cmd"}) + "\n")
            out.flush()
            continue

        try:
            src = req["src"]
            dst = req.get("dst") or src
            mult = float(req.get("multiplier", CFG["multiplier"]))
            if eng is None:
                eng = globals()["get_engine"]()
            img = Image.open(src).convert("RGB")
            res = eng.upscale(img, multiplier=mult)
            if req.get("sharpen"):
                from PIL import ImageFilter
                res = res.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
            # 保持原格式保存
            if dst.lower().endswith((".jpg", ".jpeg")):
                res.save(dst, quality=95, subsampling=1)
            else:
                res.save(dst)
            out.write(_json.dumps({"ok": True, "width": res.size[0],
                                   "height": res.size[1], "dst": dst}) + "\n")
            out.flush()
        except Exception as e:
            import traceback
            out.write(_json.dumps({"ok": False, "error": str(e),
                                   "trace": traceback.format_exc()[-800:]}) + "\n")
            out.flush()




# --------------------------------------------------------------------------- #
# 单例 + 批量入口（供 main.py 调用）
# --------------------------------------------------------------------------- #
_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> "SeedVR2Engine":
    """全局单例：模型只加载一次，后续复用（加载约 20s，必须复用）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            eng = SeedVR2Engine()
            eng._ensure_loaded()
            _ENGINE = eng
        return _ENGINE


def is_available() -> tuple[bool, str]:
    """检查放大能力是否可用：torch/CUDA/模型文件。"""
    try:
        import torch  # noqa: F401
    except Exception as e:
        return False, f"当前 Python 环境没有 torch：{e}"
    try:
        import torch
        if not torch.cuda.is_available():
            return False, "没有检测到可用 CUDA 显卡"
    except Exception as e:
        return False, f"CUDA 检查失败：{e}"
    for key in ("unet", "vae"):
        if not os.path.isfile(CFG[key]):
            return False, f"模型文件缺失：{CFG[key]}"
    return True, "可用"


# --------------------------------------------------------------------------- #
# 主进程侧客户端：把放大任务交给子进程（ComfyUI 的 Python 跑 torch）
# --------------------------------------------------------------------------- #
_PYTHON = os.environ.get(
    "CAPTIONER_TORCH_PYTHON",
    os.path.join(os.path.dirname(COMFY_ROOT), "python", "python.exe"),
)
_WORKER = None
_WORKER_PID = None
_WORKER_LOCK = threading.Lock()


def _worker_cmd():
    return [_PYTHON, str(Path(__file__).resolve()), "--worker"]


def _ensure_worker():
    """启动（或复用）常驻子进程。"""
    global _WORKER, _WORKER_PID
    if _WORKER is not None and _WORKER.poll() is None:
        return _WORKER
    if not os.path.isfile(_PYTHON):
        raise RuntimeError(
            "找不到带 torch 的 Python：%s\n"
            "设置环境变量 CAPTIONER_TORCH_PYTHON 指向装好 torch 的 python.exe" % _PYTHON)
    import subprocess
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    _WORKER = subprocess.Popen(
        _worker_cmd(),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1, env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    _WORKER_PID = _WORKER.pid
    return _WORKER


def run_subprocess(src: str, dst: str, multiplier: float = None,
                   sharpen: bool = False, timeout: float = 900) -> dict:
    """把一张图交给子进程放大。返回 {ok,width,height,dst} 或 {ok:False,error}。"""
    import json as _json
    with _WORKER_LOCK:
        proc = _ensure_worker()
        req = {"cmd": "upscale", "src": str(src), "dst": str(dst),
               "multiplier": multiplier if multiplier is not None else CFG["multiplier"],
               "sharpen": bool(sharpen)}
        try:
            proc.stdin.write(_json.dumps(req) + "\n")
            proc.stdin.flush()
        except Exception:
            # 子进程挂了：重启再试一次
            _WORKER = None
            proc = _ensure_worker()
            proc.stdin.write(_json.dumps(req) + "\n")
            proc.stdin.flush()

        line = proc.stdout.readline()
        if not line:
            _WORKER = None
            return {"ok": False, "error": "放大子进程无响应（可能显存不足或崩溃）"}
        try:
            return _json.loads(line)
        except Exception as e:
            return {"ok": False, "error": "子进程返回无法解析：%s" % e}


def stop_worker() -> None:
    """关闭子进程，释放其显存。"""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            return
        try:
            _WORKER.stdin.write('{"cmd":"exit"}\n')
            _WORKER.stdin.flush()
            _WORKER.wait(timeout=10)
        except Exception:
            try:
                _WORKER.kill()
            except Exception:
                pass
        _WORKER = None


def available_for_webui() -> tuple[bool, str]:
    """WebUI 侧可用性：子进程 Python 存在 + 模型文件在。"""
    if not os.path.isfile(_PYTHON):
        return False, "找不到 torch 环境：%s" % _PYTHON
    for key in ("unet", "vae"):
        if not os.path.isfile(CFG[key]):
            return False, "模型缺失：%s" % os.path.basename(CFG[key])
    return True, "可用"


def release() -> None:
    """释放显存（清屏或切换任务时用）。"""
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            return
        try:
            import torch
            _ENGINE._model = None
            _ENGINE._vae = None
            _ENGINE = None
            torch.cuda.empty_cache()
        except Exception:
            _ENGINE = None


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _worker_mode()
    else:
        _probe()
