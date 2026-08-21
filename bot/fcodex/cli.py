"""FOCUS/fcodex local Codex TUI wrapper."""

from __future__ import annotations

import os
import pathlib
import secrets
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.adapters.base import ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_command_resolver import resolve_managed_codex_command
from bot.codex_config import CodexConfig
from bot.config import load_config_file
from bot.env_file import load_env_file
from bot.instance_layout import DEFAULT_INSTANCE_NAME, global_data_dir, validate_instance_name
from bot.instance_resolution import (
    CliRuntimeTarget,
    current_cli_instance_name,
    list_running_instances,
    resolve_cli_runtime_target,
    resolve_running_instance_app_server_url,
)
from bot.local_websocket_auth import FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR, FOCUS_SERVICE_TOKEN_ENV_VAR
from bot.platform_paths import default_data_root, is_windows
from bot.thread_resolution import (
    looks_like_thread_id,
    resolve_resume_name_via_attached_endpoint,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseStore
from bot.thread_runtime_coordination import preview_thread_global_loaded_gate
from bot.version import __version__

if TYPE_CHECKING:
    from bot.cli_command_schema import CommandSchema


WRAPPER_PASSTHROUGH_OPTIONS_WITH_VALUE = frozenset(
    {
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
)

_REMOVED_WRAPPER_COMMAND_HINTS = {
    "/help": "本地查看/管理请改用 `focusctl`；进入 TUI 后再使用 upstream `/help`。",
    "/threads": "本地看线程请改用 `focusctl thread list --scope cwd` 或 `focusctl thread list --scope global`。",
    "/resume": "请改用 `focus resume <thread_id|thread_name>` 或 `fcodex resume <thread_id|thread_name>`。",
    "/profile": "本项目不再提供 `/profile`；如需使用上游 profile，请在启动时显式传 `focus -p <profile>` 或 `fcodex -p <profile>`。",
    "/archive": "请改用 `focusctl thread archive --thread-id <id>` 或 `--thread-name <name>`；飞书侧仍可用 `/archive`。",
}
_MANAGEMENT_COMMAND_HINTS = {
    "binding": "请改用 `focusctl binding ...`。",
    "config": "请改用 `focusctl config ...`。",
    "image": "请改用 `focusctl image ...`。",
    "instance": "请改用 `focusctl instance ...`。",
    "migrate": "请改用 `focusctl migrate from-feishu-codex`。",
    "prompt": "请改用 `focusctl prompt ...`。",
    "purge": "请改用 `focusctl purge`。",
    "service": "请改用 `focusctl service ...`。",
    "skill": "请改用 `focusctl skill ...`。",
    "thread": "请改用 `focusctl thread ...`。",
    "uninstall": "请改用 `focusctl uninstall`。",
}
WRAPPER_HELP_FLAGS = ("-h", "--help")
WRAPPER_VERSION_FLAGS = ("--version",)
WRAPPER_INSTANCE_OPTION = "--instance"
WRAPPER_RESUME_COMMAND = "resume"
_LOCAL_WEBSOCKET_NO_PROXY_HOSTS = ("127.0.0.1", "localhost", "::1")


def _has_option(user_args: list[str], names: tuple[str, ...]) -> bool:
    for arg in user_args:
        # ``--`` belongs to the wrapper's caller contract: everything after
        # it is an opaque upstream argument vector.  In particular, an
        # upstream ``--cd``/``--remote`` must not be mistaken for a Focus
        # option while we derive proxy routing and safety checks.
        if arg == "--":
            break
        for name in names:
            if arg == name or arg.startswith(f"{name}="):
                return True
    return False


def _wrapper_help_request_kind(user_args: list[str]) -> str | None:
    if not _has_option(user_args, WRAPPER_HELP_FLAGS):
        return None
    first_positional_index = _first_positional_index(user_args)
    if first_positional_index is None:
        return "top"
    if user_args[first_positional_index] == WRAPPER_RESUME_COMMAND:
        return WRAPPER_RESUME_COMMAND
    return None


def _is_wrapper_version_request(user_args: list[str]) -> bool:
    return len(user_args) == 1 and user_args[0] in WRAPPER_VERSION_FLAGS


def wrapper_command_schema(command_name: str) -> CommandSchema:
    """Export the Focus-owned wrapper syntax for shell completion.

    Unknown Codex options remain upstream passthrough.  The passthrough option
    arity comes from the same registry used by production positional parsing,
    so completion cannot independently reinterpret their values as commands.
    """

    from bot.cli_command_schema import CommandOption, CommandSchema

    normalized_name = str(command_name or "").strip()
    if normalized_name not in {"focus", "fcodex"}:
        raise ValueError(f"unsupported wrapper command: {normalized_name}")
    help_option = CommandOption(
        names=WRAPPER_HELP_FLAGS,
        takes_value=False,
        dest="help",
    )
    instance_option = CommandOption(
        names=(WRAPPER_INSTANCE_OPTION,),
        takes_value=True,
        dest="instance",
    )
    return CommandSchema(
        name=normalized_name,
        options=(
            instance_option,
            CommandOption(
                names=WRAPPER_VERSION_FLAGS,
                takes_value=False,
                dest="version",
            ),
            help_option,
        ),
        subcommands=(
            CommandSchema(
                name=WRAPPER_RESUME_COMMAND,
                # Production strips --instance before interpreting the
                # upstream resume grammar, so it remains valid on either side
                # of the subcommand and its positional target.
                options=(instance_option, help_option),
                passthrough_options_with_value=WRAPPER_PASSTHROUGH_OPTIONS_WITH_VALUE,
            ),
        ),
        passthrough_options_with_value=WRAPPER_PASSTHROUGH_OPTIONS_WITH_VALUE,
    )


def _program_name() -> str:
    injected = str(os.environ.get("FOCUS_WRAPPER_COMMAND", "") or "").strip()
    if injected in {"focus", "fcodex"}:
        return injected
    raw = pathlib.Path(sys.argv[0]).name.strip()
    return raw or "focus"


def _print_wrapper_help() -> None:
    command_name = _program_name()
    print(
        f"{command_name} 本地 wrapper（Codex TUI thin wrapper）。\n\n"
        "用法:\n"
        f"  {command_name} [--instance <name>] [upstream codex args ...]\n"
        f"  {command_name} [--instance <name>] resume <thread_id|thread_name> [upstream resume args ...]\n\n"
        "说明:\n"
        "- `focus` 和 `fcodex` 是同一个 Codex TUI thin wrapper；普通参数会继续透传给上游 `codex`\n"
        "- wrapper 会为当前 shell cwd 建立本地 proxy，并把上游 TUI 接到目标实例的 shared backend\n"
        "- `--remote` / `--remote-auth-token-env` 由 wrapper 内部保留；不接受用户指定外部 app-server\n"
        "- `--instance <name>` 只接受已创建的命名实例\n"
        "- 独立的 `--` 及其后的参数会原样透传给上游，不参与 wrapper 解析\n"
        "- `resume <thread>` 会先做实例路由、thread 名解析，以及跨实例 loaded gate 检查\n"
        "- shell 层不再支持 `/threads`、`/resume`、`/profile` 这类 slash 自命令\n\n"
        "常用入口:\n"
        f"  {command_name}\n"
        f"  {command_name} resume <thread_id|thread_name>\n"
        f"  {command_name} --instance corp-a\n"
        f"  {command_name} -p <profile>\n\n"
        "更多帮助:\n"
        "  - 上游 Codex 通用参数: `codex --help`\n"
        "  - 本地诊断 / 管理: `focusctl --help`\n"
    )


def _print_wrapper_resume_help() -> None:
    command_name = _program_name()
    print(
        f"{command_name} resume 本地 wrapper 语义。\n\n"
        "用法:\n"
        f"  {command_name} [--instance <name>] resume <thread_id|thread_name> [upstream resume args ...]\n\n"
        "说明:\n"
        "- `thread_name` 会先在选中的 shared backend 上解析成唯一 thread id；歧义时直接拒绝\n"
        "- 若已有 live runtime owner，wrapper 会优先路由到 owner 实例\n"
        "- 若其他运行中实例仍报告该 thread 处于 loaded，wrapper 会 fail-close 拒绝，而不是偷偷 cold resume\n"
        "- 通过这些检查后，剩余参数仍透传给上游 `codex resume`\n\n"
        "相关入口:\n"
        "  - 本地线程查看: `focusctl thread list --scope cwd`\n"
        "  - 本地 thread 诊断: `focusctl thread status --thread-id <id>`\n"
        "  - 上游 resume 子命令帮助: `codex resume --help`\n"
    )


def _prepend_no_proxy_hosts(env: dict[str, str], hosts: tuple[str, ...] = _LOCAL_WEBSOCKET_NO_PROXY_HOSTS) -> None:
    existing_items: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        existing_items.extend(item.strip() for item in str(env.get(key, "") or "").split(",") if item.strip())

    seen: set[str] = set()
    merged: list[str] = []
    for item in [*hosts, *existing_items]:
        normalized = item.strip()
        if not normalized:
            continue
        dedupe_key = normalized.lower()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged.append(normalized)

    value = ",".join(merged)
    env["NO_PROXY"] = value
    env["no_proxy"] = value


def _has_explicit_remote(user_args: list[str]) -> bool:
    return _has_option(user_args, ("--remote",))


def _has_explicit_remote_auth_token_env(user_args: list[str]) -> bool:
    return _has_option(user_args, ("--remote-auth-token-env",))


def _has_explicit_cwd(user_args: list[str]) -> bool:
    return _has_option(user_args, ("-C", "--cd"))


def _default_data_dir() -> pathlib.Path:
    raw = os.environ.get("FOCUS_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_data_root()


def _consume_instance_arg(user_args: list[str]) -> tuple[str, list[str]]:
    explicit_instance = ""
    remaining: list[str] = []
    i = 0
    while i < len(user_args):
        arg = user_args[i]
        if arg == "--":
            remaining.extend(user_args[i:])
            break
        if arg == WRAPPER_INSTANCE_OPTION:
            if i + 1 >= len(user_args):
                print("`--instance` 缺少实例名。", file=sys.stderr)
                raise SystemExit(2)
            instance_name = validate_instance_name(user_args[i + 1])
            if explicit_instance:
                print("`--instance` 只能传一次。", file=sys.stderr)
                raise SystemExit(2)
            explicit_instance = instance_name
            i += 2
            continue
        if arg.startswith(f"{WRAPPER_INSTANCE_OPTION}="):
            instance_name = validate_instance_name(arg.split("=", 1)[1])
            if explicit_instance:
                print("`--instance` 只能传一次。", file=sys.stderr)
                raise SystemExit(2)
            explicit_instance = instance_name
            i += 1
            continue
        remaining.append(arg)
        i += 1
    return explicit_instance, remaining


def _first_positional_index(user_args: list[str], *, start: int = 0) -> int | None:
    i = start
    while i < len(user_args):
        arg = user_args[i]
        if arg == "--":
            # The terminator ends Focus-owned parsing.  Do not inspect the
            # opaque upstream tail for wrapper commands or resume targets.
            return None
        if not arg.startswith("-") or arg == "-":
            return i
        option_name = arg.split("=", 1)[0]
        if option_name in WRAPPER_PASSTHROUGH_OPTIONS_WITH_VALUE and "=" not in arg:
            i += 2
            continue
        i += 1
    return None


def _resume_command_index(user_args: list[str]) -> int | None:
    first_positional_index = _first_positional_index(user_args)
    if first_positional_index is None:
        return None
    if user_args[first_positional_index] != WRAPPER_RESUME_COMMAND:
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
    print(f"{_program_name()} shell 层不再支持 slash 自命令：`{first_positional}`", file=sys.stderr)
    hint = _REMOVED_WRAPPER_COMMAND_HINTS.get(first_positional)
    if hint:
        print(hint, file=sys.stderr)
    else:
        print("其他 `/...` 命令请先进入 Codex TUI 再执行。", file=sys.stderr)
    return 2


def _management_command_error(user_args: list[str]) -> int | None:
    first_positional_index = _first_positional_index(user_args)
    if first_positional_index is None:
        return None
    first_positional = str(user_args[first_positional_index] or "").strip()
    hint = _MANAGEMENT_COMMAND_HINTS.get(first_positional)
    if not hint:
        return None
    print(
        f"{_program_name()} 是 Codex TUI thin wrapper，不处理本地管理命令：`{first_positional}`",
        file=sys.stderr,
    )
    print(hint, file=sys.stderr)
    return 2


def _lease_owner_instance(thread_id: str) -> str:
    lease = ThreadRuntimeLeaseStore(global_data_dir()).load(thread_id)
    if lease is None:
        return ""
    return lease.owner_instance


def _preferred_resume_instance_for_thread(thread_id: str, *, explicit_instance: str = "") -> str:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return ""
    owner_instance = _lease_owner_instance(normalized_thread_id)
    normalized_explicit = str(explicit_instance or "").strip()
    if normalized_explicit and owner_instance and owner_instance != normalized_explicit:
        raise ValueError(
            f"目标 thread 当前的 live runtime owner 是 `{owner_instance}`；"
            f"不能显式传 `--instance {normalized_explicit}`。"
            f"请改用 `--instance {owner_instance}`，或先让该 thread 完全 unloaded 后再试。"
        )
    if owner_instance:
        return owner_instance
    running_instances = list_running_instances()
    if len(running_instances) == 1:
        return running_instances[0].instance_name
    return ""


def _assert_cross_instance_resume_loaded_gate(thread_id: str, *, target_instance: str) -> None:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return
    preview = preview_thread_global_loaded_gate(
        thread_id=normalized_thread_id,
        current_instance_name=target_instance,
        running_instances=list_running_instances(),
    )
    if preview.allowed:
        return
    raise ValueError(preview.reason_text)


def _resume_lookup_instance_name() -> str:
    running_instances = list_running_instances()
    if not running_instances:
        return ""
    current_instance = current_cli_instance_name()
    preferred_order = sorted(
        running_instances,
        key=lambda entry: (entry.instance_name != current_instance, entry.instance_name),
    )
    for entry in preferred_order:
        app_server_url = resolve_running_instance_app_server_url(entry)
        if app_server_url:
            return entry.instance_name
    raise ValueError("运行中的实例均未发布可用的 app-server 地址；请重启实例后再试。")


def _resolve_runtime_target_for_wrapper(
    *,
    explicit_instance: str,
    thread_id: str = "",
    preferred_running_instance: str = "",
    allow_default_running_fallback: bool = True,
) -> CliRuntimeTarget:
    """Resolve the one shared-backend runtime target for this wrapper launch."""
    preferred_instance = str(preferred_running_instance or "").strip() or (_lease_owner_instance(thread_id) if thread_id else "")
    try:
        return resolve_cli_runtime_target(
            explicit_instance=explicit_instance or None,
            preferred_running_instance=preferred_instance,
            allow_default_running_fallback=allow_default_running_fallback,
        )
    except ValueError as exc:
        error_text = str(exc)
        if (
            thread_id
            and "检测到多个运行中的实例" in error_text
            and "请显式传 `--instance <name>`" in error_text
        ):
            running_names = ", ".join(entry.instance_name for entry in list_running_instances()) or "（无）"
            error_text = (
                f"当前是 `{_program_name()} resume <thread>` 路径，但该 thread 现在没有唯一可用的实例路由。"
                f"当前运行中的实例：{running_names}。"
                "请显式传 `--instance <name>`。"
                "如果你原本期望沿用某个实例，请先确认该实例是否仍持有该 thread，"
                "必要时在该实例侧执行 `focusctl --instance <name> service reset-backend` 后再试。"
            )
        print(error_text, file=sys.stderr)
        raise SystemExit(2)

def _inject_default_cwd(user_args: list[str]) -> list[str]:
    if _has_explicit_cwd(user_args):
        return list(user_args)
    return ["--cd", os.getcwd(), *user_args]


def _attached_endpoint_adapter_config(
    cfg: CodexConfig,
    app_server_url: str,
    *,
    data_dir: pathlib.Path | None = None,
) -> CodexAppServerConfig:
    return CodexAppServerConfig.from_config(cfg).with_attached_endpoint(
        app_server_url=app_server_url,
        app_server_data_dir=str(data_dir) if data_dir is not None else "",
    )


def _resolve_thread_target_via_attached_endpoint(
    cfg: CodexConfig,
    app_server_url: str,
    data_dir: pathlib.Path,
    target: str,
) -> tuple[ThreadSummary | None, str | None]:
    cleaned = target.strip()
    if not cleaned:
        return None, "目标不能为空"
    if looks_like_thread_id(cleaned):
        config = _attached_endpoint_adapter_config(
            cfg,
            app_server_url,
            data_dir=data_dir,
        )
        adapter = CodexAppServerAdapter(config)
        try:
            return adapter.read_thread(cleaned, include_turns=False).summary, None
        except Exception as exc:
            return None, f"未找到匹配的线程：`{cleaned}` ({exc})"
        finally:
            adapter.stop()
    try:
        thread = resolve_resume_name_via_attached_endpoint(
            config=_attached_endpoint_adapter_config(
                cfg,
                app_server_url,
                data_dir=data_dir,
            ),
            query_limit=cfg.thread_list_query_limit,
            target=cleaned,
        )
    except Exception as exc:
        return None, str(exc)
    return thread, None


@dataclass(frozen=True, slots=True)
class _ResolvedResumeTarget:
    user_args: list[str]
    preferred_running_instance: str = ""


def _resolve_resume_lookup_runtime_target(
    explicit_instance: str,
) -> CliRuntimeTarget:
    normalized_explicit = str(explicit_instance or "").strip()
    if normalized_explicit:
        return _resolve_runtime_target_for_wrapper(
            explicit_instance=normalized_explicit,
            allow_default_running_fallback=False,
        )
    try:
        lookup_instance = _resume_lookup_instance_name()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    if lookup_instance:
        return _resolve_runtime_target_for_wrapper(
            explicit_instance=lookup_instance,
            allow_default_running_fallback=False,
        )
    return _resolve_runtime_target_for_wrapper(
        explicit_instance="",
        allow_default_running_fallback=False,
    )


def _resolve_resume_target(
    cfg: CodexConfig,
    explicit_instance: str,
    user_args: list[str],
) -> _ResolvedResumeTarget:
    target_index = _resume_target_index(user_args)
    if target_index is None:
        return _ResolvedResumeTarget(user_args=list(user_args))
    target = str(user_args[target_index] or "").strip()
    if not target:
        return _ResolvedResumeTarget(user_args=list(user_args))
    if explicit_instance:
        if looks_like_thread_id(target):
            try:
                _preferred_resume_instance_for_thread(target, explicit_instance=explicit_instance)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2)
            return _ResolvedResumeTarget(user_args=list(user_args))
        lookup_target = _resolve_resume_lookup_runtime_target(explicit_instance)
        thread, error = _resolve_thread_target_via_attached_endpoint(
            cfg,
            lookup_target.app_server_url,
            lookup_target.data_dir,
            target,
        )
        if thread is None:
            print(str(error or "未找到匹配的线程。"), file=sys.stderr)
            raise SystemExit(2)
        try:
            _preferred_resume_instance_for_thread(thread.thread_id, explicit_instance=explicit_instance)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        resolved = list(user_args)
        resolved[target_index] = thread.thread_id
        return _ResolvedResumeTarget(user_args=resolved)
    if looks_like_thread_id(target):
        try:
            preferred_instance = _preferred_resume_instance_for_thread(target)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        return _ResolvedResumeTarget(
            user_args=list(user_args),
            preferred_running_instance=preferred_instance,
        )
    lookup_target = _resolve_resume_lookup_runtime_target(explicit_instance)
    thread, error = _resolve_thread_target_via_attached_endpoint(
        cfg,
        lookup_target.app_server_url,
        lookup_target.data_dir,
        target,
    )
    if thread is None:
        print(str(error or "未找到匹配的线程。"), file=sys.stderr)
        raise SystemExit(2)
    try:
        preferred_instance = _preferred_resume_instance_for_thread(thread.thread_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    resolved = list(user_args)
    resolved[target_index] = thread.thread_id
    return _ResolvedResumeTarget(
        user_args=resolved,
        preferred_running_instance=preferred_instance,
    )

def _extract_option_value(user_args: list[str], names: tuple[str, ...]) -> str:
    i = 0
    while i < len(user_args):
        arg = user_args[i]
        if arg == "--":
            break
        for name in names:
            if arg == name:
                return user_args[i + 1] if i + 1 < len(user_args) else ""
            prefix = f"{name}="
            if arg.startswith(prefix):
                return arg[len(prefix) :]
        if (
            arg.split("=", 1)[0] in WRAPPER_PASSTHROUGH_OPTIONS_WITH_VALUE
            and "=" not in arg
        ):
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
    proxy_auth_token: str,
) -> tuple[str, subprocess.Popen[str]]:
    cmd = [
        sys.executable,
        "-m",
        "bot.fcodex.proxy",
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
        "--parent-pid",
        str(os.getpid()),
    ]
    env = os.environ.copy()
    env[FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR] = proxy_auth_token
    if service_token:
        env[FOCUS_SERVICE_TOKEN_ENV_VAR] = service_token
    process = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
        env=env,
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
    explicit_instance, user_args = _consume_instance_arg(sys.argv[1:])
    if _is_wrapper_version_request(user_args):
        print(f"{_program_name()} {__version__}")
        raise SystemExit(0)
    help_request = _wrapper_help_request_kind(user_args)
    if help_request == "top":
        _print_wrapper_help()
        raise SystemExit(0)
    if help_request == "resume":
        _print_wrapper_resume_help()
        raise SystemExit(0)
    management_command_error = _management_command_error(user_args)
    if management_command_error is not None:
        raise SystemExit(management_command_error)
    if _has_explicit_remote(user_args) or _has_explicit_remote_auth_token_env(
        user_args
    ):
        print(
            "`--remote` 与 `--remote-auth-token-env` 是 Focus wrapper 的内部参数；"
            "当前产品不接受用户指定外部 app-server。"
            "如需直接连接独立 endpoint，请绕过 Focus 并使用上游 `codex`。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    cfg = CodexConfig.from_dict(load_config_file("codex"))
    configured_codex_command = cfg.codex_command.strip()
    codex_command = resolve_managed_codex_command(configured_codex_command)
    if "--dry-run" in user_args:
        print(f"{_program_name()} 不再提供 `--dry-run` wrapper 入口。", file=sys.stderr)
        print("本地查看线程请改用 `focusctl thread list`、`thread status`、`thread bindings`。", file=sys.stderr)
        raise SystemExit(2)
    removed_wrapper_error = _removed_wrapper_command_error(user_args)
    if removed_wrapper_error is not None:
        raise SystemExit(removed_wrapper_error)

    preprocessed = _resolve_resume_target(cfg, explicit_instance, user_args)
    preprocessed_args = list(preprocessed.user_args)

    thread_target = _thread_target_hint(preprocessed_args)
    resolved_target = _resolve_runtime_target_for_wrapper(
        explicit_instance=explicit_instance,
        thread_id=thread_target,
        preferred_running_instance=preprocessed.preferred_running_instance,
        allow_default_running_fallback=not bool(thread_target),
    )
    data_dir = resolved_target.data_dir
    app_server_url = resolved_target.app_server_url
    if thread_target:
        try:
            _assert_cross_instance_resume_loaded_gate(
                thread_target,
                target_instance=resolved_target.instance_name,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)

    user_args = list(preprocessed_args)

    argv = [*shlex.split(codex_command)]
    effective_cwd = _resolve_effective_cwd(user_args)
    user_args = _inject_default_cwd(user_args)
    proxy_process: subprocess.Popen[str] | None = None
    proxy_auth_token = ""
    try:
        # Upstream Codex TUI omits `cwd` on `thread/start` in `--remote` mode.
        # Without this local proxy, the shared app-server falls back to its own
        # WorkingDirectory (`~/.local/share/focus`) and fresh wrapper sessions
        # don't inherit the caller's shell cwd.
        proxy_kwargs: dict[str, str] = {}
        if resolved_target.instance_name != DEFAULT_INSTANCE_NAME or resolved_target.service_token:
            proxy_kwargs = {
                "instance_name": resolved_target.instance_name,
                "service_token": resolved_target.service_token,
            }
        proxy_auth_token = secrets.token_urlsafe(32)
        proxy_url, proxy_process = _launch_local_cwd_proxy(
            app_server_url,
            effective_cwd,
            data_dir,
            proxy_auth_token=proxy_auth_token,
            **proxy_kwargs,
        )
    except Exception as exc:
        print(f"启动 {_program_name()} 本地 cwd proxy 失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
    argv.extend(["--remote", proxy_url, "--remote-auth-token-env", FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR])
    argv.extend(user_args)
    env = os.environ.copy()
    env["FOCUS_DATA_DIR"] = str(data_dir)
    env["FOCUS_INSTANCE"] = resolved_target.instance_name
    if proxy_auth_token:
        env[FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR] = proxy_auth_token
        _prepend_no_proxy_hosts(env)
    exit_code = _run_upstream_codex(argv, env, proxy_process=proxy_process)
    if exit_code is not None:
        raise SystemExit(exit_code)
    return


if __name__ == "__main__":
    main()
