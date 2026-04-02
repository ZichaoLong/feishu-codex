"""
共享常量与工具函数。
"""

import datetime
import os
import pathlib

KEYWORD = "CODEX"

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

BOT_DIR = pathlib.Path(__file__).parent
PROJECT_ROOT = BOT_DIR.parent
FC_DATA_DIR = PROJECT_ROOT / "data" / "feishu_codex"

DEFAULT_SOURCE_KINDS = ["cli", "vscode", "exec", "appServer"]
DEFAULT_SESSION_RECENT_LIMIT = 5
DEFAULT_SESSION_STARRED_LIMIT = 20
DEFAULT_THREAD_LIST_QUERY_LIMIT = 100
DEFAULT_HISTORY_PREVIEW_ROUNDS = 3
DEFAULT_STREAM_PATCH_INTERVAL_MS = 700
DEFAULT_APP_SERVER_MODE = "managed"
DEFAULT_APP_SERVER_URL = "ws://127.0.0.1:8765"


def display_path(path: str, base: str = "") -> str:
    """格式化路径用于展示，尽量选择更短的表达。"""
    if not path or not os.path.isabs(path):
        return path

    candidates = [path]

    if base:
        try:
            candidates.append(os.path.relpath(path, base))
        except ValueError:
            pass

    home = os.path.expanduser("~")
    if path == home:
        candidates.append("~")
    elif path.startswith(home + os.sep):
        candidates.append("~" + path[len(home):])

    return min(candidates, key=len)


def resolve_working_dir(raw: str = "", *, fallback: str = "") -> str:
    """解析工作目录，始终返回真实绝对路径。"""
    base = raw or fallback or os.getcwd()
    return os.path.realpath(base)


def format_timestamp(ts: int | float | None) -> str:
    """将秒级 Unix 时间戳格式化为北京时间。"""
    if ts in (None, 0):
        return "-"
    try:
        dt = datetime.datetime.fromtimestamp(float(ts), tz=BEIJING_TZ)
    except (TypeError, ValueError, OSError):
        return "-"
    return dt.strftime("%m-%d %H:%M")


def shorten(text: str, limit: int) -> str:
    """按字符数截断文本。"""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"
