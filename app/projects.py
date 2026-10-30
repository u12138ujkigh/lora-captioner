# -*- coding: utf-8 -*-
"""项目管理：一个项目 = 一个源目录 + 一套配置。

配置文件 projects.json 结构：
{
  "projects": [
    {
      "id": "p1",
      "name": "Yanni",
      "directory": "D:\\data\\Yanni",
      "out_dir": "D:\\data\\Yanni_放大",     # 放大产物
      "config": { ... 打标/放大参数 ... },
      "created_at": 1234567890.0,
      "updated_at": 1234567890.0
    }
  ],
  "active_id": "p1"
}
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_lock = threading.RLock()

# 项目的默认配置（新建项目时的初值）
DEFAULT_CONFIG = {
    # 打标
    "lora_type": "person",
    "provider_id": "",
    "model": "",
    "effort": "",
    "person_name": "",
    "generic_subject": "a young East Asian woman",
    "trigger_word": "",
    "trigger_position": "prefix",
    "style_hint": "",
    "extra_instructions": "",
    "skip_existing": True,
    "concurrency": 3,
    "recursive": False,
    "shot_tags": True,
    "folder_name": "",
    "auto_rename": False,
    # 放大
    "multiplier": 3.0,
    "sharpen": False,
}


class ProjectStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {"projects": [], "active_id": ""}
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("projects"), list):
                self._data = {
                    "projects": raw.get("projects", []),
                    "active_id": raw.get("active_id", ""),
                }
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    # ---------- 查询 ----------
    def all(self) -> list[dict]:
        with _lock:
            out = []
            for p in self._data["projects"]:
                q = dict(p)
                q.setdefault("archived", False)
                out.append(q)
            return out

    def get(self, pid: str) -> Optional[dict]:
        with _lock:
            p = next((x for x in self._data["projects"] if x["id"] == pid), None)
            if p is None:
                return None
            q = dict(p)
            q.setdefault("archived", False)
            return q

    def active(self) -> Optional[dict]:
        with _lock:
            pid = self._data.get("active_id", "")
            if pid:
                p = self.get(pid)
                if p:
                    return p
            # 没指定或指定的不存在：优先挑未归档的
            alive = [x for x in self.all() if not x.get("archived")]
            pool = alive or self.all()
            return pool[0] if pool else None

    def set_active(self, pid: str) -> bool:
        with _lock:
            if not any(p["id"] == pid for p in self._data["projects"]):
                return False
            self._data["active_id"] = pid
            self._save()
            return True

    # ---------- 增删改 ----------
    def create(self, name: str, directory: str,
               config: Optional[dict] = None,
               out_dir: Optional[str] = None) -> dict:
        with _lock:
            pid = "p" + uuid.uuid4().hex[:10]
            now = time.time()
            src = Path(directory)
            proj = {
                "id": pid,
                "name": name or src.name or "未命名",
                "directory": str(src),
                # 留空则推导为同级的「目录名_放大」
                "out_dir": str(out_dir).strip() if out_dir and str(out_dir).strip()
                           else str(src.parent / (src.name + "_放大")),
                "config": dict(DEFAULT_CONFIG, **(config or {})),
                "archived": False,
                "created_at": now,
                "updated_at": now,
            }
            self._data["projects"].append(proj)
            if not self._data.get("active_id"):
                self._data["active_id"] = pid
            self._save()
            return proj

    def update(self, pid: str, **fields) -> Optional[dict]:
        with _lock:
            # 注意：必须取 _data 里的原始引用。
            # get() 返回的是副本（为了给老数据补 archived 默认值），
            # 改副本不会写回 _data，保存后就丢了。
            p = next((x for x in self._data["projects"] if x["id"] == pid), None)
            if not p:
                return None
            p.setdefault("archived", False)
            if "config" in fields and isinstance(fields["config"], dict):
                # 合并而不是替换，避免丢字段
                p["config"] = dict(p.get("config") or {}, **fields.pop("config"))
            for k, v in fields.items():
                if k in ("id", "created_at"):
                    continue
                p[k] = v
            # 目录变了就重算输出目录
            if "directory" in fields:
                src = Path(p["directory"])
                p["out_dir"] = str(src.parent / (src.name + "_放大"))
            p["updated_at"] = time.time()
            self._save()
            return p

    def set_archived(self, pid: str, archived: bool) -> Optional[dict]:
        """归档 / 取消归档。"""
        return self.update(pid, archived=bool(archived))

    def delete(self, pid: str) -> bool:
        with _lock:
            before = len(self._data["projects"])
            self._data["projects"] = [p for p in self._data["projects"]
                                      if p["id"] != pid]
            if len(self._data["projects"]) == before:
                return False
            if self._data.get("active_id") == pid:
                self._data["active_id"] = (
                    self._data["projects"][0]["id"]
                    if self._data["projects"] else "")
            self._save()
            return True

    # ---------- 迁移旧配置 ----------
    def migrate_legacy(self, legacy: dict) -> Optional[dict]:
        """把旧的 ui_config.json / last_config.json 变成一个项目。"""
        if not legacy:
            return None
        cap = legacy.get("caption") or legacy or {}
        ups = legacy.get("upscale") or {}
        directory = str(cap.get("directory") or "").strip()
        if not directory:
            return None
        with _lock:
            # 已有同目录项目就不重复建
            for p in self._data["projects"]:
                if str(p.get("directory", "")).lower() == directory.lower():
                    return p
        cfg = dict(DEFAULT_CONFIG)
        for k in ("lora_type", "provider_id", "model", "effort", "person_name",
                  "generic_subject", "trigger_word", "trigger_position",
                  "style_hint", "extra_instructions", "skip_existing",
                  "concurrency", "recursive", "shot_tags", "folder_name",
                  "auto_rename"):
            if k in cap and cap[k] not in (None, ""):
                cfg[k] = cap[k]
        if ups.get("multiplier"):
            cfg["multiplier"] = ups["multiplier"]
        if ups.get("sharpen") is not None:
            cfg["sharpen"] = ups["sharpen"]
        name = Path(directory).name or "导入项目"
        return self.create(name, directory, cfg)
