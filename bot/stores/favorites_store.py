"""
收藏线程的本地存储。
"""

import json
import os
import pathlib
import threading


class FavoritesStore:
    """管理 per-user 收藏线程列表。"""

    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "favorites.json"

    def load_user_favorites(self, user_id: str) -> set[str]:
        with self._lock:
            data = self._read_all()
        items = data.get(user_id, [])
        return {item for item in items if isinstance(item, str) and item}

    def is_starred(self, user_id: str, thread_id: str) -> bool:
        return thread_id in self.load_user_favorites(user_id)

    def toggle(self, user_id: str, thread_id: str) -> bool:
        with self._lock:
            data = self._read_all()
            favorites = {
                item for item in data.get(user_id, [])
                if isinstance(item, str) and item
            }
            if thread_id in favorites:
                favorites.remove(thread_id)
                starred = False
            else:
                favorites.add(thread_id)
                starred = True
            data[user_id] = sorted(favorites)
            self._write_all(data)
        return starred

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
        for user_id, items in raw.items():
            if not isinstance(user_id, str) or not isinstance(items, list):
                continue
            normalized[user_id] = [item for item in items if isinstance(item, str) and item]
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
