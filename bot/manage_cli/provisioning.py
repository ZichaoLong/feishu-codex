"""Shared Manage CLI provisioning and immutable projection helpers."""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import secrets
import stat

from bot.atomic_file import atomic_write_text
from bot.env_file import ensure_env_template
from bot.file_permissions import ensure_private_file_permissions
from bot.instance_layout import (
    DEFAULT_INSTANCE_NAME,
    apply_instance_environment,
    require_instance_exists,
    resolve_instance_paths,
    validate_instance_name,
)
from bot.platform_paths import (
    default_config_root,
    default_data_root,
    default_user_bin_dir,
    is_windows,
)
from bot.public_command_contract import PUBLIC_COMMAND_SPECS
from bot.service_manager import build_service_definition

from .errors import InstallLifecycleError


_MANAGED_ROOT_MARKER = ".focus-managed-root"
_MANAGED_ROOT_MARKER_SCHEMA = 1
_MANAGED_ROOT_MARKER_MAX_BYTES = 4096


def _install_templates_module():
    return importlib.import_module("bot.install_templates")


def _system_yaml_template() -> str:
    return _install_templates_module().SYSTEM_YAML_TEMPLATE


def _codex_yaml_template() -> str:
    return _install_templates_module().CODEX_YAML_TEMPLATE


def render_initial_codex_yaml() -> str:
    return _install_templates_module().render_initial_codex_yaml()


def _managed_venv_dir() -> pathlib.Path:
    return default_data_root() / ".venv"


def _venv_python() -> pathlib.Path:
    venv_dir = _managed_venv_dir()
    if is_windows():
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _ensure_text_file(
    path: pathlib.Path, contents: str, *, overwrite: bool, private: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        if private:
            ensure_private_file_permissions(path)
        return
    path.write_text(contents, encoding="utf-8")
    if private:
        ensure_private_file_permissions(path)


def _ensure_init_token(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        ensure_private_file_permissions(path)
        return
    path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
    ensure_private_file_permissions(path)


def _canonical_path(path: pathlib.Path | str, *, label: str) -> pathlib.Path:
    candidate = pathlib.Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = pathlib.Path.cwd() / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InstallLifecycleError(
            f"无法安全解析 {label}：{candidate}: {exc}"
        ) from exc


def _managed_root_marker_payload(root: pathlib.Path, *, role: str) -> dict[str, object]:
    return {
        "schema": _MANAGED_ROOT_MARKER_SCHEMA,
        "managed_by": "focus",
        "role": role,
        "canonical_target": str(root),
    }


def _managed_root_marker_matches(
    actual: dict[str, object], expected: dict[str, object]
) -> bool:
    return (
        set(actual) == set(expected)
        and type(actual.get("schema")) is int
        and isinstance(actual.get("managed_by"), str)
        and isinstance(actual.get("role"), str)
        and isinstance(actual.get("canonical_target"), str)
        and actual == expected
    )


def _read_managed_root_marker(root: pathlib.Path, *, role: str) -> dict[str, object]:
    marker = root / _MANAGED_ROOT_MARKER
    if marker.is_symlink():
        raise InstallLifecycleError(
            f"FOCUS {role} 根目录 marker 不能是符号链接：{marker}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(marker, flags)
    except FileNotFoundError as exc:
        raise InstallLifecycleError(
            f"FOCUS {role} 根目录缺少 {_MANAGED_ROOT_MARKER}：{root}。"
            "请先从当前源码运行 `bash install.sh` 或 `./install.ps1` 完成 repair，再重试 purge。"
        ) from exc
    except OSError as exc:
        raise InstallLifecycleError(
            f"FOCUS {role} 根目录 marker 无法可靠读取：{marker}: {exc}"
        ) from exc
    try:
        marker_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(marker_stat.st_mode):
            raise InstallLifecycleError(
                f"FOCUS {role} 根目录 marker 必须是普通文件：{marker}"
            )
        if marker_stat.st_size > _MANAGED_ROOT_MARKER_MAX_BYTES:
            raise InstallLifecycleError(
                f"FOCUS {role} 根目录 marker 异常过大：{marker}"
            )
        with os.fdopen(file_descriptor, "r", encoding="utf-8") as handle:
            file_descriptor = None
            raw = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallLifecycleError(
            f"FOCUS {role} 根目录 marker 无法可靠读取：{marker}: {exc}"
        ) from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    if not isinstance(raw, dict):
        raise InstallLifecycleError(f"FOCUS {role} 根目录 marker 格式无效：{marker}")
    return raw


def _ensure_managed_root_marker(root: pathlib.Path | str, *, role: str) -> pathlib.Path:
    canonical_root = _canonical_path(root, label=f"FOCUS {role} 根目录")
    canonical_root.mkdir(parents=True, exist_ok=True)
    marker = canonical_root / _MANAGED_ROOT_MARKER
    expected = _managed_root_marker_payload(canonical_root, role=role)
    if marker.is_symlink():
        raise InstallLifecycleError(
            f"FOCUS {role} 根目录 marker 不能是符号链接：{marker}"
        )
    if marker.exists():
        actual = _read_managed_root_marker(canonical_root, role=role)
        if not _managed_root_marker_matches(actual, expected):
            raise InstallLifecycleError(
                f"FOCUS {role} 根目录 marker 与当前布局不一致：{marker}。"
                "请确认 FOCUS_CONFIG_ROOT / FOCUS_DATA_ROOT 后移除错误 marker，再重新运行安装 repair。"
            )
        atomic_write_text(
            marker,
            json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            mode=0o600,
        )
        return marker
    atomic_write_text(
        marker,
        json.dumps(expected, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        mode=0o600,
    )
    return marker


def _ensure_instance_scaffold(instance_name: str) -> None:
    paths = apply_instance_environment(instance_name)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.global_data_dir.mkdir(parents=True, exist_ok=True)
    _ensure_managed_root_marker(default_config_root(), role="config")
    _ensure_managed_root_marker(default_data_root(), role="data")
    system_template = _system_yaml_template()
    codex_template = _codex_yaml_template()
    _ensure_text_file(
        paths.config_dir / "system.yaml.example", system_template, overwrite=True
    )
    _ensure_text_file(
        paths.config_dir / "codex.yaml.example", codex_template, overwrite=True
    )
    _ensure_text_file(
        paths.config_dir / "system.yaml", system_template, overwrite=False, private=True
    )
    _ensure_text_file(
        paths.config_dir / "codex.yaml", render_initial_codex_yaml(), overwrite=False
    )
    ensure_env_template()
    _ensure_init_token(paths.config_dir / "init.token")


def _module_command(module_name: str, *args: str) -> tuple[str, ...]:
    return (str(_venv_python()), "-m", module_name, *args)


def _wrapper_path(command_name: str) -> pathlib.Path:
    bin_dir = default_user_bin_dir()
    if is_windows():
        return bin_dir / f"{command_name}.cmd"
    return bin_dir / command_name


def _service_daemon_command(instance_name: str) -> tuple[str, ...]:
    return (
        str(_wrapper_path("focusd")),
        "--instance",
        validate_instance_name(instance_name),
    )


def _write_wrapper(
    path: pathlib.Path, module_name: str, *, wrapper_command: str = ""
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entrypoint = f"from {module_name} import main; main()"
    normalized_wrapper_command = str(wrapper_command or "").strip()
    if is_windows():
        wrapper_path = path.with_suffix(".cmd")
        lines = ["@echo off"]
        if normalized_wrapper_command:
            lines.append(f'set "FOCUS_WRAPPER_COMMAND={normalized_wrapper_command}"')
        lines.extend(
            [
                f'"{_venv_python()}" -c "{entrypoint}" %*',
                "",
            ]
        )
        wrapper_path.write_text(
            "\r\n".join(lines),
            encoding="utf-8",
        )
        return
    lines = ["#!/usr/bin/env sh"]
    if normalized_wrapper_command:
        lines.extend(
            [
                f"FOCUS_WRAPPER_COMMAND='{normalized_wrapper_command}'",
                "export FOCUS_WRAPPER_COMMAND",
            ]
        )
    lines.extend(
        [
            f'exec "{_venv_python()}" -c \'{entrypoint}\' "$@"',
            "",
        ]
    )
    path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _install_wrappers() -> pathlib.Path:
    bin_dir = default_user_bin_dir()
    for spec in PUBLIC_COMMAND_SPECS:
        _write_wrapper(
            bin_dir / spec.name,
            spec.module,
            wrapper_command=spec.wrapper_command,
        )
    return bin_dir


def _service_definition(instance_name: str):
    normalized = validate_instance_name(instance_name)
    paths = resolve_instance_paths(normalized)
    return build_service_definition(
        instance_name=normalized,
        paths=paths,
        daemon_command=_service_daemon_command(normalized),
    )


def _prepare_cli_instance(instance_name: str) -> str:
    normalized = validate_instance_name(instance_name)
    if normalized == DEFAULT_INSTANCE_NAME:
        _ensure_instance_scaffold(normalized)
        return normalized
    return require_instance_exists(normalized)


def _normalize_requested_instances(
    instance_names: list[str] | tuple[str, ...] | None,
) -> list[str]:
    raw_values = list(instance_names or [])
    if not raw_values:
        raw_values = [DEFAULT_INSTANCE_NAME]
    normalized_values: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        normalized = validate_instance_name(raw)
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_values.append(normalized)
    return normalized_values
