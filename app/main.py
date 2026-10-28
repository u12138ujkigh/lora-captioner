# -*- coding: utf-8 -*-
"""LoRA Captioner WebUI — FastAPI 后端。

SSE 事件流：/api/events 推送批处理进度；其余 REST 接口管理配置与单图操作。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import sys
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import captioner as cap
from . import upscaler as upscaler
from . import providers as prov

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR.parent / "static"
CONFIG_FILE = APP_DIR.parent / "last_config.json"

app = FastAPI(title="LoRA Captioner")

# --------------------------------------------------------------------------- #
# 全局状态
# --------------------------------------------------------------------------- #
_event_queue: "queue.Queue[dict]" = queue.Queue()
_batch = cap.BatchState()

# 放大是独立功能：自己的一份批次状态，与打标互不干扰
_up_batch = {
    "running": False, "cancel": False,
    "directory": "", "out_dir": "", "multiplier": 3.0, "sharpen": False,
    "tasks": [],          # [{path,name,status,error,duration_ms,from,to}]
    "started_at": 0.0, "finished_at": 0.0,
}
_up_lock = threading.Lock()
_batch_lock = threading.Lock()
_recent_dirs: list[str] = []

# 多目录队列：{directory, lora_type, provider_id, model, ...同 StartRequest}
_queue: list[dict] = []
_queue_lock = threading.Lock()
_advance_lock = threading.Lock()   # 防止 add 与 batch_done 同时触发双推进
QUEUE_FILE = APP_DIR.parent / "queue.json"


def _queue_persist():
    """队列落盘（防重启丢失）。调用方必须不持 _queue_lock。"""
    try:
        snapshot = list(_queue)
        QUEUE_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _queue_restore():
    """启动时恢复队列；目录已不存在的项丢弃。"""
    global _queue
    try:
        if QUEUE_FILE.is_file():
            restored = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(restored, list):
                _queue = [q for q in restored
                          if isinstance(q, dict) and Path(str(q.get("directory", ""))).is_dir()]
    except Exception:
        _queue = []


_queue_restore()


def emit(event: dict) -> None:
    _event_queue.put(event)


def load_last_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_last_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
class StartRequest(BaseModel):
    directory: str
    lora_type: str
    provider_id: str
    model: str
    person_name: str = ""
    generic_subject: str = ""
    trigger_word: str = ""
    trigger_position: str = "prefix"
    style_hint: str = ""
    extra_instructions: str = ""
    skip_existing: bool = True
    concurrency: int = 3
    recursive: bool = False
    limit: int = 0            # 0 = 全部；试跑模式传 2
    shot_tags: bool = True    # 文件名构图标签联动（_head/_half/_full → caption 补构图词）
    folder_name: str = ""     # 数据集文件夹命名（{名}_{张数}；不进 caption）
    auto_rename: bool = False # 批次完成后自动重命名文件夹
    # ---- SeedVR2 素材放大（先放大再打标）----
    upscale: bool = False         # 是否在打标前放大
    upscale_multiplier: float = 3.0
    upscale_mode: str = "inplace"  # inplace=覆盖原图（原图备份）/ newfile=另存 _up 后缀
    upscale_sharpen: bool = False  # 放大后锐化


class EditRequest(BaseModel):
    path: str
    content: str
    lora_type: str = ""
    trigger_word: str = ""


class RetagRequest(BaseModel):
    path: str
    lora_type: str
    provider_id: str
    model: str
    person_name: str = ""
    generic_subject: str = ""
    trigger_word: str = ""
    trigger_position: str = "prefix"
    style_hint: str = ""
    extra_instructions: str = ""
    shot_tags: bool = True


class RetagManyRequest(BaseModel):
    paths: list[str]
    lora_type: str
    provider_id: str
    model: str
    person_name: str = ""
    generic_subject: str = ""
    trigger_word: str = ""
    trigger_position: str = "prefix"
    style_hint: str = ""
    extra_instructions: str = ""
    shot_tags: bool = True
    concurrency: int = 3


class UpscaleRequest(BaseModel):
    directory: str
    recursive: bool = False
    multiplier: float = 3.0
    sharpen: bool = False
    limit: int = 0               # 0=全部；试跑传 2
    skip_existing: bool = True   # 输出目录里已存在的跳过
    out_dir: str = ""            # 留空 = 同级 目录名_放大
    only_paths: list[str] = []   # 只处理这些（用于单张重试/重跑失败）


class TranslateRequest(BaseModel):
    provider_id: str = ""
    model: str = ""
    text: str
    engine: str = "bing"      # bing（默认，无需密钥）| llm（走 provider）
    aligned: bool = False     # 需要词级对齐映射


class AuditRequest(BaseModel):
    directory: str
    recursive: bool = False
    lora_type: str = ""
    trigger_word: str = ""


class ReplaceRequest(BaseModel):
    directory: str
    recursive: bool = False
    find: str
    replace: str
    whole_word: bool = False
    case_sensitive: bool = False
    dry_run: bool = True
    keep_backup: bool = True


# --------------------------------------------------------------------------- #
# REST 接口
# --------------------------------------------------------------------------- #
@app.get("/api/providers")
def api_providers(refresh: bool = False):
    channels = prov.all_channels(refresh=refresh)
    out = []
    for pid, ch in channels.items():
        out.append({
            "provider_id": pid,
            "label": ch.label,
            "kind": ch.kind,
            "models": [{"id": m.id, "name": m.name or m.id, "vision": m.vision} for m in ch.models],
        })
    return {"providers": out}


class ScanRequest(BaseModel):
    directory: str
    recursive: bool = False


@app.post("/api/scan2")
def api_scan2(req: ScanRequest):
    items = cap.scan_images(req.directory, req.recursive)
    return {"items": items, "count": len(items)}


def _img_size(p: Path) -> tuple[int, int]:
    """读图片宽高；失败返回 (0,0)。"""
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.width, im.height
    except Exception:
        return 0, 0


def _up_out_dir(src_dir: str, out_dir: str = "") -> Path:
    """放大输出目录：默认在源目录同级，名字加 _放大 后缀。"""
    if out_dir and out_dir.strip():
        return Path(out_dir.strip())
    src = Path(src_dir)
    return src.parent / (src.name + "_放大")


def _up_stats() -> dict:
    tasks = _up_batch["tasks"]
    total = len(tasks)
    done = sum(1 for t in tasks if t["status"] == "done")
    errs = sum(1 for t in tasks if t["status"] == "error")
    skip = sum(1 for t in tasks if t["status"] == "skipped")
    elapsed = ((_up_batch["finished_at"] or time.time()) - _up_batch["started_at"]
               if _up_batch["started_at"] else 0)
    durs = [t["duration_ms"] for t in tasks if t.get("duration_ms")]
    return {
        "total": total, "done": done, "errors": errs, "skipped": skip,
        "elapsed_s": round(elapsed, 1),
        "avg_s": round(sum(durs) / len(durs) / 1000, 2) if durs else 0,
        "success_rate": round(done / total * 100, 1) if total else 0,
    }


def _run_upscale(req: dict) -> None:
    """后台线程：逐张放大（串行；ComfyUI 一次一张更稳且显存不互相抢）。"""
    tasks = _up_batch["tasks"]
    out_root = Path(_up_batch["out_dir"])
    mult = float(req["multiplier"])
    sharpen = bool(req["sharpen"])

    emit({"type": "up_batch_start", "total": len(tasks),
          "pending": sum(1 for t in tasks if t["status"] == "pending")})

    for t in tasks:
        if _up_batch["cancel"]:
            break
        if t["status"] != "pending":
            continue
        t["status"] = "running"
        emit({"type": "up_task_start", "path": t["path"], "name": t["name"]})
        t0 = time.time()
        try:
            src = Path(t["path"])
            from PIL import Image
            with Image.open(src) as im:
                before = list(im.size)

            # 输出到同级 目录名_放大，文件名保持原样
            dst = out_root / src.name
            out_root.mkdir(parents=True, exist_ok=True)

            res = upscaler.run_one(str(src), str(dst), mult, sharpen)
            if not res.get("ok"):
                raise RuntimeError(res.get("error", "放大失败"))

            t["status"] = "done"
            t["from"] = before
            t["to"] = [res.get("width"), res.get("height")]
            t["out"] = str(dst)
            t["duration_ms"] = int((time.time() - t0) * 1000)
            emit({"type": "up_task_done", "path": t["path"], "name": t["name"],
                  "from": t["from"], "to": t["to"], "out": str(dst),
                  "duration_ms": t["duration_ms"]})
        except Exception as e:
            t["status"] = "error"
            t["error"] = str(e)
            t["duration_ms"] = int((time.time() - t0) * 1000)
            emit({"type": "up_task_error", "path": t["path"], "name": t["name"],
                  "error": str(e), "duration_ms": t["duration_ms"]})

    _up_batch["finished_at"] = time.time()
    with _up_lock:
        _up_batch["running"] = False
        _up_batch["cancel"] = False
    # 跑完让 ComfyUI 卸掉模型，把显存还回去
    try:
        upscaler.free_comfy_memory()
    except Exception:
        pass
    emit({"type": "up_batch_done", "stats": _up_stats()})


@app.post("/api/upscale/scan")
def api_upscale_scan(req: UpscaleRequest):
    """扫描预览：不启动，只列出待处理与已完成的图，供确认弹窗用。"""
    directory = req.directory.strip()
    src_root = Path(directory)
    if not src_root.is_dir():
        return JSONResponse({"error": f"目录不存在: {directory}"}, status_code=400)

    out_root = _up_out_dir(directory, req.out_dir)
    items = cap.scan_images(directory, req.recursive)
    if not items:
        return JSONResponse({"error": "目录里没有图片"}, status_code=400)

    # 递归时排除输出目录自身
    out_resolved = str(out_root.resolve()).lower()
    items = [it for it in items
             if not str(Path(it["path"]).resolve()).lower().startswith(out_resolved)]

    rows = []
    pending = done = 0
    for it in items:
        name = Path(it["path"]).name
        target = out_root / name
        exists = target.is_file()
        ow, oh = _img_size(target) if exists else (0, 0)
        rows.append({
            "path": it["path"], "name": name,
            "width": it.get("width", 0), "height": it.get("height", 0),
            "done": exists,
            "out": str(target),
            "out_width": ow, "out_height": oh,
        })
        if exists:
            done += 1
        else:
            pending += 1

    return {
        "ok": True, "src_dir": str(src_root), "out_dir": str(out_root),
        "total": len(rows), "pending": pending, "already": done,
        "items": rows,
    }


@app.post("/api/upscale/start")
def api_upscale_start(req: UpscaleRequest):
    with _up_lock:
        if _up_batch["running"]:
            return JSONResponse({"error": "放大已在跑，先停止"}, status_code=409)
        directory = req.directory.strip()
        if not Path(directory).is_dir():
            return JSONResponse({"error": f"目录不存在: {directory}"}, status_code=400)

        ok, msg = upscaler.available_for_webui()
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)

        out_root = _up_out_dir(directory, req.out_dir)
        items = cap.scan_images(directory, req.recursive)
        if not items:
            return JSONResponse({"error": "目录里没有图片"}, status_code=400)

        # 递归时排除输出目录自身，避免把放大结果又扫回来
        out_resolved = str(out_root.resolve()).lower()
        items = [it for it in items
                 if not str(Path(it["path"]).resolve()).lower().startswith(out_resolved)]

        # 只处理指定文件（单张重试 / 重跑失败）
        if req.only_paths:
            want = {str(Path(x)) for x in req.only_paths}
            items = [it for it in items if str(Path(it["path"])) in want]

        tasks = []
        skipped = 0
        for it in items:
            target = out_root / Path(it["path"]).name
            if req.skip_existing and target.is_file():
                skipped += 1
                continue
            tasks.append({"path": it["path"], "name": it["name"],
                          "status": "pending", "error": "", "duration_ms": 0})
            if req.limit and req.limit > 0 and len(tasks) >= req.limit:
                break

        if not tasks:
            return JSONResponse({"error": "没有需要放大的图片（目标文件都已存在）"},
                                status_code=400)

        _up_batch.update({
            "running": True, "cancel": False, "directory": directory,
            "out_dir": str(out_root),
            "multiplier": req.multiplier, "sharpen": req.sharpen,
            "tasks": tasks, "started_at": time.time(), "finished_at": 0.0,
        })

    threading.Thread(target=_run_upscale, args=(dict(req.model_dump()),),
                     daemon=True).start()
    return {"ok": True, "total": len(tasks), "skipped": skipped}


@app.post("/api/upscale/stop")
def api_upscale_stop():
    _up_batch["cancel"] = True
    try:
        upscaler.free_comfy_memory()
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/upscale/clear")
def api_upscale_clear():
    with _up_lock:
        if _up_batch["running"]:
            return JSONResponse({"error": "先停止放大"}, status_code=409)
        _up_batch.update({"tasks": [], "directory": "", "out_dir": "",
                          "started_at": 0.0, "finished_at": 0.0, "cancel": False})
    return {"ok": True}


@app.get("/api/upscale/batch")
def api_upscale_batch():
    with _up_lock:
        return {
            "running": _up_batch["running"],
            "directory": _up_batch["directory"],
            "out_dir": _up_batch["out_dir"],
            "multiplier": _up_batch["multiplier"],
            "stats": _up_stats(),
            "tasks": list(_up_batch["tasks"]),
        }


@app.get("/api/upscale/status")
def api_upscale_status():
    """放大能力自检：ComfyUI 在线 + 模型文件在。"""
    ok, msg = upscaler.available_for_webui()
    return {"available": ok, "message": msg,
            "multiplier": upscaler.CFG["multiplier"],
            "unet": upscaler.CFG["unet"],
            "vae": upscaler.CFG["vae"]}


@app.get("/api/config")
def api_get_config():
    return load_last_config()


@app.post("/api/config")
def api_set_config(cfg: dict):
    save_last_config(cfg)
    return {"ok": True}


@app.post("/api/start")
def api_start(req: StartRequest):
    with _batch_lock:
        if _batch.running:
            return JSONResponse({"error": "已有批次在跑，先停止或清屏"}, status_code=409)
        directory = req.directory.strip()
        if not Path(directory).is_dir():
            return JSONResponse({"error": f"目录不存在: {directory}"}, status_code=400)
        items = cap.scan_images(directory, req.recursive)
        if not items:
            return JSONResponse({"error": "目录里没有图片"}, status_code=400)
        if req.limit and req.limit > 0:
            items = items[:req.limit]

        _batch.directory = directory
        _batch.lora_type = req.lora_type
        _batch.provider_id = req.provider_id
        _batch.model = req.model
        _batch.trigger_word = req.trigger_word
        _batch.skip_existing = req.skip_existing
        _batch.started_at = time.time()
        _batch.finished_at = 0.0
        _batch.cancel_requested = False
        _batch.running = True
        _batch.tasks = []
        for it in items:
            _batch.tasks.append(cap.FileTask(path=it["path"], name=it["name"],
                                             status=("skipped" if (req.skip_existing and it["has_caption"]) else "pending"),
                                             caption=(Path(it["path"]).with_suffix(".txt").read_text(encoding="utf-8", errors="ignore").strip()
                                                      if (req.skip_existing and it["has_caption"]) else "")))

        # 保存最近配置
        save_last_config({
            "directory": directory,
            "lora_type": req.lora_type,
            "provider_id": req.provider_id,
            "model": req.model,
            "person_name": req.person_name,
            "generic_subject": req.generic_subject,
            "trigger_word": req.trigger_word,
            "trigger_position": req.trigger_position,
            "style_hint": req.style_hint,
            "extra_instructions": req.extra_instructions,
            "skip_existing": req.skip_existing,
            "concurrency": req.concurrency,
            "recursive": req.recursive,
            "shot_tags": req.shot_tags,
        })

        threading.Thread(target=_run_batch, args=(dict(req),), daemon=True).start()
        return {"ok": True, "total": len(_batch.tasks)}


# --------------------------------------------------------------------------- #
# 多目录队列
# --------------------------------------------------------------------------- #
def _validate_batch_params(params: dict) -> tuple[bool, str]:
    if not Path(params.get("directory", "")).is_dir():
        return False, f"目录不存在: {params.get('directory')}"
    if not params.get("provider_id") or not params.get("model"):
        return False, "provider/model 未选择"
    return True, ""


def _launch_batch(params: dict) -> tuple[bool, str, int]:
    """启动一个批次（与 /api/start 相同的启动逻辑，供队列复用）。"""
    items = cap.scan_images(params["directory"], params.get("recursive", False))
    if not items:
        return False, "目录里没有图片", 0
    if params.get("limit", 0) and params["limit"] > 0:
        items = items[:params["limit"]]

    _batch.directory = params["directory"]
    _batch.lora_type = params["lora_type"]
    _batch.provider_id = params["provider_id"]
    _batch.model = params["model"]
    _batch.trigger_word = params.get("trigger_word", "")
    _batch.skip_existing = params.get("skip_existing", True)
    _batch.started_at = time.time()
    _batch.finished_at = 0.0
    _batch.cancel_requested = False
    _batch.running = True
    _batch.tasks = []
    skip = params.get("skip_existing", True)
    for it in items:
        has_cap = it["has_caption"]
        _batch.tasks.append(cap.FileTask(
            path=it["path"], name=it["name"],
            status=("skipped" if (skip and has_cap) else "pending"),
            caption=(Path(it["path"]).with_suffix(".txt").read_text(encoding="utf-8", errors="ignore").strip()
                     if (skip and has_cap) else "")))
    threading.Thread(target=_run_batch, args=(params,), daemon=True).start()
    return True, "", len(_batch.tasks)


def _queue_advance():
    """队列推进：当前批次结束后启动下一个。_advance_lock 保证同一时刻只有一个推进者。"""
    with _advance_lock:
        while True:
            with _queue_lock:
                nxt = _queue.pop(0) if _queue else None
            if nxt is None:
                return
            _queue_persist()
            if _batch.running:
                time.sleep(1.0)
                with _queue_lock:
                    _queue.insert(0, nxt)
                _queue_persist()
                continue
            ok, err, total = _launch_batch(nxt)
            if ok:
                emit({"type": "queue_advance", "directory": nxt["directory"], "total": total,
                      "remaining": len(_queue)})
            else:
                emit({"type": "queue_skip", "directory": nxt["directory"], "error": err})


class QueueAddRequest(StartRequest):
    pass


@app.post("/api/queue/add")
def api_queue_add(req: QueueAddRequest):
    with _queue_lock:
        if any(q["directory"] == req.directory for q in _queue):
            return JSONResponse({"error": f"目录已在队列中: {req.directory}"}, status_code=409)
    ok, err = _validate_batch_params(dict(req))
    if not ok:
        return JSONResponse({"error": err}, status_code=400)
    params = dict(req)
    # 队列模式不保留试跑截断（试跑是交互验证用的，不该进队列）
    if params.get("limit"):
        params["limit"] = 0
    save_last_config({
        "directory": req.directory, "lora_type": req.lora_type,
        "provider_id": req.provider_id, "model": req.model,
        "person_name": req.person_name, "generic_subject": req.generic_subject,
        "trigger_word": req.trigger_word, "trigger_position": req.trigger_position,
        "style_hint": req.style_hint, "extra_instructions": req.extra_instructions,
        "skip_existing": req.skip_existing, "concurrency": req.concurrency,
        "recursive": req.recursive, "shot_tags": req.shot_tags,
        "folder_name": req.folder_name, "auto_rename": req.auto_rename,
    })
    with _queue_lock:
        _queue.append(params)
        pos = len(_queue)
    _queue_persist()
    # 若当前无批次在跑，立即异步推进
    threading.Thread(target=_queue_advance, daemon=True).start()
    return {"ok": True, "position": pos, "queue_len": len(_queue)}


@app.post("/api/queue/add_many")
def api_queue_add_many(payload: dict):
    """批量入队：{directories: [str], params: {...}}。"""
    dirs = payload.get("directories") or []
    params = payload.get("params") or {}
    added, skipped, errors = [], [], []
    for d in dirs:
        p = dict(params)
        p["directory"] = str(d).strip()
        if p.get("limit"):
            p["limit"] = 0          # 队列不吃试跑截断
        with _queue_lock:
            if any(q["directory"] == p["directory"] for q in _queue):
                skipped.append(p["directory"]); continue
        ok, err = _validate_batch_params(p)
        if not ok:
            errors.append({"directory": p["directory"], "error": err}); continue
        with _queue_lock:
            _queue.append(p)
        added.append(p["directory"])
    _queue_persist()
    if added:
        threading.Thread(target=_queue_advance, daemon=True).start()
    return {"ok": True, "added": added, "skipped_dup": skipped,
            "errors": errors, "queue_len": len(_queue)}


@app.get("/api/queue")
def api_queue_list():
    with _queue_lock:
        items = [{"directory": q["directory"], "lora_type": q["lora_type"],
                  "model": q.get("model", ""), "trigger_word": q.get("trigger_word", ""),
                  "person_name": q.get("person_name", ""), "generic_subject": q.get("generic_subject", ""),
                  "folder_name": q.get("folder_name", ""),
                  "skip_existing": q.get("skip_existing", True),
                  "image_count": len(cap.scan_images(q["directory"], q.get("recursive", False)))}
                 for q in _queue]
        return {"queue": items,
                "running": _batch.running,
                "current": _batch.directory,
                "current_stats": _batch.stats() if _batch.running else None}


@app.post("/api/queue/move")
def api_queue_move(payload: dict):
    """调整顺序：{directory, direction: 'up'|'down'|'top'|'bottom'}。"""
    d = str(payload.get("directory", "")).strip()
    direction = str(payload.get("direction", ""))
    with _queue_lock:
        idx = next((i for i, q in enumerate(_queue) if q["directory"] == d), -1)
        if idx < 0:
            return JSONResponse({"error": "不在队列中"}, status_code=404)
        item = _queue.pop(idx)
        if direction == "up" and idx > 0:
            _queue.insert(idx - 1, item)
        elif direction == "down" and idx < len(_queue):
            _queue.insert(idx + 1, item)
        elif direction == "top":
            _queue.insert(0, item)
        elif direction == "bottom":
            _queue.append(item)
        else:
            _queue.insert(idx, item)
    _queue_persist()
    return {"ok": True}


@app.post("/api/queue/skip_current")
def api_queue_skip_current():
    """跳过当前批次：请求取消，当前批结束由队列推进接管。"""
    if not _batch.running:
        return JSONResponse({"error": "当前没有在跑的批次"}, status_code=400)
    _batch.cancel_requested = True
    return {"ok": True}


@app.post("/api/queue/clear")
def api_queue_clear():
    with _queue_lock:
        n = len(_queue)
        _queue.clear()
    _queue_persist()
    return {"ok": True, "removed": n}


@app.post("/api/queue/remove")
def api_queue_remove(payload: dict):
    d = str(payload.get("directory", "")).strip()
    with _queue_lock:
        before = len(_queue)
        _queue[:] = [q for q in _queue if q["directory"] != d]
        removed = before - len(_queue)
    _queue_persist()
    return {"ok": True, "removed": removed}


def _upscale_one(task, multiplier: float, sharpen: bool, mode: str) -> tuple[str, dict]:
    """放大单张图（提交给 ComfyUI 执行，本进程不加载模型）。

    返回 (给打标用的图片路径, meta)。
    mode=inplace：覆盖原图，原图先备份为 .orig；
    mode=newfile：另存 名字_up.jpg，原图不动。
    """
    from PIL import Image

    src = Path(task.path)
    try:
        with Image.open(src) as im:
            before = im.size
    except Exception as e:
        raise RuntimeError("读取原图失败：%s" % e)

    if mode == "inplace":
        target = src
        backup = src.with_suffix(src.suffix + ".orig")
        if not backup.exists():
            shutil.copy2(src, backup)
        dst = src
    else:
        target = src.with_name(src.stem + "_up" + src.suffix)
        dst = target

    res = upscaler.run_one(str(src), str(dst), multiplier, sharpen)
    if not res.get("ok"):
        raise RuntimeError("放大失败：%s" % res.get("error", "未知错误"))

    return str(target), {
        "upscaled": True,
        "from": list(before),
        "to": [res.get("width"), res.get("height")],
        "file": target.name,
    }


def _run_batch(params: dict):
    """后台线程：并发打标。"""
    try:
        system_prompt = cap.build_system_prompt(
            lora_type=params["lora_type"],
            person_name=params.get("person_name", ""),
            generic_subject=params.get("generic_subject", ""),
            trigger_word=params.get("trigger_word", ""),
            style_hint=params.get("style_hint", ""),
            extra_instructions=params.get("extra_instructions", ""),
        )
        concurrency = max(1, min(int(params.get("concurrency", 3)), 8))
        pending = [t for t in _batch.tasks if t.status == "pending"]
        emit({"type": "batch_start", "total": len(_batch.tasks),
              "pending": len(pending), "system_prompt": system_prompt})

        idx_lock = threading.Lock()
        cursor = {"i": 0}

        def worker():
            while True:
                if _batch.cancel_requested:
                    return
                with idx_lock:
                    if cursor["i"] >= len(pending):
                        return
                    task = pending[cursor["i"]]
                    cursor["i"] += 1
                task.status = "running"
                emit({"type": "task_start", "path": task.path, "name": task.name})
                t0 = time.time()
                try:
                    # ---- 先放大（可选）----
                    caption_path = Path(task.path)
                    up_meta = None
                    if params.get("upscale"):
                        emit({"type": "task_stage", "path": task.path,
                              "name": task.name, "stage": "upscale"})
                        caption_path, up_meta = _upscale_one(
                            task,
                            float(params.get("upscale_multiplier", 3.0)),
                            bool(params.get("upscale_sharpen")),
                            params.get("upscale_mode", "inplace"),
                        )
                        emit({"type": "task_upscaled", "path": task.path,
                              "name": task.name, "meta": up_meta})
                        if _batch.cancel_requested:
                            return

                    emit({"type": "task_stage", "path": task.path,
                          "name": task.name, "stage": "caption"})
                    raw = prov.caption_image(
                        provider_id=params["provider_id"],
                        model=params["model"],
                        image_path=caption_path,
                        system_prompt=system_prompt,
                    )
                    text, meta = cap.assemble_caption(
                        raw, params["lora_type"],
                        params.get("trigger_word", ""),
                        params.get("trigger_position", "prefix"),
                    )
                    # 文件名构图标签联动（_head/_half/_full）
                    if params.get("shot_tags", True):
                        text = cap.ensure_shot_tag(text, cap.shot_tag_from_name(task.name))
                        meta = dict(meta or {})
                        meta["shot_tag"] = bool(cap.shot_tag_from_name(task.name))
                    if up_meta:
                        meta = dict(meta or {})
                        meta.update(up_meta)
                    cp = Path(task.path).with_suffix(".txt")
                    task.backup = cap.backup_and_write(cp, text)
                    task.status = "done"
                    task.caption = text
                    task.meta = meta
                    task.duration_ms = int((time.time() - t0) * 1000)
                    emit({"type": "task_done", "path": task.path, "name": task.name,
                          "caption": text, "duration_ms": task.duration_ms,
                          "backup": task.backup, "meta": meta})
                except Exception as e:
                    task.status = "error"
                    task.error = str(e)
                    task.duration_ms = int((time.time() - t0) * 1000)
                    emit({"type": "task_error", "path": task.path, "name": task.name,
                          "error": str(e), "duration_ms": task.duration_ms})

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        _batch.finished_at = time.time()
        stats = _batch.stats()
        emit({"type": "batch_done", "stats": stats,
              "cancelled": _batch.cancel_requested})
        # 批次完成 → 自动重命名文件夹（auto_rename 且填了名字）
        rename_result = None
        if params.get("auto_rename") and params.get("folder_name", "").strip():
            rename_result = cap.execute_folder_rename(
                params["directory"], params["folder_name"])
            if rename_result.get("ok") and not rename_result.get("unchanged"):
                # 目录已改名：清任务（旧路径失效），通知前端更新目录框
                with _batch_lock:
                    _batch.tasks = []
                    _batch.directory = ""
                emit({"type": "renamed",
                      "old_path": rename_result.get("old_path"),
                      "new_path": rename_result["new_path"],
                      "target": rename_result["target"]})
            elif rename_result.get("error"):
                emit({"type": "rename_failed", "error": rename_result["error"]})
        emit({"type": "batch_after", "stats": stats,
              "rename": rename_result})
        # 队列推进：批次结束后自动开下一个
        if _queue:
            threading.Thread(target=_queue_advance, daemon=True).start()
    except Exception as e:
        emit({"type": "batch_done", "stats": _batch.stats(), "error": str(e)})
    finally:
        with _batch_lock:
            _batch.running = False


@app.post("/api/stop")
def api_stop():
    _batch.cancel_requested = True
    return {"ok": True}


@app.post("/api/clear")
def api_clear():
    if _batch.running:
        _batch.cancel_requested = True
        # 等待线程退出
        for _ in range(50):
            if not _batch.running:
                break
            time.sleep(0.1)
    with _queue_lock:
        _queue.clear()
        _queue_persist()
    with _batch_lock:
        _batch.tasks = []
        _batch.directory = ""
        _batch.started_at = 0.0
        _batch.finished_at = 0.0
        _batch.cancel_requested = False
    return {"ok": True}


@app.get("/api/state")
def api_state():
    with _batch_lock:
        return {
            "running": _batch.running,
            "directory": _batch.directory,
            "lora_type": _batch.lora_type,
            "stats": _batch.stats(),
            "tasks": [
                {"path": t.path, "name": t.name, "status": t.status,
                 "caption": t.caption, "error": t.error,
                 "duration_ms": t.duration_ms, "backup": t.backup}
                for t in _batch.tasks
            ],
        }


@app.get("/api/caption")
def api_get_caption(path: str):
    cp = Path(path).with_suffix(".txt")
    if cp.is_file():
        return {"path": path, "caption": cp.read_text(encoding="utf-8", errors="ignore").strip(),
                "exists": True}
    return {"path": path, "caption": "", "exists": False}


@app.post("/api/caption")
def api_save_caption(req: EditRequest):
    cp = Path(req.path).with_suffix(".txt")
    did_backup = cap.backup_and_write(cp, req.content.strip())
    # 同步任务状态
    with _batch_lock:
        for t in _batch.tasks:
            if t.path == req.path:
                t.status = "edited"
                t.caption = req.content.strip()
                break
    return {"ok": True, "backup": did_backup}


@app.post("/api/retag")
def api_retag(req: RetagRequest):
    """单张重打（同步执行）。"""
    try:
        system_prompt = cap.build_system_prompt(
            lora_type=req.lora_type,
            person_name=req.person_name,
            generic_subject=req.generic_subject,
            trigger_word=req.trigger_word,
            style_hint=req.style_hint,
            extra_instructions=req.extra_instructions,
        )
        raw = prov.caption_image(
            provider_id=req.provider_id, model=req.model,
            image_path=Path(req.path), system_prompt=system_prompt,
        )
        text, meta = cap.assemble_caption(raw, req.lora_type, req.trigger_word, req.trigger_position)
        if req.shot_tags:
            text = cap.ensure_shot_tag(text, cap.shot_tag_from_name(Path(req.path).name))
        cp = Path(req.path).with_suffix(".txt")
        did_backup = cap.backup_and_write(cp, text)
        with _batch_lock:
            for t in _batch.tasks:
                if t.path == req.path:
                    t.status = "done"
                    t.caption = text
                    break
        return {"ok": True, "caption": text, "backup": did_backup, "meta": meta}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/retag_many")
def api_retag_many(req: RetagManyRequest):
    """批量重打（后台并发执行，SSE 推进度）。paths 来自质检面板的问题文件。"""
    with _batch_lock:
        if _batch.running:
            return JSONResponse({"error": "已有批次在跑，先停止或清屏"}, status_code=409)
        if not req.paths:
            return JSONResponse({"error": "没有要重打的文件"}, status_code=400)
        _batch.running = True
        _batch.cancel_requested = False
        _batch.started_at = time.time()
        _batch.finished_at = 0.0
        _batch.lora_type = req.lora_type
        _batch.provider_id = req.provider_id
        _batch.model = req.model
        _batch.trigger_word = req.trigger_word
        # 以当前网格为底（网格没有的文件也追加进任务表）
        known = {t.path for t in _batch.tasks}
        _batch.tasks = [t for t in _batch.tasks if t.path in set(req.paths)]
        for p in req.paths:
            if p not in known:
                _batch.tasks.append(cap.FileTask(path=p, name=Path(p).name, status="pending"))

    params = dict(req)
    threading.Thread(target=_run_batch, args=(params,), daemon=True).start()
    return {"ok": True, "total": len(req.paths)}


@app.post("/api/retag_many_sync")
def api_retag_many_sync(req: RetagManyRequest):
    """批量重打（同步版，前端逐张可反馈）。用于质检面板小组重打。"""
    results = []
    system_prompt = cap.build_system_prompt(
        lora_type=req.lora_type,
        person_name=req.person_name,
        generic_subject=req.generic_subject,
        trigger_word=req.trigger_word,
        style_hint=req.style_hint,
        extra_instructions=req.extra_instructions,
    )
    for p in req.paths:
        try:
            raw = prov.caption_image(
                provider_id=req.provider_id, model=req.model,
                image_path=Path(p), system_prompt=system_prompt,
            )
            text, meta = cap.assemble_caption(raw, req.lora_type, req.trigger_word, req.trigger_position)
            if req.shot_tags:
                text = cap.ensure_shot_tag(text, cap.shot_tag_from_name(Path(p).name))
            cp = Path(p).with_suffix(".txt")
            cap.backup_and_write(cp, text)
            with _batch_lock:
                for t in _batch.tasks:
                    if t.path == p:
                        t.status = "done"; t.caption = text; break
            results.append({"path": p, "ok": True, "caption": text})
        except Exception as e:
            results.append({"path": p, "ok": False, "error": str(e)})
    return {"ok": True, "results": results}


@app.post("/api/translate")
def api_translate(req: TranslateRequest):
    """翻译 caption。默认走 Bing（免费、无需密钥），可回退到 LLM。

    aligned=True 时额外返回词级对齐：把英文按逗号切段，逐段给出中文，
    供前端做「删中文词 → 删对应英文词」的联动。
    """
    text = (req.text or "").strip()
    if not text:
        return {"ok": True, "translation": "", "segments": []}

    # ---- 1) 取得译文 ----
    translation = ""
    used = req.engine or "bing"
    err = None

    if used == "llm" and req.provider_id and req.model:
        try:
            translation = prov.translate_text(req.provider_id, req.model, text)
        except Exception as e:
            err = str(e)
            used = "bing"
    if not translation:
        try:
            translation = prov.bing_translate(text)
        except Exception as e:
            err = str(e)

    if not translation:
        # 最后尝试 LLM（若配置了）
        if req.provider_id and req.model:
            try:
                translation = prov.translate_text(req.provider_id, req.model, text)
                used = "llm"
            except Exception as e:
                err = str(e)
    if not translation:
        return JSONResponse({"error": err or "翻译失败"}, status_code=500)

    # ---- 2) 词级对齐（按逗号分段，逐段翻译）----
    segments = []
    if req.aligned:
        parts = [x.strip() for x in text.split(",") if x.strip()]
        # Bing 支持一次多句；逐段请求会慢，这里整段翻完后按标点切分对齐
        zh_parts = []
        try:
            zh_all = prov.bing_translate(text)
            zh_parts = [x.strip() for x in re.split(r"[，,]", zh_all) if x.strip()]
        except Exception:
            zh_parts = []

        if len(zh_parts) == len(parts):
            segments = [{"en": e, "zh": z} for e, z in zip(parts, zh_parts)]
        else:
            # 段数对不上：退化为整句对齐（前端可走重译）
            segments = [{"en": text, "zh": translation, "whole": True}]

    return {"ok": True, "translation": translation,
            "segments": segments, "engine": used, "error": err}


@app.get("/api/upscale/original")
def api_upscale_original(path: str):
    """返回某张图对应的「放大前原图」，用于对比查看。

    覆盖模式：原图备份在 xxx.jpg.orig
    另存模式：放大图叫 xxx_up.jpg，原图是去掉 _up 的那个
    """
    src = Path(path)
    if not src.is_file():
        return JSONResponse({"error": "文件不存在"}, status_code=404)

    cand = None
    # 1) 覆盖模式：同目录 *.orig
    orig = src.with_suffix(src.suffix + ".orig")
    if orig.is_file():
        cand = orig
    # 2) 另存模式：xxx_up.jpg -> xxx.jpg
    elif src.stem.endswith("_up"):
        base = src.with_name(src.stem[:-3] + src.suffix)
        if base.is_file():
            cand = base
    # 3) 自己就是原图
    else:
        cand = src

    if cand is None:
        return JSONResponse({"error": "找不到原图"}, status_code=404)

    from fastapi.responses import FileResponse
    mime = "image/jpeg"
    if cand.suffix.lower() == ".png":
        mime = "image/png"
    elif cand.suffix.lower() == ".webp":
        mime = "image/webp"
    return FileResponse(cand, media_type=mime,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/pick-folder")
def api_pick_folder(initial: str = ""):
    """弹出 Windows 原生「选择文件夹」对话框，返回绝对路径。

    浏览器出于安全无法直接给出本地路径，所以由服务端（本机进程）弹窗。
    用一个隐藏的 Tk 窗口承载对话框，不显示主界面。
    """
    def _pick():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askdirectory(
                title="选择数据集文件夹",
                initialdir=initial if initial and Path(initial).is_dir() else None,
                mustexist=True,
            )
        finally:
            root.destroy()
        return path

    try:
        import tkinter  # noqa: F401
    except Exception as e:
        return JSONResponse({"error": f"当前环境没有 tkinter，无法弹出选择框：{e}"},
                            status_code=500)
    try:
        picked = _pick()
    except Exception as e:
        return JSONResponse({"error": f"打开文件夹选择器失败：{e}"}, status_code=500)

    if not picked:
        return {"ok": False, "cancelled": True, "path": ""}
    return {"ok": True, "cancelled": False, "path": str(Path(picked))}


@app.get("/api/browse")
def api_browse(path: str = ""):
    """列目录（给目录选择器用）。"""
    p = Path(path) if path else Path("D:/")
    if not p.is_dir():
        p = Path("D:/")
    dirs = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if c.is_dir() and not c.name.startswith((".", "$")):
                dirs.append({"name": c.name, "path": str(c)})
    except PermissionError:
        pass
    return {"current": str(p), "parent": str(p.parent) if str(p.parent) != str(p) else "",
            "dirs": dirs[:500]}


@app.get("/api/image")
def api_image(path: str):
    p = Path(path)
    if not p.is_file():
        return PlainTextResponse("not found", status_code=404)
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "bmp": "image/bmp"}.get(p.suffix.lstrip(".").lower(), "application/octet-stream")
    return PlainTextResponse("", status_code=200, media_type=mime) if False else FileResponseSafe(p, mime)


from fastapi.responses import FileResponse  # noqa: E402


def FileResponseSafe(p: Path, mime: str):
    return FileResponse(p, media_type=mime, filename=p.name)


@app.get("/api/events")
async def api_events():
    """SSE 事件流。"""
    async def gen():
        while True:
            try:
                item = _event_queue.get_nowait()
            except queue.Empty:
                # 心跳保活
                yield f": keepalive\n\n"
                await asyncio.sleep(0.6)
                continue
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            if item.get("type") == "batch_done":
                # 事件发完后继续挂着等下一批
                continue
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class RenameRequest(BaseModel):
    directory: str
    name: str          # 人物名 / LoRA 名；空则用目录现名


@app.get("/api/rename/preview")
def api_rename_preview(directory: str, name: str = ""):
    return cap.propose_folder_rename(directory, name)


@app.post("/api/rename/execute")
def api_rename_execute(req: RenameRequest):
    """执行文件夹重命名。重命名后清空当前批次（路径已失效）。"""
    result = cap.execute_folder_rename(req.directory, req.name)
    if result.get("error"):
        return JSONResponse(result, status_code=400)
    if result.get("ok") and not result.get("unchanged"):
        # 目录变了，旧任务路径全部失效 → 清批
        with _batch_lock:
            _batch.tasks = []
            _batch.directory = ""
        emit({"type": "renamed", "old_path": result.get("old_path"),
              "new_path": result["new_path"], "target": result["target"]})
    return result


@app.get("/api/debug/threads")
def api_debug_threads():
    """线程转储（诊断队列死锁用）。"""
    import traceback
    frames = {}
    for th in threading.enumerate():
        frames[th.name] = traceback.format_stack(sys._current_frames().get(th.ident, None) or [])
    return {"threads": frames}


@app.get("/api/template")
def api_template(lora_type: str, person_name: str = "", generic_subject: str = "",
                 trigger_word: str = "", style_hint: str = "", extra_instructions: str = ""):
    try:
        sp = cap.build_system_prompt(lora_type, person_name, generic_subject,
                                     trigger_word, style_hint, extra_instructions)
        return {"ok": True, "system_prompt": sp}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


# --------------------------------------------------------------------------- #
# 质量抽检
# --------------------------------------------------------------------------- #
@app.post("/api/audit")
def api_audit(req: AuditRequest):
    try:
        items = cap.scan_images(req.directory, req.recursive)
        if not items:
            return JSONResponse({"error": "目录里没有图片"}, status_code=400)
        result = cap.audit_captions(items, req.lora_type or "person", req.trigger_word)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --------------------------------------------------------------------------- #
# 批量查找替换
# --------------------------------------------------------------------------- #
@app.post("/api/replace")
def api_replace(req: ReplaceRequest):
    """批量查找替换 caption 文件。dry_run=True 只预览不落盘。"""
    find = req.find
    if not find.strip():
        return JSONResponse({"error": "查找内容为空"}, status_code=400)
    items = cap.scan_images(req.directory, req.recursive)
    if not items:
        return JSONResponse({"error": "目录里没有图片"}, status_code=400)

    if req.whole_word:
        flags = 0 if req.case_sensitive else re.I
        pattern = re.compile(r"\b" + re.escape(find) + r"\b", flags)
    else:
        flags = 0 if req.case_sensitive else re.I
        pattern = re.compile(re.escape(find), flags)

    hits: list[dict] = []
    changed = 0
    for it in items:
        cp = Path(it["path"]).with_suffix(".txt")
        if not cp.is_file():
            continue
        old = cp.read_text(encoding="utf-8", errors="ignore").rstrip("\n")
        if not pattern.search(old):
            continue
        new = pattern.sub(req.replace, old)
        # 拼接后清理：去多余空逗号
        new = re.sub(r"\s*,\s*,+", ", ", new)
        new = re.sub(r"^[,\s]+|[,\s]+$", "", new)
        hits.append({
            "path": it["path"], "name": it["name"],
            "before": old[:400], "after": new[:400],
            "count": len(pattern.findall(old)),
        })
        if not req.dry_run and new != old:
            cap.backup_and_write(cp, new)
            changed += 1
    return {
        "ok": True, "dry_run": req.dry_run,
        "total_files": len(items), "matched": len(hits),
        "written": changed if not req.dry_run else 0,
        "hits": hits[:500],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def main() -> None:
    port = int(os.environ.get("CAPTIONER_PORT", "7860"))
    print(f"LoRA Captioner WebUI -> http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
