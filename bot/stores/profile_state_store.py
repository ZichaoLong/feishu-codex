"""
feishu-codex 本地 profile 状态。

这份状态只影响：
- 飞书侧默认 profile
- 未显式传 `-p/--profile` 的 fcodex

不改写全局 Codex 配置。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading


class ProfileStateStore:
    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "profile_state.json"

    def load_default_profile(self) -> str:
        with self._lock:
            data = self._read_all()
        profile = data.get("default_profile")
        return str(profile).strip() if isinstance(profile, str) else ""

    def save_default_profile(self, profile: str) -> None:
        normalized = str(profile).strip()
        with self._lock:
            data = self._read_all()
            if normalized:
                data["default_profile"] = normalized
            else:
                data.pop("default_profile", None)
            self._write_all(data)

    def _read_all(self) -> dict:
        path = self._file_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_all(self, data: dict) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))
