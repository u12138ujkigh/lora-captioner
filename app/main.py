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
_batch_lock = threading.Lock()
_recent_dirs: list[str] = []

# 多目录队列：{directory, lora_type, provider_id, model, ...同 StartRequest}
_queue: list[dict] = []
_queue_lock = threading.Lock()
_advance_lock = threading.Lock()   # 防止 add 与 batch_done 同时触发双推进


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


class TranslateRequest(BaseModel):
    provider_id: str
    model: str
    text: str


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
                if _queue:
                    nxt = _queue.pop(0)
                else:
                    return
            if _batch.running:
                time.sleep(1.0)
                with _queue_lock:
                    _queue.insert(0, nxt)
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
    save_last_config({
        "directory": req.directory, "lora_type": req.lora_type,
        "provider_id": req.provider_id, "model": req.model,
        "person_name": req.person_name, "generic_subject": req.generic_subject,
        "trigger_word": req.trigger_word, "trigger_position": req.trigger_position,
        "style_hint": req.style_hint, "extra_instructions": req.extra_instructions,
        "skip_existing": req.skip_existing, "concurrency": req.concurrency,
        "recursive": req.recursive, "shot_tags": req.shot_tags,
    })
    with _queue_lock:
        _queue.append(params)
        pos = len(_queue)
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
        with _queue_lock:
            if any(q["directory"] == p["directory"] for q in _queue):
                skipped.append(p["directory"]); continue
        ok, err = _validate_batch_params(p)
        if not ok:
            errors.append({"directory": p["directory"], "error": err}); continue
        with _queue_lock:
            _queue.append(p)
        added.append(p["directory"])
    if added:
        threading.Thread(target=_queue_advance, daemon=True).start()
    return {"ok": True, "added": added, "skipped_dup": skipped,
            "errors": errors, "queue_len": len(_queue)}


@app.get("/api/queue")
def api_queue_list():
    with _queue_lock:
        return {"queue": [{"directory": q["directory"], "lora_type": q["lora_type"],
                           "model": q.get("model", ""), "trigger_word": q.get("trigger_word", "")}
                          for q in _queue],
                "running": _batch.running,
                "current": _batch.directory}


@app.post("/api/queue/clear")
def api_queue_clear():
    with _queue_lock:
        n = len(_queue)
        _queue.clear()
    return {"ok": True, "removed": n}


@app.post("/api/queue/remove")
def api_queue_remove(payload: dict):
    d = str(payload.get("directory", "")).strip()
    with _queue_lock:
        before = len(_queue)
        _queue[:] = [q for q in _queue if q["directory"] != d]
        removed = before - len(_queue)
    return {"ok": True, "removed": removed}


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
                    raw = prov.caption_image(
                        provider_id=params["provider_id"],
                        model=params["model"],
                        image_path=Path(task.path),
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


@app.post("/api/translate")
def api_translate(req: TranslateRequest):
    try:
        out = prov.translate_text(req.provider_id, req.model, req.text)
        return {"ok": True, "translation": out}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
