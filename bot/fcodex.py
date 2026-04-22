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
from dataclasses import dataclass, replace

from bot.adapters.base import ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.config import load_config_file
from bot.constants import DEFAULT_APP_SERVER_URL, display_path
from bot.env_file import load_env_file
from bot.instance_layout import DEFAULT_INSTANCE_NAME, global_data_dir, resolve_instance_paths, validate_instance_name
from bot.instance_resolution import current_cli_instance_name, default_running_instance, list_running_instances, unique_running_instance
from bot.platform_paths import default_data_root
from bot.profile_resolution import resolve_local_default_profile_via_remote_backend
from bot.session_resolution import (
    list_current_dir_threads,
    list_global_threads,
    looks_like_thread_id,
    resolve_resume_name_via_remote_backend,
)
from bot.service_control_plane import ServiceControlError, control_request
from bot.shared_command_surface import (
    format_shared_wrapper_command_names,
    get_shared_command,
    shared_wrapper_commands,
    shared_wrapper_help_lines,
    shared_wrapper_usage_lines,
)
from bot.stores.app_server_runtime_store import resolve_effective_app_server_url
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.profile_state_store import ProfileStateStore
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseStore

_WRAPPER_COMMANDS = shared_wrapper_commands()
_HELP_COMMAND = get_shared_command("help")
_PROFILE_COMMAND = get_shared_command("profile")
_RM_COMMAND = get_shared_command("rm")
_SESSION_COMMAND = get_shared_command("session")
_RESUME_COMMAND = get_shared_command("resume")

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


@dataclass(frozen=True, slots=True)
class _ResolvedInstanceTarget:
    instance_name: str
    data_dir: pathlib.Path
    app_server_url: str
    service_token: str = ""


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


def _default_data_dir() -> pathlib.Path:
    raw = os.environ.get("FC_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_data_root()


def _consume_instance_arg(user_args: list[str]) -> tuple[str, list[str]]:
    if not user_args:
        return "", []
    first = user_args[0]
    if first == "--instance":
        if len(user_args) < 2:
            print("`--instance` 缺少实例名。", file=sys.stderr)
            raise SystemExit(2)
        return validate_instance_name(user_args[1]), user_args[2:]
    if first.startswith("--instance="):
        return validate_instance_name(first.split("=", 1)[1]), user_args[1:]
    return "", list(user_args)


def _consume_dry_run_arg(user_args: list[str]) -> tuple[bool, list[str]]:
    matches = [idx for idx, arg in enumerate(user_args) if arg == "--dry-run"]
    if not matches:
        return False, list(user_args)
    if len(matches) > 1:
        print("`--dry-run` 只能出现一次。", file=sys.stderr)
        raise SystemExit(2)
    if matches[0] != 0:
        print("`--dry-run` 必须放在 fcodex 自命令之前。", file=sys.stderr)
        print("示例：`fcodex --dry-run /resume <thread_id|thread_name>`", file=sys.stderr)
        raise SystemExit(2)
    return True, user_args[1:]


def _running_instance_entries() -> list[InstanceRegistryEntry]:
    return list_running_instances()


def _running_instance_by_name(instance_name: str) -> InstanceRegistryEntry | None:
    normalized = validate_instance_name(instance_name)
    return next((item for item in _running_instance_entries() if item.instance_name == normalized), None)


def _pick_lookup_instance_entry(explicit_instance: str) -> InstanceRegistryEntry | None:
    if explicit_instance:
        return _running_instance_by_name(explicit_instance)
    unique = unique_running_instance()
    if unique is not None:
        return unique
    default = default_running_instance()
    if default is not None:
        return default
    running = _running_instance_entries()
    if running:
        return sorted(running, key=lambda item: item.instance_name)[0]
    return None


def _lease_owner_instance(thread_id: str) -> str:
    lease = ThreadRuntimeLeaseStore(global_data_dir()).load(thread_id)
    if lease is None:
        return ""
    return lease.owner_instance


def _resolve_instance_target(
    *,
    cfg: dict,
    explicit_instance: str,
    thread_id: str = "",
) -> _ResolvedInstanceTarget:
    configured_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL
    if explicit_instance:
        paths = resolve_instance_paths(explicit_instance)
        running = _running_instance_by_name(explicit_instance)
        if running is not None:
            return _ResolvedInstanceTarget(
                instance_name=running.instance_name,
                data_dir=pathlib.Path(running.data_dir),
                app_server_url=running.app_server_url or resolve_effective_app_server_url(configured_url, data_dir=paths.data_dir),
                service_token=running.service_token,
            )
        return _ResolvedInstanceTarget(
            instance_name=paths.instance_name,
            data_dir=paths.data_dir,
            app_server_url=resolve_effective_app_server_url(configured_url, data_dir=paths.data_dir),
        )

    owner_instance = _lease_owner_instance(thread_id) if thread_id else ""
    if owner_instance:
        running = _running_instance_by_name(owner_instance)
        if running is not None:
            return _ResolvedInstanceTarget(
                instance_name=running.instance_name,
                data_dir=pathlib.Path(running.data_dir),
                app_server_url=running.app_server_url or resolve_effective_app_server_url(configured_url, data_dir=pathlib.Path(running.data_dir)),
                service_token=running.service_token,
            )

    unique = unique_running_instance()
    if unique is not None:
        return _ResolvedInstanceTarget(
            instance_name=unique.instance_name,
            data_dir=pathlib.Path(unique.data_dir),
            app_server_url=unique.app_server_url or resolve_effective_app_server_url(configured_url, data_dir=pathlib.Path(unique.data_dir)),
            service_token=unique.service_token,
        )

    default = default_running_instance()
    if default is not None:
        return _ResolvedInstanceTarget(
            instance_name=default.instance_name,
            data_dir=pathlib.Path(default.data_dir),
            app_server_url=default.app_server_url or resolve_effective_app_server_url(configured_url, data_dir=pathlib.Path(default.data_dir)),
            service_token=default.service_token,
        )

    running = _running_instance_entries()
    if len(running) > 1:
        print("检测到多个运行中的实例，请显式传 `--instance <name>`。", file=sys.stderr)
        raise SystemExit(2)

    if not running:
        current_instance = current_cli_instance_name()
        paths = resolve_instance_paths(current_instance)
        return _ResolvedInstanceTarget(
            instance_name=paths.instance_name,
            data_dir=paths.data_dir if paths.instance_name != DEFAULT_INSTANCE_NAME else _default_data_dir(),
            app_server_url=resolve_effective_app_server_url(
                configured_url,
                data_dir=paths.data_dir if paths.instance_name != DEFAULT_INSTANCE_NAME else _default_data_dir(),
            ),
        )

    only = running[0]
    return _ResolvedInstanceTarget(
        instance_name=only.instance_name,
        data_dir=pathlib.Path(only.data_dir),
        app_server_url=only.app_server_url or resolve_effective_app_server_url(configured_url, data_dir=pathlib.Path(only.data_dir)),
        service_token=only.service_token,
    )


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
            print(f"用法：{_PROFILE_COMMAND.wrapper_usage}", file=sys.stderr)
            return 2
        if len(user_args) == 1:
            lines, _ = _runtime_profile_summary(adapter, profile_store, config, app_server_url)
            print("\n".join(lines))
            return 0

        target_profile = user_args[1].strip()
        if not target_profile:
            print(f"用法：{_PROFILE_COMMAND.wrapper_usage}", file=sys.stderr)
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


def _parse_session_scope(user_args: list[str]) -> str | None:
    scope = "cwd"
    if len(user_args) == 2:
        arg = user_args[1].strip().lower()
        if arg == "cwd":
            scope = "cwd"
        elif arg == "global":
            scope = "global"
        else:
            return None
    elif len(user_args) > 2:
        return None
    return scope


def _print_dry_run_target_header(command: str, target: _ResolvedInstanceTarget) -> None:
    print(f"dry-run: fcodex {command}")
    print(f"instance: {target.instance_name}")
    print(f"data dir: {display_path(str(target.data_dir))}")
    print(f"app server: {target.app_server_url}")
    print("read-only: yes")


def _resume_dry_run_lease_lines(thread_id: str, target: _ResolvedInstanceTarget) -> list[str]:
    lease = ThreadRuntimeLeaseStore(global_data_dir()).load(thread_id)
    if lease is None:
        return ["thread runtime lease: no live owner recorded"]
    if lease.owner_instance == target.instance_name:
        return [
            f"thread runtime lease: owned by target instance `{lease.owner_instance}`",
            f"lease holders: {len(lease.holders)}",
        ]
    owner = _running_instance_by_name(lease.owner_instance)
    if owner is None:
        return [
            f"thread runtime lease: stale owner `{lease.owner_instance}` is not currently registered",
            "transfer check: real run may purge the stale lease before acquiring",
        ]
    if owner.service_token != lease.owner_service_token:
        return [
            f"thread runtime lease: stale owner token for `{lease.owner_instance}`",
            "transfer check: real run may purge the stale lease before acquiring",
        ]
    try:
        status = control_request(pathlib.Path(owner.data_dir), "thread/status", {"thread_id": thread_id})
    except ServiceControlError as exc:
        return [
            f"thread runtime lease: blocked by owner instance `{lease.owner_instance}`",
            f"transfer check: cannot query owner status ({exc})",
        ]
    if bool(status.get("release_feishu_runtime_available")):
        return [
            f"thread runtime lease: transferable from owner instance `{lease.owner_instance}`",
            "transfer check: owner reports release-feishu-runtime available",
        ]
    reason_code = str(status.get("release_feishu_runtime_reason_code", "") or "").strip()
    reason = str(status.get("release_feishu_runtime_reason", "") or "").strip()
    suffix = f" ({reason_code})" if reason_code else ""
    lines = [f"thread runtime lease: blocked by owner instance `{lease.owner_instance}`{suffix}"]
    if reason:
        lines.append(f"transfer reason: {reason}")
    return lines


def _handle_session_dry_run(cfg: dict, explicit_instance: str, user_args: list[str]) -> int:
    scope = _parse_session_scope(user_args)
    if scope is None:
        print(f"用法：{_SESSION_COMMAND.wrapper_usage}", file=sys.stderr)
        return 2
    target = _resolve_instance_target(cfg=cfg, explicit_instance=explicit_instance)
    session_command = "/session" if scope == "cwd" else "/session global"
    _print_dry_run_target_header(session_command, target)
    print(f"scope: {scope}")
    if scope == "cwd":
        print(f"working dir: {display_path(os.getcwd())}")
    print("thread admission: not consulted by fcodex wrapper discovery")
    print("would start TUI: no")
    print("")
    return _handle_local_list_command(cfg, target.app_server_url, scope)


def _handle_resume_dry_run(cfg: dict, explicit_instance: str, user_args: list[str]) -> int:
    if len(user_args) != 2:
        print(f"用法：{_RESUME_COMMAND.wrapper_usage}", file=sys.stderr)
        return 2
    raw_target = user_args[1].strip()
    if not raw_target:
        print(f"用法：{_RESUME_COMMAND.wrapper_usage}", file=sys.stderr)
        return 2
    lookup_target = _resolve_instance_target(cfg=cfg, explicit_instance=explicit_instance)
    thread, error = _resolve_thread_target_via_remote_backend(cfg, lookup_target.app_server_url, raw_target)
    if thread is None:
        print(f"dry-run resume failed: {error}", file=sys.stderr)
        return 2
    target = _resolve_instance_target(
        cfg=cfg,
        explicit_instance=explicit_instance,
        thread_id=thread.thread_id,
    )
    profile_store = ProfileStateStore(target.data_dir)
    profile_resolution = resolve_local_default_profile_via_remote_backend(
        base_config=_remote_adapter_config(cfg, target.app_server_url),
        app_server_url=target.app_server_url,
        stored_profile=profile_store.load_default_profile(),
    )

    _print_dry_run_target_header("/resume", target)
    if target.instance_name != lookup_target.instance_name:
        print(
            "routing note: target instance differs from lookup instance because the thread has a live runtime owner."
        )
        print(f"lookup instance: {lookup_target.instance_name}")
    print(f"target: {raw_target}")
    print(f"resolved thread: {thread.thread_id} {thread.title}".rstrip())
    print(f"thread cwd: {display_path(thread.cwd)}")
    print(f"backend thread status: {thread.status or 'unknown'}")
    print(f"default profile: {profile_resolution.effective_profile or '（未设置）'}")
    if profile_resolution.stale_profile:
        print(
            "stale profile: "
            f"{profile_resolution.stale_profile} (real run will clear it; dry-run does not mutate local state)"
        )
    print("thread admission: not consulted by fcodex wrapper resume")
    print(f"would pass to upstream: resume {thread.thread_id}")
    print("would start TUI: no")
    for line in _resume_dry_run_lease_lines(thread.thread_id, target):
        print(line)
    return 0


def _handle_dry_run_command(cfg: dict, explicit_instance: str, user_args: list[str]) -> int:
    if not user_args:
        print(
            "用法：`fcodex --dry-run /session [cwd|global]` 或 "
            "`fcodex --dry-run /resume <thread_id|thread_name>`",
            file=sys.stderr,
        )
        return 2
    cmd = user_args[0]
    if cmd == "/session":
        return _handle_session_dry_run(cfg, explicit_instance, user_args)
    if cmd == "/resume":
        return _handle_resume_dry_run(cfg, explicit_instance, user_args)
    print("`--dry-run` 目前只支持 fcodex wrapper 自命令 `/session` 与 `/resume`。", file=sys.stderr)
    print("说明：裸 `fcodex resume <id>` 仍是 upstream Codex CLI 行为，不属于 wrapper dry-run surface。", file=sys.stderr)
    return 2


def _handle_rm_command(cfg: dict, app_server_url: str, user_args: list[str], data_dir: pathlib.Path) -> int:
    if len(user_args) != 2:
        print(f"用法：{_RM_COMMAND.wrapper_usage}", file=sys.stderr)
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
        print(f"用法：{_RESUME_COMMAND.wrapper_usage}", file=sys.stderr)
        raise SystemExit(2)

    target = user_args[1].strip()
    if not target:
        print(f"用法：{_RESUME_COMMAND.wrapper_usage}", file=sys.stderr)
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


def _thread_target_hint(user_args: list[str]) -> str:
    if len(user_args) >= 2 and user_args[0] == "resume":
        return user_args[1].strip()
    if len(user_args) >= 2 and user_args[0] == "/resume":
        target = user_args[1].strip()
        return target if looks_like_thread_id(target) else ""
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


def _launch_local_cwd_proxy(
    backend_url: str,
    effective_cwd: str,
    data_dir: pathlib.Path,
    *,
    instance_name: str = DEFAULT_INSTANCE_NAME,
    service_token: str = "",
) -> tuple[str, subprocess.Popen[str]]:
    cmd = [
        sys.executable,
        "-m",
        "bot.fcodex_proxy",
        "--backend-url",
        backend_url,
        "--cwd",
        effective_cwd,
        "--data-dir",
        str(data_dir),
        "--instance",
        instance_name,
        "--global-data-dir",
        str(global_data_dir()),
        "--service-token",
        service_token,
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
    for usage in shared_wrapper_usage_lines():
        print(f"  {usage}", file=sys.stderr)
        print(f"  --instance <name> {usage[len('fcodex '):]}", file=sys.stderr)
    print("  fcodex --dry-run /session [cwd|global]", file=sys.stderr)
    print("  fcodex --dry-run /resume <thread_id|thread_name>", file=sys.stderr)
    print("  --instance <name> --dry-run /session [cwd|global]", file=sys.stderr)
    print("  --instance <name> --dry-run /resume <thread_id|thread_name>", file=sys.stderr)
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
            print(f"说明：shell 层只支持 {format_shared_wrapper_command_names()}。", file=sys.stderr)
            print("其他 `/...` 命令请先进入 Codex TUI 再执行。", file=sys.stderr)
            return 2
        return None

    cmd = user_args[0]
    if cmd == "/profile":
        return _handle_profile_command(cfg, app_server_url, user_args, data_dir)
    if cmd == "/rm":
        return _handle_rm_command(cfg, app_server_url, user_args, data_dir)
    if cmd == "/session":
        scope = _parse_session_scope(user_args)
        if scope is None:
            print(f"用法：{_SESSION_COMMAND.wrapper_usage}", file=sys.stderr)
            return 2
        return _handle_local_list_command(cfg, app_server_url, scope)
    if cmd == "/resume":
        return _resolve_wrapper_resume_args(cfg, app_server_url, user_args)
    if cmd == "/help":
        if len(user_args) != 1:
            print(f"用法：{_HELP_COMMAND.wrapper_usage}", file=sys.stderr)
            return 2
        for line in shared_wrapper_help_lines():
            print(line)
        print("说明：以上 wrapper 自命令必须单独使用，不能与裸 codex 参数混用。")
        print("说明：多实例场景可在最前面加 `--instance <name>`；歧义时会要求显式指定。")
        print("说明：`fcodex`、`fcodex <prompt>`、`fcodex resume <id>` 仍是 upstream Codex CLI，只是默认连到 feishu-codex shared backend。")
        print("说明：`fcodex /session`、`fcodex /resume <name>` 复用与飞书一致的共享发现逻辑。")
        print(
            "说明：`fcodex --dry-run /session` 与 "
            "`fcodex --dry-run /resume <thread_id|thread_name>` 只做只读预检，不启动 TUI。"
        )
        print("说明：进入 TUI 后，`/help`、`/resume` 等命令恢复 upstream 原样，不等同于 wrapper 命令。")
        print("说明：`fcodex /profile` 只改 feishu-codex / 默认 fcodex 的本地默认 profile；`fcodex -p <profile>` 仍以显式参数为准。")
        return 0
    print(f"未知 fcodex 自命令：{cmd}", file=sys.stderr)
    print(f"可用：{format_shared_wrapper_command_names()}", file=sys.stderr)
    return 2


def main() -> None:
    load_env_file()
    cfg = load_config_file("codex")
    codex_command = str(cfg.get("codex_command", "codex")).strip() or "codex"
    explicit_instance, user_args = _consume_instance_arg(sys.argv[1:])
    dry_run, user_args = _consume_dry_run_arg(user_args)
    if explicit_instance and _has_explicit_remote(user_args):
        print("`--instance` 不能与显式 `--remote` 同时使用。", file=sys.stderr)
        raise SystemExit(2)
    if dry_run:
        if _has_explicit_remote(user_args):
            print("`--dry-run` 不能与显式 `--remote` 同时使用。", file=sys.stderr)
            raise SystemExit(2)
        raise SystemExit(_handle_dry_run_command(cfg, explicit_instance, user_args))

    preprocessed_args = list(user_args)
    if not _has_explicit_remote(preprocessed_args):
        if len(preprocessed_args) == 2 and preprocessed_args[0] == "/resume" and not looks_like_thread_id(preprocessed_args[1].strip()):
            lookup_target = _resolve_instance_target(cfg=cfg, explicit_instance=explicit_instance)
            preprocessed_args = _resolve_wrapper_resume_args(cfg, lookup_target.app_server_url, preprocessed_args) or preprocessed_args

    if _has_explicit_remote(preprocessed_args):
        data_dir = _default_data_dir()
        app_server_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL
        resolved_target = _ResolvedInstanceTarget(
            instance_name=current_cli_instance_name(),
            data_dir=data_dir,
            app_server_url=app_server_url,
        )
    else:
        thread_target = _thread_target_hint(preprocessed_args)
        resolved_target = _resolve_instance_target(
            cfg=cfg,
            explicit_instance=explicit_instance,
            thread_id=thread_target,
        )
        data_dir = resolved_target.data_dir
        app_server_url = resolved_target.app_server_url

    handled = _handle_internal_command(cfg, app_server_url, preprocessed_args, data_dir=data_dir)
    if isinstance(handled, int):
        raise SystemExit(handled)
    if isinstance(handled, list):
        user_args = handled
    else:
        user_args = preprocessed_args

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
            proxy_kwargs: dict[str, str] = {}
            if resolved_target.instance_name != DEFAULT_INSTANCE_NAME or resolved_target.service_token:
                proxy_kwargs = {
                    "instance_name": resolved_target.instance_name,
                    "service_token": resolved_target.service_token,
                }
            proxy_url, proxy_process = _launch_local_cwd_proxy(
                app_server_url,
                effective_cwd,
                data_dir,
                **proxy_kwargs,
            )
        except Exception as exc:
            print(f"启动 fcodex 本地 cwd proxy 失败：{exc}", file=sys.stderr)
            raise SystemExit(2)
        argv.extend(["--remote", proxy_url])
    argv.extend(user_args)
    try:
        env = os.environ.copy()
        env["FC_DATA_DIR"] = str(data_dir)
        env["FC_INSTANCE"] = resolved_target.instance_name
        os.execvpe(argv[0], argv, env)
    except Exception:
        if proxy_process is not None and proxy_process.poll() is None:
            proxy_process.terminate()
        raise
