"""
fcodex 本地 wrapper。
"""

from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import replace

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

_WRAPPER_COMMANDS = {
    "/help",
    "/help-resume",
    "/help-session",
    "/profile",
    "/rm",
    "/resume",
    "/session",
    "/sessions",
}

_OPTIONS_WITH_VALUE = {
    "-C",
    "--add-dir",
    "-a",
    "--ask-for-approval",
    "-c",
    "--config",
    "--cd",
    "--disable",
    "--enable",
    "-i",
    "--image",
    "--local-provider",
    "-m",
    "--model",
    "-p",
    "--profile",
    "--remote",
    "--remote-auth-token-env",
    "-s",
    "--sandbox",
}


def _has_option(user_args: list[str], names: tuple[str, ...]) -> bool:
    for arg in user_args:
        for name in names:
            if arg == name or arg.startswith(f"{name}="):
                return True
    return False


def _has_explicit_remote(user_args: list[str]) -> bool:
    return _has_option(user_args, ("--remote",))


def _has_explicit_profile(user_args: list[str]) -> bool:
    return _has_option(user_args, ("-p", "--profile"))


def _has_explicit_cwd(user_args: list[str]) -> bool:
    return _has_option(user_args, ("-C", "--cd"))


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


def _inject_default_cwd(user_args: list[str]) -> list[str]:
    if _has_explicit_cwd(user_args):
        return list(user_args)
    return ["--cd", os.getcwd(), *user_args]


def _terminal_columns(default: int = 120) -> int:
    try:
        return shutil.get_terminal_size((default, 20)).columns
    except Exception:
        return default


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _print_thread_rows(rows: list[tuple[str, str, str, str]]) -> int:
    if not rows:
        print("未找到匹配线程。", file=sys.stderr)
        return 1

    columns = _terminal_columns()
    id_width = 36
    provider_width = min(max(len("PROVIDER"), *(len(item[1]) for item in rows)), 18)
    cwd_width = min(max(len("CWD"), *(len(item[2]) for item in rows)), 32)
    fixed_width = id_width + provider_width + cwd_width + 6
    title_width = max(24, columns - fixed_width)
    print(
        f"{'THREAD_ID'.ljust(id_width)}  "
        f"{'PROVIDER'.ljust(provider_width)}  "
        f"{'CWD'.ljust(cwd_width)}  TITLE"
    )
    for thread_id, provider, cwd, title in rows:
        print(
            f"{thread_id.ljust(id_width)}  "
            f"{_truncate(provider, provider_width).ljust(provider_width)}  "
            f"{_truncate(cwd, cwd_width).ljust(cwd_width)}  "
            f"{_truncate(title, title_width)}"
        )
    return 0


def _handle_local_list_command(cfg: dict, app_server_url: str, scope: str) -> int:
    config = _remote_adapter_config(cfg, app_server_url)
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


def _remote_adapter_config(cfg: dict, app_server_url: str) -> CodexAppServerConfig:
    config = CodexAppServerConfig.from_dict(cfg)
    return replace(config, app_server_mode="remote", app_server_url=app_server_url)


def _runtime_profile_summary(
    adapter: CodexAppServerAdapter,
    profile_store: ProfileStateStore,
    base_config: CodexAppServerConfig,
    app_server_url: str,
) -> tuple[list[str], str | None]:
    runtime = adapter.read_runtime_config()
    resolution = resolve_local_default_profile_via_remote_backend(
        base_config=base_config,
        app_server_url=app_server_url,
        stored_profile=profile_store.load_default_profile(),
    )
    if resolution.stale_profile:
        profile_store.save_default_profile("")
    local_profile = resolution.effective_profile or ""

    lines = [
        f"feishu-codex / fcodex 默认 profile：`{local_profile or '（未设置）'}`",
        f"当前运行时 provider：`{runtime.current_model_provider or '（未设置）'}`",
    ]
    if runtime.profiles:
        lines.append("可用 profile：")
        for profile in runtime.profiles:
            provider = profile.model_provider or "（未显式设置 provider）"
            marker = " <- 默认" if profile.name == local_profile else ""
            lines.append(f"- `{profile.name}` -> `{provider}`{marker}")
    else:
        lines.append("未在当前 Codex 配置中发现可用 profile。")
    lines.append("说明：这里只改 feishu-codex 与未显式 `-p/--profile` 的 fcodex 默认 profile。")
    lines.append("说明：不会改动裸 `codex` 全局配置；`fcodex -p <profile>` 永远以显式参数为准。")
    if resolution.stale_profile:
        lines.append(
            f"注意：之前保存的默认 profile `{resolution.stale_profile}` 已不存在，现已自动清空并回退到 Codex 原生默认。"
        )
    return lines, runtime.current_profile


def _handle_profile_command(cfg: dict, app_server_url: str, user_args: list[str], data_dir: pathlib.Path) -> int:
    config = _remote_adapter_config(cfg, app_server_url)
    adapter = CodexAppServerAdapter(config)
    profile_store = ProfileStateStore(data_dir)
    try:
        if len(user_args) > 2:
            print("用法：fcodex /profile [profile_name]", file=sys.stderr)
            return 2
        if len(user_args) == 1:
            lines, _ = _runtime_profile_summary(adapter, profile_store, config, app_server_url)
            print("\n".join(lines))
            return 0

        target_profile = user_args[1].strip()
        if not target_profile:
            print("用法：fcodex /profile [profile_name]", file=sys.stderr)
            return 2
        runtime = adapter.read_runtime_config()
        profiles = {profile.name: profile for profile in runtime.profiles}
        if target_profile not in profiles:
            print(f"未找到 profile：`{target_profile}`", file=sys.stderr)
            return 2
        profile_store.save_default_profile(target_profile)
        provider = profiles[target_profile].model_provider or "（未显式设置 provider）"
        print(f"feishu-codex / fcodex 默认 profile 已切换为：`{target_profile}`")
        print(f"对应 provider：`{provider}`")
        print("说明：这不会改动裸 `codex` 全局配置。")
        print("说明：已打开的 fcodex / Codex TUI 不会被强制热切换；新的 wrapper 启动会读取这里。")
        return 0
    finally:
        adapter.stop()


def _resolve_thread_target_via_remote_backend(
    cfg: dict,
    app_server_url: str,
    target: str,
) -> tuple[ThreadSummary | None, str | None]:
    cleaned = target.strip()
    if not cleaned:
        return None, "目标不能为空"
    if looks_like_thread_id(cleaned):
        config = _remote_adapter_config(cfg, app_server_url)
        adapter = CodexAppServerAdapter(config)
        try:
            return adapter.read_thread(cleaned, include_turns=False).summary, None
        except Exception as exc:
            return None, f"未找到匹配的线程：`{cleaned}` ({exc})"
        finally:
            adapter.stop()
    try:
        thread = resolve_resume_name_via_remote_backend(
            base_config=_remote_adapter_config(cfg, app_server_url),
            app_server_url=app_server_url,
            query_limit=int(cfg.get("thread_list_query_limit", 100)),
            target=cleaned,
        )
    except Exception as exc:
        return None, str(exc)
    return thread, None


def _handle_rm_command(cfg: dict, app_server_url: str, user_args: list[str]) -> int:
    if len(user_args) != 2:
        print("用法：fcodex /rm <thread_id|thread_name>", file=sys.stderr)
        return 2
    thread, error = _resolve_thread_target_via_remote_backend(cfg, app_server_url, user_args[1])
    if not thread:
        print(f"归档线程失败：{error}", file=sys.stderr)
        return 2
    config = _remote_adapter_config(cfg, app_server_url)
    adapter = CodexAppServerAdapter(config)
    try:
        adapter.archive_thread(thread.thread_id)
    except Exception as exc:
        print(f"归档线程失败：{exc}", file=sys.stderr)
        return 2
    finally:
        adapter.stop()
    print(f"已归档线程：`{thread.thread_id[:8]}…` {thread.title}")
    print("说明：这里调用的是 Codex 的线程归档（archive），会从常规列表中隐藏，不是硬删除。")
    return 0


def _resolve_wrapper_resume_args(cfg: dict, app_server_url: str, user_args: list[str]) -> list[str] | None:
    if not user_args or user_args[0] != "/resume":
        return None

    if len(user_args) != 2:
        print("用法：fcodex /resume <thread_id|thread_name>", file=sys.stderr)
        raise SystemExit(2)

    target = user_args[1].strip()
    if not target:
        print("用法：fcodex /resume <thread_id|thread_name>", file=sys.stderr)
        raise SystemExit(2)
    if looks_like_thread_id(target):
        return ["resume", target]

    thread = resolve_resume_name_via_remote_backend(
        base_config=_remote_adapter_config(cfg, app_server_url),
        app_server_url=app_server_url,
        query_limit=int(cfg.get("thread_list_query_limit", 100)),
        target=target,
    )
    return ["resume", thread.thread_id]


def _extract_option_value(user_args: list[str], names: tuple[str, ...]) -> str:
    i = 0
    while i < len(user_args):
        arg = user_args[i]
        for name in names:
            if arg == name:
                return user_args[i + 1] if i + 1 < len(user_args) else ""
            prefix = f"{name}="
            if arg.startswith(prefix):
                return arg[len(prefix) :]
        if arg.split("=", 1)[0] in _OPTIONS_WITH_VALUE and "=" not in arg:
            i += 2
            continue
        i += 1
    return ""


def _resolve_effective_cwd(user_args: list[str]) -> str:
    raw = _extract_option_value(user_args, ("-C", "--cd")).strip()
    if not raw:
        return os.getcwd()
    return os.path.abspath(os.path.expanduser(raw))


def _read_subprocess_ready_line(process: subprocess.Popen[str], timeout_seconds: float = 5.0) -> str:
    if process.stdout is None:
        raise RuntimeError("proxy stdout unavailable")

    result: dict[str, object] = {}

    def _reader() -> None:
        try:
            result["line"] = process.stdout.readline()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = exc

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError("proxy readiness timeout")
    error = result.get("error")
    if isinstance(error, Exception):
        raise error
    return str(result.get("line", ""))


def _launch_local_cwd_proxy(backend_url: str, effective_cwd: str) -> tuple[str, subprocess.Popen[str]]:
    cmd = [
        sys.executable,
        "-m",
        "bot.fcodex_proxy",
        "--backend-url",
        backend_url,
        "--cwd",
        effective_cwd,
        "--parent-pid",
        str(os.getpid()),
    ]
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )
    try:
        ready_line = _read_subprocess_ready_line(process).strip()
        if ready_line:
            return ready_line, process
        exit_code = process.poll()
        if exit_code is None:
            raise RuntimeError("proxy did not report listen url")
        raise RuntimeError(f"proxy exited before ready (exit={exit_code})")
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        raise


def _print_wrapper_usage() -> None:
    print("用法：", file=sys.stderr)
    print("  fcodex /help", file=sys.stderr)
    print("  fcodex /profile [profile_name]", file=sys.stderr)
    print("  fcodex /rm <thread_id|thread_name>", file=sys.stderr)
    print("  fcodex /session [cwd|global]", file=sys.stderr)
    print("  fcodex /resume <thread_id|thread_name>", file=sys.stderr)
    print("说明：以上 wrapper 自命令都必须单独使用。", file=sys.stderr)


def _handle_internal_command(
    cfg: dict,
    app_server_url: str,
    user_args: list[str],
    *,
    data_dir: pathlib.Path,
) -> int | list[str] | None:
    if not user_args:
        return None

    first_known_wrapper_idx = next((idx for idx, arg in enumerate(user_args) if arg in _WRAPPER_COMMANDS), -1)
    if first_known_wrapper_idx > 0:
        print("fcodex wrapper 自命令必须单独使用。", file=sys.stderr)
        _print_wrapper_usage()
        return 2
    if first_known_wrapper_idx < 0:
        if user_args[0].startswith("/"):
            print(f"未知 fcodex 自命令：{user_args[0]}", file=sys.stderr)
            print("说明：shell 层只支持 `/help`、`/profile`、`/rm`、`/session`、`/resume`。", file=sys.stderr)
            print("其他 `/...` 命令请先进入 Codex TUI 再执行。", file=sys.stderr)
            return 2
        return None

    cmd = user_args[0]
    if cmd == "/profile":
        return _handle_profile_command(cfg, app_server_url, user_args, data_dir)
    if cmd == "/rm":
        return _handle_rm_command(cfg, app_server_url, user_args)
    if cmd in {"/session", "/sessions"}:
        scope = "cwd"
        if len(user_args) == 2:
            arg = user_args[1].strip().lower()
            if arg in {"cwd", "current"}:
                scope = "cwd"
            elif arg in {"global", "all"}:
                scope = "global"
            else:
                print("用法：fcodex /session [cwd|global]", file=sys.stderr)
                return 2
        elif len(user_args) > 2:
            print("用法：fcodex /session [cwd|global]", file=sys.stderr)
            return 2
        return _handle_local_list_command(cfg, app_server_url, scope)
    if cmd == "/resume":
        return _resolve_wrapper_resume_args(cfg, app_server_url, user_args)
    if cmd in {"/help", "/help-resume", "/help-session"}:
        if len(user_args) != 1:
            print("用法：fcodex /help", file=sys.stderr)
            return 2
        print("fcodex /help                   查看 wrapper 自命令说明。")
        print("fcodex /profile [name]         查看或切换 feishu-codex / 默认 fcodex 的本地默认 profile。")
        print("fcodex /rm <id|name>           归档线程（archive），从常规列表中隐藏。")
        print("fcodex /session [cwd|global]  列出共享后端线程；默认当前目录，跨 provider。")
        print("fcodex /resume <thread_id|thread_name>  恢复线程；`name` 走全局跨 provider 精确匹配。")
        print("说明：以上 wrapper 自命令必须单独使用，不能与裸 codex 参数混用。")
        print("说明：`fcodex`、`fcodex <prompt>`、`fcodex resume <id>` 仍是 upstream Codex CLI，只是默认连到 feishu-codex shared backend。")
        print("说明：`fcodex /session`、`fcodex /resume <name>` 复用与飞书一致的共享发现逻辑。")
        print("说明：进入 TUI 后，`/help`、`/resume` 等命令恢复 upstream 原样，不等同于 wrapper 命令。")
        print("说明：`fcodex /profile` 只改 feishu-codex / 默认 fcodex 的本地默认 profile；`fcodex -p <profile>` 仍以显式参数为准。")
        return 0
    print(f"未知 fcodex 自命令：{cmd}", file=sys.stderr)
    print("可用：/help  /profile  /rm  /session  /resume", file=sys.stderr)
    return 2


def main() -> None:
    cfg = load_config_file("codex")
    codex_command = str(cfg.get("codex_command", "codex")).strip() or "codex"
    app_server_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL
    user_args = sys.argv[1:]
    data_dir = _default_data_dir()
    handled = _handle_internal_command(cfg, app_server_url, user_args, data_dir=data_dir)
    if isinstance(handled, int):
        raise SystemExit(handled)
    if isinstance(handled, list):
        user_args = handled

    profile_store = ProfileStateStore(data_dir)
    stored_profile = profile_store.load_default_profile()
    resolution = resolve_local_default_profile_via_remote_backend(
        base_config=_remote_adapter_config(cfg, app_server_url),
        app_server_url=app_server_url,
        stored_profile=stored_profile,
    )
    if resolution.stale_profile:
        profile_store.save_default_profile("")
    default_profile = resolution.effective_profile

    argv = [*shlex.split(codex_command)]
    effective_cwd = _resolve_effective_cwd(user_args)
    user_args = _inject_default_profile(user_args, default_profile)
    user_args = _inject_default_cwd(user_args)
    proxy_process: subprocess.Popen[str] | None = None
    if not _has_explicit_remote(user_args):
        try:
            # Upstream Codex TUI omits `cwd` on `thread/start` in `--remote` mode.
            # Without this local proxy, the shared app-server falls back to its own
            # WorkingDirectory (`~/.local/share/feishu-codex`) and fresh `fcodex`
            # sessions don't inherit the caller's shell cwd.
            proxy_url, proxy_process = _launch_local_cwd_proxy(app_server_url, effective_cwd)
        except Exception as exc:
            print(f"启动 fcodex 本地 cwd proxy 失败：{exc}", file=sys.stderr)
            raise SystemExit(2)
        argv.extend(["--remote", proxy_url])
    argv.extend(user_args)
    try:
        os.execvpe(argv[0], argv, os.environ.copy())
    except Exception:
        if proxy_process is not None and proxy_process.poll() is None:
            proxy_process.terminate()
        raise
