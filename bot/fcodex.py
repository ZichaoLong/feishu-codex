"""
fcodex 本地 wrapper。
"""

from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, replace

from bot.adapters.base import ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_config_reader import resolve_profile_from_codex_config
from bot.config import load_config_file
from bot.constants import DEFAULT_APP_SERVER_URL
from bot.env_file import load_env_file
from bot.instance_layout import DEFAULT_INSTANCE_NAME, global_data_dir, validate_instance_name
from bot.instance_resolution import CliRuntimeTarget, current_cli_instance_name, resolve_cli_runtime_target
from bot.platform_paths import default_data_root, is_windows
from bot.profile_resolution import resolve_local_default_profile_via_remote_backend
from bot.session_resolution import (
    looks_like_thread_id,
    resolve_resume_name_via_remote_backend,
)
from bot.stores.profile_state_store import ProfileStateStore
from bot.stores.thread_resume_profile_store import ThreadResumeProfileStore
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseStore
from bot.thread_profile_mutability import check_thread_resume_profile_mutable

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

_REMOVED_WRAPPER_COMMAND_HINTS = {
    "/help": "本地查看/管理请改用 `feishu-codexctl`；进入 TUI 后再使用 upstream `/help`。",
    "/session": "本地看线程请改用 `feishu-codexctl thread list --scope cwd` 或 `feishu-codexctl thread list --scope global`。",
    "/resume": "请改用 `fcodex resume <thread_id|thread_name>`。",
    "/profile": "请改用启动时显式传 `fcodex -p <profile>`。",
    "/rm": "请在飞书侧用 `/rm`，或进入 Codex TUI 后使用 upstream `/rm`。",
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


def _first_positional_index(user_args: list[str], *, start: int = 0) -> int | None:
    i = start
    while i < len(user_args):
        arg = user_args[i]
        if arg == "--":
            return i + 1 if i + 1 < len(user_args) else None
        if not arg.startswith("-") or arg == "-":
            return i
        option_name = arg.split("=", 1)[0]
        if option_name in _OPTIONS_WITH_VALUE and "=" not in arg:
            i += 2
            continue
        i += 1
    return None


def _resume_command_index(user_args: list[str]) -> int | None:
    first_positional_index = _first_positional_index(user_args)
    if first_positional_index is None:
        return None
    if user_args[first_positional_index] != "resume":
        return None
    return first_positional_index


def _resume_target_index(user_args: list[str]) -> int | None:
    resume_index = _resume_command_index(user_args)
    if resume_index is None:
        return None
    return _first_positional_index(user_args, start=resume_index + 1)


def _removed_wrapper_command_error(user_args: list[str]) -> int | None:
    first_positional_index = _first_positional_index(user_args)
    if first_positional_index is None:
        return None
    first_positional = str(user_args[first_positional_index] or "").strip()
    if not first_positional.startswith("/"):
        return None
    print(f"fcodex shell 层不再支持 slash 自命令：`{first_positional}`", file=sys.stderr)
    hint = _REMOVED_WRAPPER_COMMAND_HINTS.get(first_positional)
    if hint:
        print(hint, file=sys.stderr)
    else:
        print("其他 `/...` 命令请先进入 Codex TUI 再执行。", file=sys.stderr)
    return 2


def _lease_owner_instance(thread_id: str) -> str:
    lease = ThreadRuntimeLeaseStore(global_data_dir()).load(thread_id)
    if lease is None:
        return ""
    return lease.owner_instance


def _resolve_runtime_target_for_wrapper(
    *,
    cfg: dict,
    explicit_instance: str,
    thread_id: str = "",
) -> CliRuntimeTarget:
    """Resolve the one shared-backend runtime target for this wrapper launch."""
    configured_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL
    preferred_running_instance = _lease_owner_instance(thread_id) if thread_id else ""
    try:
        return resolve_cli_runtime_target(
            configured_app_server_url=configured_url,
            explicit_instance=explicit_instance or None,
            preferred_running_instance=preferred_running_instance,
            default_instance_data_dir=_default_data_dir(),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


def _inject_default_profile(user_args: list[str], profile: str) -> list[str]:
    if not profile or _has_explicit_profile(user_args):
        return list(user_args)
    return ["--profile", profile, *user_args]


def _inject_default_cwd(user_args: list[str]) -> list[str]:
    if _has_explicit_cwd(user_args):
        return list(user_args)
    return ["--cd", os.getcwd(), *user_args]


def _remote_adapter_config(cfg: dict, app_server_url: str) -> CodexAppServerConfig:
    config = CodexAppServerConfig.from_dict(cfg)
    return replace(config, app_server_mode="remote", app_server_url=app_server_url)


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


def _resolve_resume_target_name(
    cfg: dict,
    explicit_instance: str,
    user_args: list[str],
) -> list[str]:
    target_index = _resume_target_index(user_args)
    if target_index is None:
        return list(user_args)
    target = str(user_args[target_index] or "").strip()
    if not target or looks_like_thread_id(target):
        return list(user_args)
    if _lease_owner_instance(target):
        return list(user_args)
    lookup_target = _resolve_runtime_target_for_wrapper(cfg=cfg, explicit_instance=explicit_instance)
    thread = resolve_resume_name_via_remote_backend(
        base_config=_remote_adapter_config(cfg, lookup_target.app_server_url),
        app_server_url=lookup_target.app_server_url,
        query_limit=int(cfg.get("thread_list_query_limit", 100)),
        target=target,
    )
    resolved = list(user_args)
    resolved[target_index] = thread.thread_id
    return resolved


def _thread_resume_profile_mutable(cfg: dict, app_server_url: str, thread_id: str) -> tuple[bool, str]:
    adapter = CodexAppServerAdapter(_remote_adapter_config(cfg, app_server_url))
    try:
        return check_thread_resume_profile_mutable(
            thread_id,
            unbound_reason="恢复目标不能为空。",
            has_runtime_lease=lambda normalized_thread_id: (
                ThreadRuntimeLeaseStore(global_data_dir()).load(normalized_thread_id) is not None
            ),
            list_loaded_thread_ids=adapter.list_loaded_thread_ids,
        )
    finally:
        adapter.stop()


def _persist_thread_resume_profile_for_resume(
    cfg: dict,
    app_server_url: str,
    thread_id: str,
    profile: str,
) -> None:
    normalized_profile = str(profile or "").strip()
    if not normalized_profile:
        return
    runtime_provider = ""
    adapter = CodexAppServerAdapter(_remote_adapter_config(cfg, app_server_url))
    try:
        runtime_config = adapter.read_runtime_config()
        for item in runtime_config.profiles:
            if item.name == normalized_profile:
                runtime_provider = str(item.model_provider or "").strip()
                break
    except Exception:
        runtime_provider = ""
    finally:
        adapter.stop()
    resolved = resolve_profile_from_codex_config(normalized_profile)
    ThreadResumeProfileStore(global_data_dir()).save(
        thread_id,
        profile=normalized_profile,
        model=resolved.model,
        model_provider=resolved.model_provider or runtime_provider,
    )


def _inject_saved_thread_resume_profile_if_needed(user_args: list[str], thread_id: str) -> list[str]:
    if _has_explicit_profile(user_args):
        return list(user_args)
    record = ThreadResumeProfileStore(global_data_dir()).load(thread_id)
    if record is None or not record.profile:
        return list(user_args)
    return _inject_default_profile(user_args, record.profile)


@dataclass(frozen=True, slots=True)
class _WrapperProfileLaunchPlan:
    user_args: list[str]
    thread_profile_seed: str = ""


def _build_wrapper_profile_launch_plan(
    *,
    cfg: dict,
    app_server_url: str,
    user_args: list[str],
    default_profile: str,
) -> _WrapperProfileLaunchPlan:
    """Plan wrapper-side profile behavior before launching upstream Codex.

    Wrapper responsibilities stop at:

    - existing-thread resume profile read/write
    - deciding whether this launch carries a one-time new-thread seed

    The proxy later owns persisting that seed once the first real `thread_id`
    is returned by `thread/start`.
    """
    planned_args = list(user_args)
    resume_thread_id = _thread_target_hint(planned_args)
    explicit_profile = _extract_option_value(planned_args, ("-p", "--profile")).strip()
    if resume_thread_id:
        if explicit_profile:
            can_write, deny_reason = _thread_resume_profile_mutable(cfg, app_server_url, resume_thread_id)
            if not can_write:
                print(deny_reason, file=sys.stderr)
                raise SystemExit(2)
            _persist_thread_resume_profile_for_resume(
                cfg,
                app_server_url,
                resume_thread_id,
                explicit_profile,
            )
            return _WrapperProfileLaunchPlan(user_args=planned_args)
        return _WrapperProfileLaunchPlan(
            user_args=_inject_saved_thread_resume_profile_if_needed(planned_args, resume_thread_id),
        )
    if explicit_profile:
        return _WrapperProfileLaunchPlan(
            user_args=planned_args,
            thread_profile_seed=explicit_profile,
        )
    return _WrapperProfileLaunchPlan(
        user_args=_inject_default_profile(planned_args, default_profile),
    )


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
    target_index = _resume_target_index(user_args)
    if target_index is not None:
        target = str(user_args[target_index] or "").strip()
        if looks_like_thread_id(target):
            return target
        if _lease_owner_instance(target):
            return target
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
    thread_profile_seed: str = "",
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
        "--thread-profile-seed",
        thread_profile_seed,
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


def _stop_child_process(process: subprocess.Popen[str] | None, *, timeout_seconds: float = 1.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_upstream_codex(
    argv: list[str],
    env: dict[str, str],
    *,
    proxy_process: subprocess.Popen[str] | None,
) -> int | None:
    if not is_windows():
        try:
            os.execvpe(argv[0], argv, env)
        except Exception:
            _stop_child_process(proxy_process)
            raise
        return None

    codex_process = subprocess.Popen(argv, env=env)
    try:
        return codex_process.wait()
    except BaseException:
        _stop_child_process(codex_process)
        raise
    finally:
        _stop_child_process(proxy_process)


def main() -> None:
    load_env_file()
    cfg = load_config_file("codex")
    codex_command = str(cfg.get("codex_command", "codex")).strip() or "codex"
    explicit_instance, user_args = _consume_instance_arg(sys.argv[1:])
    if "--dry-run" in user_args:
        print("fcodex 不再提供 `--dry-run` wrapper 入口。", file=sys.stderr)
        print("本地查看线程请改用 `feishu-codexctl thread list`、`thread status`、`thread bindings`。", file=sys.stderr)
        raise SystemExit(2)
    if explicit_instance and _has_explicit_remote(user_args):
        print("`--instance` 不能与显式 `--remote` 同时使用。", file=sys.stderr)
        raise SystemExit(2)
    removed_wrapper_error = _removed_wrapper_command_error(user_args)
    if removed_wrapper_error is not None:
        raise SystemExit(removed_wrapper_error)

    preprocessed_args = list(user_args)
    if not _has_explicit_remote(preprocessed_args):
        preprocessed_args = _resolve_resume_target_name(cfg, explicit_instance, preprocessed_args)

    if _has_explicit_remote(preprocessed_args):
        data_dir = _default_data_dir()
        app_server_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL
        resolved_target = CliRuntimeTarget(
            instance_name=current_cli_instance_name(),
            data_dir=data_dir,
            app_server_url=app_server_url,
        )
    else:
        thread_target = _thread_target_hint(preprocessed_args)
        resolved_target = _resolve_runtime_target_for_wrapper(
            cfg=cfg,
            explicit_instance=explicit_instance,
            thread_id=thread_target,
        )
        data_dir = resolved_target.data_dir
        app_server_url = resolved_target.app_server_url

    user_args = list(preprocessed_args)

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
    thread_profile_seed = ""
    if not _has_explicit_remote(user_args):
        profile_launch_plan = _build_wrapper_profile_launch_plan(
            cfg=cfg,
            app_server_url=app_server_url,
            user_args=user_args,
            default_profile=default_profile,
        )
        user_args = list(profile_launch_plan.user_args)
        thread_profile_seed = profile_launch_plan.thread_profile_seed
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
                thread_profile_seed=thread_profile_seed,
                **proxy_kwargs,
            )
        except Exception as exc:
            print(f"启动 fcodex 本地 cwd proxy 失败：{exc}", file=sys.stderr)
            raise SystemExit(2)
        argv.extend(["--remote", proxy_url])
    argv.extend(user_args)
    env = os.environ.copy()
    env["FC_DATA_DIR"] = str(data_dir)
    env["FC_INSTANCE"] = resolved_target.instance_name
    exit_code = _run_upstream_codex(argv, env, proxy_process=proxy_process)
    if exit_code is not None:
        raise SystemExit(exit_code)
    return


if __name__ == "__main__":
    main()
