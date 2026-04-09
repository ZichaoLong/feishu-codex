"""
收藏线程的本地存储。
"""

import json
import os
import pathlib
import threading


class FavoritesStore:
    """管理按 open_id 归属的收藏线程列表。"""

    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "favorites.json"

    def load_favorites(self, open_id: str) -> set[str]:
        with self._lock:
            data = self._read_all()
        items = data.get(open_id, [])
        return {item for item in items if isinstance(item, str) and item}

    def is_starred(self, open_id: str, thread_id: str) -> bool:
        return thread_id in self.load_favorites(open_id)

    def toggle(self, open_id: str, thread_id: str) -> bool:
        with self._lock:
            data = self._read_all()
            favorites = {
                item for item in data.get(open_id, [])
                if isinstance(item, str) and item
            }
            if thread_id in favorites:
                favorites.remove(thread_id)
                starred = False
            else:
                favorites.add(thread_id)
                starred = True
            data[open_id] = sorted(favorites)
            self._write_all(data)
        return starred

    def remove(self, open_id: str, thread_id: str) -> bool:
        with self._lock:
            data = self._read_all()
            favorites = {
                item for item in data.get(open_id, [])
                if isinstance(item, str) and item
            }
            if thread_id not in favorites:
                return False
            favorites.remove(thread_id)
            data[open_id] = sorted(favorites)
            self._write_all(data)
        return True

    def remove_thread_globally(self, thread_id: str) -> bool:
        with self._lock:
            data = self._read_all()
            changed = False
            for open_id in list(data):
                favorites = {
                    item for item in data.get(open_id, [])
                    if isinstance(item, str) and item
                }
                if thread_id not in favorites:
                    continue
                favorites.remove(thread_id)
                changed = True
                if favorites:
                    data[open_id] = sorted(favorites)
                else:
                    data.pop(open_id, None)
            if changed:
                self._write_all(data)
        return changed

    def _read_all(self) -> dict[str, list[str]]:
        path = self._file_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        normalized: dict[str, list[str]] = {}
        for open_id, items in raw.items():
            if not isinstance(open_id, str) or not isinstance(items, list):
                continue
            normalized[open_id] = [item for item in items if isinstance(item, str) and item]
        return normalized

    def _write_all(self, data: dict[str, list[str]]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))
