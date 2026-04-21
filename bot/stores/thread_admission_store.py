"""
Per-instance visible-thread admission store.

Admission controls which persisted threads are visible on the Feishu command
surface of one instance. Local `fcodex` discovery is intentionally broader and
is not derived from this store.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading


class ThreadAdmissionStore:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "thread_admissions.json"

    def list_all(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._read_all()))

    def contains(self, thread_id: str) -> bool:
        normalized = self._normalize_thread_id(thread_id)
        if not normalized:
            return False
        with self._lock:
            return normalized in self._read_all()

    def admit(self, thread_id: str) -> bool:
        normalized = self._normalize_thread_id(thread_id)
        if not normalized:
            raise ValueError("thread_id 不能为空。")
        with self._lock:
            data = self._read_all()
            before = normalized in data
            data.add(normalized)
            self._write_all(data)
        return not before

    def revoke(self, thread_id: str) -> bool:
        normalized = self._normalize_thread_id(thread_id)
        if not normalized:
            return False
        with self._lock:
            data = self._read_all()
            if normalized not in data:
                return False
            data.remove(normalized)
            self._write_all(data)
        return True

    @staticmethod
    def _normalize_thread_id(thread_id: str) -> str:
        return str(thread_id or "").strip()

    def _read_all(self) -> set[str]:
        path = self._file_path()
        if not path.exists():
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(raw, list):
            return set()
        return {self._normalize_thread_id(item) for item in raw if self._normalize_thread_id(item)}

    def _write_all(self, data: set[str]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(sorted(data), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
