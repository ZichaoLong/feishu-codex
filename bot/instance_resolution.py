"""
Shared local CLI helpers for multi-instance resolution.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from bot.instance_layout import (
    DEFAULT_INSTANCE_NAME,
    current_instance_name,
    require_instance_exists,
    resolve_instance_paths,
    validate_instance_name,
)
from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.instance_registry_store import InstanceRegistryEntry, InstanceRegistryStore


def list_running_instances() -> list[InstanceRegistryEntry]:
    return InstanceRegistryStore().list_instances()


def load_running_instance(instance_name: str) -> InstanceRegistryEntry | None:
    normalized = validate_instance_name(instance_name)
    return InstanceRegistryStore().load(normalized)


def unique_running_instance() -> InstanceRegistryEntry | None:
    instances = list_running_instances()
    if len(instances) != 1:
        return None
    return instances[0]


def default_running_instance() -> InstanceRegistryEntry | None:
    return load_running_instance(DEFAULT_INSTANCE_NAME)


def current_cli_instance_name() -> str:
    explicit = str(os.environ.get("FOCUS_INSTANCE", "") or "").strip()
    if explicit:
        return validate_instance_name(explicit)
    return current_instance_name()


def current_cli_instance_paths():
    return resolve_instance_paths(current_cli_instance_name())


def _resolve_running_instance_app_server_url_via_control_plane(data_dir: pathlib.Path) -> str:
    try:
        result = control_request(pathlib.Path(data_dir), "service/status")
    except ServiceControlError:
        return ""
    if not isinstance(result, dict):
        return ""
    return str(result.get("app_server_url", "") or "").strip()


def resolve_running_instance_app_server_url(
    entry: InstanceRegistryEntry,
) -> str:
    """Resolve only the READY endpoint published by the live Focus service.

    Registry/configuration URLs and the owned-runtime record are discovery or
    process-lifecycle facts, not protocol-readiness proof. Falling back to any
    of them would let a reset or guardian-cleanup window publish a stale port,
    which may later be occupied by an unrelated process.
    """

    data_dir = pathlib.Path(entry.data_dir)
    return _resolve_running_instance_app_server_url_via_control_plane(data_dir)


@dataclass(frozen=True, slots=True)
class CliInstanceTarget:
    instance_name: str
    data_dir: pathlib.Path
    running_entry: InstanceRegistryEntry | None = None


@dataclass(frozen=True, slots=True)
class CliRuntimeTarget:
    instance_name: str
    data_dir: pathlib.Path
    app_server_url: str
    service_token: str = ""
    running_entry: InstanceRegistryEntry | None = None


def resolve_cli_instance_target(
    explicit_instance: str | None = None,
    *,
    preferred_running_instance: str = "",
    allow_default_running_fallback: bool = True,
) -> CliInstanceTarget:
    normalized_instance = str(explicit_instance or "").strip()
    if normalized_instance:
        validated = validate_instance_name(normalized_instance)
        running = load_running_instance(validated)
        if running is not None:
            return CliInstanceTarget(
                instance_name=running.instance_name,
                data_dir=pathlib.Path(running.data_dir),
                running_entry=running,
            )
        require_instance_exists(validated)
        paths = resolve_instance_paths(validated)
        return CliInstanceTarget(
            instance_name=paths.instance_name,
            data_dir=paths.data_dir,
        )
    normalized_preferred = str(preferred_running_instance or "").strip().lower()
    if normalized_preferred:
        running = load_running_instance(normalized_preferred)
        if running is not None:
            return CliInstanceTarget(
                instance_name=running.instance_name,
                data_dir=pathlib.Path(running.data_dir),
                running_entry=running,
            )
    unique = unique_running_instance()
    if unique is not None:
        return CliInstanceTarget(
            instance_name=unique.instance_name,
            data_dir=pathlib.Path(unique.data_dir),
            running_entry=unique,
        )
    if allow_default_running_fallback:
        default = default_running_instance()
        if default is not None:
            return CliInstanceTarget(
                instance_name=default.instance_name,
                data_dir=pathlib.Path(default.data_dir),
                running_entry=default,
            )
    running_instances = list_running_instances()
    if len(running_instances) > 1:
        raise ValueError("检测到多个运行中的实例，请显式传 `--instance <name>`。")
    if not running_instances:
        paths = current_cli_instance_paths()
        require_instance_exists(paths.instance_name)
        return CliInstanceTarget(
            instance_name=paths.instance_name,
            data_dir=paths.data_dir,
        )
    only = running_instances[0]
    return CliInstanceTarget(
        instance_name=only.instance_name,
        data_dir=pathlib.Path(only.data_dir),
        running_entry=only,
    )


def resolve_cli_runtime_target(
    *,
    explicit_instance: str | None = None,
    preferred_running_instance: str = "",
    allow_default_running_fallback: bool = True,
) -> CliRuntimeTarget:
    """Resolve one runtime target for local CLI entrypoints.

    This is the shared instance-selection fact source for CLI wrappers:

    - explicit `--instance` wins
    - otherwise a preferred running owner instance may win
    - otherwise normal CLI instance resolution applies

    `allow_default_running_fallback` lets callers split two public contracts:

    - threadless launches may still treat a running `default` instance as the
      convenience fallback
    - thread-targeted resume paths may disable that fallback and fail closed
      when the target instance is still ambiguous

    Wrapper-specific logic such as thread-lease owner discovery stays outside
    this module. Once the caller chooses a preferred owner instance, this helper
    resolves the resulting instance's data dir, runtime-discovered backend URL,
    and service token in one place.
    """

    preferred_instance = str(preferred_running_instance or "").strip().lower()
    resolved = _resolve_cli_runtime_base_target(
        explicit_instance=explicit_instance,
        preferred_running_instance=preferred_instance,
        allow_default_running_fallback=allow_default_running_fallback,
    )
    running_entry = resolved.running_entry
    if running_entry is None:
        raise ValueError(
            f"实例 `{resolved.instance_name}` 当前未运行，未发布可连接的 app-server；"
            "请先启动该实例的 Focus service 后再试。"
        )
    data_dir = pathlib.Path(running_entry.data_dir)
    app_server_url = resolve_running_instance_app_server_url(running_entry)
    if not app_server_url:
        raise ValueError(
            f"运行中的实例 `{resolved.instance_name}` 未发布可用的 app-server 地址；请重启该实例后再试。"
        )
    service_token = running_entry.service_token
    return CliRuntimeTarget(
        instance_name=resolved.instance_name,
        data_dir=data_dir,
        app_server_url=app_server_url,
        service_token=service_token,
        running_entry=running_entry,
    )


def _resolve_cli_runtime_base_target(
    *,
    explicit_instance: str | None,
    preferred_running_instance: str,
    allow_default_running_fallback: bool,
) -> CliInstanceTarget:
    return resolve_cli_instance_target(
        explicit_instance,
        preferred_running_instance=preferred_running_instance,
        allow_default_running_fallback=allow_default_running_fallback,
    )
