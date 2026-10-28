# -*- coding: utf-8 -*-
"""SeedVR2 放大：通过 ComfyUI 的 /prompt API 执行。

不再自己加载模型 —— 那会把 3GB 权重和推理中间张量都堆进本进程，
内存和显存双爆。改为把任务交给已在运行的 ComfyUI：
它自己管模型的加载/卸载、显存复用和分块，跑完还能释放。

参数来自 D:\\sucai\\▶▷SeedVR2图像放大.json：
  UNET      seedvr2_3b_int8_convrot.safetensors
  VAE       seedvr2_ema_vae_fp16.safetensors
  倍数      PrimitiveFloat = 3（连线喂给 ResizeImageMaskNode）
  缩放      lanczos
  采样      euler / simple / steps=1 / cfg=1 / denoise=1
  VAE 分块  tile 512 / overlap 128 / temporal 4096 / tmp_overlap 8
  后处理    adain
"""
import os
import sys
import json
import time
import uuid
import shutil
import threading
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
COMFY_URL = os.environ.get("CAPTIONER_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_ROOT = os.environ.get("CAPTIONER_COMFY_ROOT", r"E:\ComfyUI-WorkFisher-V2\ComfyUI")
COMFY_INPUT = os.path.join(COMFY_ROOT, "input")

CFG = {
    "unet": "seedvr2_3b_int8_convrot.safetensors",
    "vae": "seedvr2_ema_vae_fp16.safetensors",
    "multiplier": 3.0,
    "steps": 1,
    "cfg": 1.0,
    "denoise": 1.0,
    "sampler": "euler",
    "scheduler": "simple",
    "tile": 512,
    "overlap": 128,
    "temporal": 4096,
    "temporal_overlap": 8,
    "color_fix": "adain",
}

_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #
def _get(path: str, timeout: float = 15.0) -> bytes:
    with urllib.request.urlopen(COMFY_URL + path, timeout=timeout) as r:
        return r.read()


def _post_json(path: str, obj: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        COMFY_URL + path,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


# --------------------------------------------------------------------------- #
# 可用性
# --------------------------------------------------------------------------- #
def comfy_alive() -> tuple[bool, str]:
    try:
        _get("/system_stats", timeout=5)
        return True, "在线"
    except Exception as e:
        return False, f"连不上 ComfyUI（{COMFY_URL}）：{e}"


def available_for_webui() -> tuple[bool, str]:
    ok, msg = comfy_alive()
    if not ok:
        return False, msg
    models = os.path.join(COMFY_ROOT, "models")
    for p in (os.path.join(models, "diffusion_models", CFG["unet"]),
              os.path.join(models, "vae", CFG["vae"])):
        if not os.path.isfile(p):
            return False, f"模型缺失：{os.path.basename(p)}"
    return True, "可用（走 ComfyUI）"


def get_free_vram_gb() -> float:
    try:
        d = json.loads(_get("/system_stats", timeout=5).decode("utf-8"))
        return d["devices"][0]["vram_free"] / (1024 ** 3)
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# 工作流构造（API 格式）
# --------------------------------------------------------------------------- #
def build_workflow(image_name: str, multiplier: float, sharpen: bool, prefix: str) -> dict:
    """构造最小 SeedVR2 放大链路。

    LoadImage -> 缩放 -> 预处理 -> VAE编码 -> 条件 -> KSampler
      -> VAE解码 -> 后处理 -> [锐化] -> SaveImage
    """
    wf = {
        "1": {"class_type": "LoadImage",
              "inputs": {"image": image_name, "upload": "image"}},
        "2": {"class_type": "ResizeImageMaskNode",
              "inputs": {"input": ["1", 0],
                         "resize_type": "scale by multiplier",
                         "resize_type.multiplier": float(multiplier),
                         "scale_method": "lanczos"}},
        "3": {"class_type": "SeedVR2Preprocess",
              "inputs": {"resized_images": ["2", 0]}},
        "4": {"class_type": "VAELoader",
              "inputs": {"vae_name": CFG["vae"]}},
        "5": {"class_type": "VAEEncodeTiled",
              "inputs": {"pixels": ["3", 0], "vae": ["4", 0],
                         "tile_size": CFG["tile"], "overlap": CFG["overlap"],
                         "temporal_size": CFG["temporal"],
                         "temporal_overlap": CFG["temporal_overlap"]}},
        "6": {"class_type": "UNETLoader",
              "inputs": {"unet_name": CFG["unet"], "weight_dtype": "default"}},
        "7": {"class_type": "SeedVR2Conditioning",
              "inputs": {"model": ["6", 0], "vae_conditioning": ["5", 0]}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["6", 0],
                         "positive": ["7", 0], "negative": ["7", 1],
                         "latent_image": ["5", 0],
                         "seed": 0, "steps": CFG["steps"], "cfg": CFG["cfg"],
                         "sampler_name": CFG["sampler"],
                         "scheduler": CFG["scheduler"],
                         "denoise": CFG["denoise"]}},
        "9": {"class_type": "VAEDecodeTiled",
              "inputs": {"samples": ["8", 0], "vae": ["4", 0],
                         "tile_size": CFG["tile"], "overlap": CFG["overlap"],
                         "temporal_size": CFG["temporal"],
                         "temporal_overlap": CFG["temporal_overlap"]}},
        "10": {"class_type": "SeedVR2PostProcessing",
               "inputs": {"images": ["9", 0],
                          "original_resized_images": ["2", 0],
                          "color_correction_method": CFG["color_fix"]}},
    }

    last = ["10", 0]
    if sharpen:
        wf["11"] = {"class_type": "ImageSmartSharpen+",
                    "inputs": {"image": last, "noise_radius": 3,
                               "preserve_edges": 0.75,
                               "sharpen": 0.6, "ratio": 0.5}}
        last = ["11", 0]

    wf["99"] = {"class_type": "SaveImage",
                "inputs": {"images": last, "filename_prefix": prefix}}
    return wf


# --------------------------------------------------------------------------- #
# 上传 / 执行 / 下载
# --------------------------------------------------------------------------- #
def _upload(image_path: str) -> str:
    src = Path(image_path)
    if not os.path.isdir(COMFY_INPUT):
        raise RuntimeError(f"ComfyUI input 目录不存在：{COMFY_INPUT}")
    name = f"cap_{uuid.uuid4().hex[:12]}{src.suffix.lower()}"
    shutil.copy2(src, os.path.join(COMFY_INPUT, name))
    return name


def _delete_input(name: str) -> None:
    try:
        p = os.path.join(COMFY_INPUT, name)
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _queue(wf: dict) -> str:
    r = _post_json("/prompt", {"prompt": wf,
                               "client_id": f"captioner-{uuid.uuid4().hex[:8]}"})
    pid = r.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 拒绝任务：{str(r)[:300]}")
    return pid


def _wait(pid: str, timeout: float = 600.0) -> list:
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(1.5)
        try:
            hist = json.loads(_get(f"/history/{pid}", timeout=15).decode("utf-8"))
        except Exception:
            continue
        if pid not in hist:
            continue
        entry = hist[pid]
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            detail = ""
            for m in status.get("messages", []):
                if isinstance(m, (list, tuple)) and len(m) > 1:
                    detail += str(m[1])[:300]
            raise RuntimeError(f"ComfyUI 执行出错：{detail or '未知错误'}")
        imgs = []
        for node_out in entry.get("outputs", {}).values():
            for im in node_out.get("images", []):
                imgs.append(im)
        if imgs:
            return imgs
        if status.get("completed"):
            return imgs
    raise RuntimeError(f"ComfyUI 超时（{timeout:.0f}s）")


def _download(im: dict, dst_path: str) -> None:
    q = urllib.parse.urlencode({
        "filename": im["filename"],
        "subfolder": im.get("subfolder", ""),
        "type": im.get("type", "output"),
    })
    data = _get(f"/view?{q}", timeout=120)
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
    with open(dst_path, "wb") as f:
        f.write(data)


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def run_one(src: str, dst: str, multiplier: float = None,
            sharpen: bool = False, timeout: float = 600.0) -> dict:
    """放大一张图：上传 -> ComfyUI 执行 -> 下载结果。"""
    mult = CFG["multiplier"] if multiplier is None else float(multiplier)
    named = None
    with _lock:
        try:
            ok, msg = comfy_alive()
            if not ok:
                return {"ok": False, "error": msg}

            named = _upload(src)
            prefix = f"captioner_up_{uuid.uuid4().hex[:8]}"
            wf = build_workflow(named, mult, sharpen, prefix)
            pid = _queue(wf)
            imgs = _wait(pid, timeout)
            if not imgs:
                return {"ok": False, "error": "ComfyUI 没有返回图片"}

            _download(imgs[0], dst)

            from PIL import Image
            with Image.open(dst) as im:
                size = list(im.size)
            return {"ok": True, "width": size[0], "height": size[1], "dst": dst}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if named:
                _delete_input(named)


def free_comfy_memory() -> None:
    """让 ComfyUI 卸载模型、释放显存。"""
    try:
        _post_json("/free", {"unload_models": True, "free_memory": True}, timeout=15)
    except Exception:
        pass


def release() -> None:
    """兼容旧接口。"""
    free_comfy_memory()


# --------------------------------------------------------------------------- #
# 命令行自测
# --------------------------------------------------------------------------- #
def _probe():
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    print("ComfyUI:", comfy_alive())
    print("可用性:", available_for_webui())
    print(f"空闲显存: {get_free_vram_gb():.2f} GB")

    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src or not os.path.isfile(src):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (256, 384), (40, 60, 90))
        d = ImageDraw.Draw(img)
        d.ellipse([80, 60, 176, 156], fill=(220, 200, 190))
        d.ellipse([40, 190, 216, 384], fill=(60, 90, 130))
        src = "_probe_in.jpg"
        img.save(src, quality=92)
        print("用合成图:", src)

    from PIL import Image
    print("输入:", Image.open(src).size)
    t0 = time.time()
    r = run_one(src, "_probe_out.jpg", 3.0, False)
    print(f"结果: {r}  耗时 {time.time()-t0:.1f}s")
    if r.get("ok"):
        print("输出:", Image.open("_probe_out.jpg").size)
    free_comfy_memory()
    print("已请求 ComfyUI 释放显存")


if __name__ == "__main__":
    _probe()
