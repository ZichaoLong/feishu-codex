"""
fcodex 本地 wrapper。
"""

from __future__ import annotations

import os
import pathlib
import shlex
import sys

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.config import load_config_file
from bot.constants import DEFAULT_APP_SERVER_URL, FC_DATA_DIR, PROJECT_ROOT
from bot.profile_resolution import resolve_local_default_profile_via_remote_backend
from bot.session_resolution import (
    list_current_dir_threads,
    list_global_threads,
    looks_like_thread_id,
    resolve_resume_name_via_remote_backend,
)
from bot.stores.profile_state_store import ProfileStateStore


def _has_explicit_remote(user_args: list[str]) -> bool:
    return "--remote" in user_args


def _has_explicit_profile(user_args: list[str]) -> bool:
    return "-p" in user_args or "--profile" in user_args


def _looks_like_dev_layout() -> bool:
    return (PROJECT_ROOT / "bot").is_dir() and (PROJECT_ROOT / "config").is_dir()


def _default_data_dir() -> pathlib.Path:
    raw = os.environ.get("FC_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if _looks_like_dev_layout():
        return FC_DATA_DIR
    # install.sh 的标准安装目录。即使外层 wrapper 漏传 FC_DATA_DIR，
    # 也能继续与 feishu-codex 服务共用同一份本地状态。
    return pathlib.Path.home() / ".local" / "share" / "feishu-codex"


def _inject_default_profile(user_args: list[str], profile: str) -> list[str]:
    if not profile or _has_explicit_profile(user_args):
        return list(user_args)
    return ["--profile", profile, *user_args]


def _print_thread_rows(rows: list[tuple[str, str, str, str]]) -> int:
    if not rows:
        print("未找到匹配线程。", file=sys.stderr)
        return 1

    id_width = max(len("THREAD_ID"), *(len(item[0]) for item in rows))
    provider_width = max(len("PROVIDER"), *(len(item[1]) for item in rows))
    cwd_width = max(len("CWD"), *(len(item[2]) for item in rows))
    print(
        f"{'THREAD_ID'.ljust(id_width)}  "
        f"{'PROVIDER'.ljust(provider_width)}  "
        f"{'CWD'.ljust(cwd_width)}  TITLE"
    )
    for thread_id, provider, cwd, title in rows:
        print(
            f"{thread_id.ljust(id_width)}  "
            f"{provider.ljust(provider_width)}  "
            f"{cwd.ljust(cwd_width)}  {title}"
        )
    return 0


def _handle_local_list_command(cfg: dict, app_server_url: str, scope: str) -> int:
    config = CodexAppServerConfig.from_dict(cfg)
    adapter = CodexAppServerAdapter(config)
    try:
        limit = int(cfg.get("thread_list_query_limit", 100))
        if scope == "global":
            threads = list_global_threads(adapter, limit=limit)
        else:
            cwd = os.getcwd()
            threads = list_current_dir_threads(adapter, cwd=cwd, limit=limit)
        rows = [
            (
                thread.thread_id,
                thread.model_provider or "",
                thread.cwd,
                thread.title,
            )
            for thread in threads
        ]
        return _print_thread_rows(rows)
    finally:
        adapter.stop()


def _handle_internal_command(cfg: dict, app_server_url: str, user_args: list[str]) -> int | None:
    if not user_args:
        return None

    cmd = user_args[0]
    if cmd == "sessions":
        scope = "cwd"
        if len(user_args) >= 2:
            arg = user_args[1].strip().lower()
            if arg in {"cwd", "current"}:
                scope = "cwd"
            elif arg in {"global", "all"}:
                scope = "global"
            else:
                print("用法：fcodex sessions [cwd|global]", file=sys.stderr)
                return 2
        return _handle_local_list_command(cfg, app_server_url, scope)
    if cmd in {"help-sessions", "help-resume"}:
        print("fcodex sessions [cwd|global]  使用 feishu-codex 的共享发现逻辑列出线程。")
        print("fcodex resume <thread_name>   使用共享跨 provider 精确匹配后恢复。")
        print("运行中的 TUI 内置 /resume     保持 upstream 原样，不保证跨 provider。")
        return 0
    return None


def _maybe_resolve_resume_name(cfg: dict, app_server_url: str, user_args: list[str]) -> list[str]:
    if not user_args or user_args[0] != "resume":
        return list(user_args)
    if _has_explicit_remote(user_args):
        return list(user_args)
    if len(user_args) < 2:
        return list(user_args)

    target = user_args[1].strip()
    if not target or target.startswith("-") or looks_like_thread_id(target):
        return list(user_args)

    config = CodexAppServerConfig.from_dict(cfg)
    thread = resolve_resume_name_via_remote_backend(
        base_config=config,
        app_server_url=app_server_url,
        query_limit=int(cfg.get("thread_list_query_limit", 100)),
        target=target,
    )
    resolved = list(user_args)
    resolved[1] = thread.thread_id
    return resolved


def main() -> None:
    cfg = load_config_file("codex")
    codex_command = str(cfg.get("codex_command", "codex")).strip() or "codex"
    app_server_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL
    user_args = sys.argv[1:]
    handled = _handle_internal_command(cfg, app_server_url, user_args)
    if handled is not None:
        raise SystemExit(handled)

    data_dir = _default_data_dir()
    profile_store = ProfileStateStore(data_dir)
    stored_profile = profile_store.load_default_profile()
    resolution = resolve_local_default_profile_via_remote_backend(
        base_config=CodexAppServerConfig.from_dict(cfg),
        app_server_url=app_server_url,
        stored_profile=stored_profile,
    )
    if resolution.stale_profile:
        profile_store.save_default_profile("")
    default_profile = resolution.effective_profile

    argv = [*shlex.split(codex_command)]
    user_args = _maybe_resolve_resume_name(cfg, app_server_url, user_args)
    user_args = _inject_default_profile(user_args, default_profile)
    if "--remote" not in user_args:
        argv.extend(["--remote", app_server_url])
    argv.extend(user_args)
    os.execvpe(argv[0], argv, os.environ.copy())
