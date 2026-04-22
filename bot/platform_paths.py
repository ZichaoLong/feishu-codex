"""
Cross-platform local filesystem layout helpers.

The repository keeps an explicit separation between:

- per-machine config root
- per-machine data root
- user-facing launcher directory

Development checkouts keep using the repo-local layout so tests and direct
source execution remain stable.
"""

from __future__ import annotations

import os
import pathlib
import sys

from bot.constants import FC_DATA_DIR, PROJECT_ROOT

APP_NAME = "feishu-codex"
ENV_FILE_NAME = "feishu-codex.env"
LOG_FILE_NAME = "feishu-codex.log"


def current_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def is_windows() -> bool:
    return current_platform() == "windows"


def is_macos() -> bool:
    return current_platform() == "macos"


def is_linux() -> bool:
    return current_platform() == "linux"


def looks_like_dev_layout() -> bool:
    return (PROJECT_ROOT / "bot").is_dir() and (PROJECT_ROOT / "config").is_dir()


def default_config_root() -> pathlib.Path:
    raw = os.environ.get("FC_CONFIG_ROOT", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if looks_like_dev_layout():
        return PROJECT_ROOT / "config"
    home = pathlib.Path.home()
    if is_windows():
        appdata = pathlib.Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return appdata / APP_NAME / "config"
    if is_macos():
        return home / "Library" / "Application Support" / APP_NAME / "config"
    return home / ".config" / APP_NAME


def default_data_root() -> pathlib.Path:
    raw = os.environ.get("FC_DATA_ROOT", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if looks_like_dev_layout():
        return FC_DATA_DIR
    home = pathlib.Path.home()
    if is_windows():
        local_appdata = pathlib.Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return local_appdata / APP_NAME / "data"
    if is_macos():
        return home / "Library" / "Application Support" / APP_NAME / "data"
    return home / ".local" / "share" / APP_NAME


def default_user_bin_dir() -> pathlib.Path:
    raw = os.environ.get("FC_BIN_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    home = pathlib.Path.home()
    if is_windows():
        local_appdata = pathlib.Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return local_appdata / APP_NAME / "bin"
    return home / ".local" / "bin"


def default_env_file() -> pathlib.Path:
    raw = os.environ.get("FC_ENV_FILE", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_config_root() / ENV_FILE_NAME


def default_log_file(data_dir: pathlib.Path | str | None = None) -> pathlib.Path:
    root = pathlib.Path(data_dir).expanduser() if data_dir is not None else default_data_root()
    return root / LOG_FILE_NAME


def default_systemd_user_dir() -> pathlib.Path:
    return pathlib.Path.home() / ".config" / "systemd" / "user"


def default_launch_agent_dir() -> pathlib.Path:
    return pathlib.Path.home() / "Library" / "LaunchAgents"
