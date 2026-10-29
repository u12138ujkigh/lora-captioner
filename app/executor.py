# -*- coding: utf-8 -*-
"""全局单队列执行引擎。

设计要点（修复旧架构的根因）：
- 全进程只有一个执行器，同一时刻只跑一件事。
  放大、打标、全流程都排进同一个队列，不可能再出现
  「全流程在跑又点放大」导致 ComfyUI 被两拨任务争抢。
- 每个任务是一个 Job，带自己的进度与取消标志。
- 队列可持久化，重启后恢复未完成任务。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Job:
    """队列里的一个任务。"""
    id: str
    kind: str                  # "upscale" | "caption" | "pipeline"
    project_id: str = ""
    label: str = ""
    params: dict = field(default_factory=dict)
    status: str = "pending"    # pending | running | done | error | cancelled
    progress: dict = field(default_factory=dict)
    error: str = ""
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    cancel_requested: bool = False
    result: dict = field(default_factory=dict)
    # 逐张明细（供监控面板用）。每条：
    # {name, path, status: pending|running|done|error, duration_ms, error,
    #  src:[w,h], dst:[w,h], caption}
    items: list = field(default_factory=list)
    _item_index: dict = field(default_factory=dict)

    # ---------- 逐张明细 ----------
    def init_items(self, entries: list) -> None:
        """任务开始时登记待处理列表（只存轻量字段，不带二进制）。"""
        self.items = [
            {"name": e.get("name", ""), "path": e.get("path", ""),
             "status": "pending", "duration_ms": 0, "error": "",
             "src": e.get("src") or [], "dst": [], "caption": ""}
            for e in entries
        ]
        self._item_index = {it["path"]: i for i, it in enumerate(self.items)}

    def item_start(self, path: str) -> Optional[int]:
        i = self._item_index.get(path)
        if i is None:
            return None
        self.items[i]["status"] = "running"
        return i

    def item_done(self, path: str, duration_ms: int = 0,
                  dst: Optional[list] = None, caption: str = "") -> None:
        i = self._item_index.get(path)
        if i is None:
            return
        it = self.items[i]
        it["status"] = "done"
        it["duration_ms"] = duration_ms
        if dst:
            it["dst"] = dst
        if caption:
            it["caption"] = caption[:120]

    def item_error(self, path: str, error: str, duration_ms: int = 0) -> None:
        i = self._item_index.get(path)
        if i is None:
            return
        it = self.items[i]
        it["status"] = "error"
        it["error"] = str(error)[:200]
        it["duration_ms"] = duration_ms

    def items_summary(self) -> dict:
        """监控面板用的汇总（不含完整列表）。"""
        n = {"pending": 0, "running": 0, "done": 0, "error": 0}
        for it in self.items:
            n[it["status"]] = n.get(it["status"], 0) + 1
        return n

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "project_id": self.project_id,
            "label": self.label, "status": self.status,
            "progress": self.progress, "error": self.error,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_requested,
            "result": self.result,
            "params": self.params,
            "counts": self.items_summary(),
        }


class Executor:
    """全局唯一执行器：单队列、单线程、串行执行。"""

    def __init__(self, emit: Callable[[dict], None]):
        self._emit = emit
        self._queue: list[Job] = []
        self._current: Optional[Job] = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._handlers: dict[str, Callable[[Job], None]] = {}
        self._sid = 0

    # ---------- 生命周期 ----------
    def register(self, kind: str, handler: Callable[[Job], None]) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="executor")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            job = None
            with self._lock:
                pending = [j for j in self._queue if j.status == "pending"]
                if pending:
                    job = pending[0]
                    job.status = "running"
                    job.started_at = time.time()
                    self._current = job
            if job is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue

            self._emit({"type": "job_start", "job": job.to_dict()})
            handler = self._handlers.get(job.kind)
            try:
                if handler is None:
                    raise RuntimeError(f"未知任务类型：{job.kind}")
                handler(job)
                if job.cancel_requested and job.status == "running":
                    job.status = "cancelled"
                elif job.status == "running":
                    job.status = "done"
            except Exception as e:
                job.status = "error"
                job.error = str(e)
            finally:
                job.finished_at = time.time()
                with self._lock:
                    self._current = None
                self._emit({"type": "job_done", "job": job.to_dict()})

    def stop_all(self) -> None:
        self._stop = True
        self._wake.set()

    # ---------- 队列操作 ----------
    def submit(self, kind: str, label: str, params: dict,
               project_id: str = "") -> Job:
        with self._lock:
            self._sid += 1
            job = Job(
                id=f"j{self._sid}_{int(time.time()*1000)}",
                kind=kind, project_id=project_id, label=label,
                params=dict(params), created_at=time.time(),
            )
            self._queue.append(job)
        self._wake.set()
        self._emit({"type": "job_queued", "job": job.to_dict()})
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            for j in self._queue:
                if j.id == job_id and j.status in ("pending", "running"):
                    j.cancel_requested = True
                    if j.status == "pending":
                        j.status = "cancelled"
                    self._wake.set()
                    return True
        return False

    def cancel_all(self) -> None:
        with self._lock:
            for j in self._queue:
                if j.status in ("pending", "running"):
                    j.cancel_requested = True
                    if j.status == "pending":
                        j.status = "cancelled"

    def clear_finished(self) -> None:
        with self._lock:
            self._queue = [j for j in self._queue
                           if j.status in ("pending", "running")]

    def set_progress(self, job: Job, **kw) -> None:
        """更新任务进度并广播。"""
        job.progress.update(kw)
        self._emit({"type": "job_progress", "job_id": job.id,
                    "progress": job.progress, "kind": job.kind,
                    "project_id": job.project_id, "label": job.label})

    # ---------- 查询 ----------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "current": self._current.to_dict() if self._current else None,
                "queue": [j.to_dict() for j in self._queue
                          if j.status in ("pending", "running")],
                "recent": [j.to_dict() for j in self._queue
                           if j.status not in ("pending", "running")][-10:],
                "busy": self._current is not None,
            }

    def is_busy(self) -> bool:
        with self._lock:
            return self._current is not None or any(
                j.status == "pending" for j in self._queue)

    # ---------- 单任务查询（监控面板用）----------
    def get_job(self, job_id: str) -> Optional[dict]:
        """返回某个任务的完整详情，含逐张明细。"""
        with self._lock:
            job = next((j for j in self._queue if j.id == job_id), None)
            if job is None and self._current and self._current.id == job_id:
                job = self._current
            if job is None:
                return None
            d = job.to_dict()
            d["items"] = list(job.items)
            d["items_total"] = len(job.items)
            return d

    def monitor(self) -> dict:
        """监控面板总览：当前任务 + 队列 + 最近结果（含逐张明细）。"""
        with self._lock:
            cur = self._current
            out = {
                "busy": cur is not None,
                "current": None,
                "queue": [j.to_dict() for j in self._queue
                          if j.status == "pending"],
                "recent": [j.to_dict() for j in self._queue
                           if j.status not in ("pending", "running")][-5:],
            }
            if cur:
                d = cur.to_dict()
                d["items"] = list(cur.items)
                d["items_total"] = len(cur.items)
                out["current"] = d
            return out


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def save_queue(executor: Executor, path: Path) -> None:
    """只存未完成的任务，重启后可继续。"""
    try:
        snap = executor.snapshot()
        path.write_text(
            json.dumps({"queue": snap["queue"]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass


def load_queue(executor: Executor, path: Path) -> int:
    """恢复队列（只恢复 pending 的）。返回恢复数量。"""
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    n = 0
    for item in data.get("queue", []):
        if item.get("status") != "pending":
            continue
        try:
            job = Job(
                id=item["id"], kind=item["kind"],
                project_id=item.get("project_id", ""),
                label=item.get("label", ""),
                params=item.get("params", {}),
                status="pending",
                created_at=item.get("created_at", time.time()),
            )
            with executor._lock:
                executor._queue.append(job)
            n += 1
        except Exception:
            continue
    if n:
        executor._wake.set()
    return n
