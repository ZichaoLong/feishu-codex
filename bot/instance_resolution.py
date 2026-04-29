"""
Shared local CLI helpers for multi-instance resolution.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from bot.instance_layout import DEFAULT_INSTANCE_NAME, current_instance_name, resolve_instance_paths, validate_instance_name
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
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
    explicit = str(os.environ.get("FC_INSTANCE", "") or "").strip()
    if explicit:
        return validate_instance_name(explicit)
    return current_instance_name()


def current_cli_instance_paths():
    return resolve_instance_paths(current_cli_instance_name())


def resolve_running_instance_app_server_url(
    entry: InstanceRegistryEntry,
    *,
    configured_app_server_url: str = "",
) -> str:
    data_dir = pathlib.Path(entry.data_dir)
    runtime = AppServerRuntimeStore(data_dir).load_managed_runtime()
    if runtime is not None and str(runtime.active_url or "").strip():
        return str(runtime.active_url).strip()
    recorded_url = str(entry.app_server_url or "").strip()
    if recorded_url:
        return recorded_url
    if configured_app_server_url:
        return resolve_effective_app_server_url(configured_app_server_url, data_dir=data_dir)
    return ""


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


def resolve_cli_instance_target(explicit_instance: str | None = None) -> CliInstanceTarget:
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
        paths = resolve_instance_paths(validated)
        return CliInstanceTarget(
            instance_name=paths.instance_name,
            data_dir=paths.data_dir,
        )
    unique = unique_running_instance()
    if unique is not None:
        return CliInstanceTarget(
            instance_name=unique.instance_name,
            data_dir=pathlib.Path(unique.data_dir),
            running_entry=unique,
        )
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
    configured_app_server_url: str,
    explicit_instance: str | None = None,
    preferred_running_instance: str = "",
    default_instance_data_dir: pathlib.Path | None = None,
) -> CliRuntimeTarget:
    """Resolve one runtime target for local CLI entrypoints.

    This is the shared instance-selection fact source for CLI wrappers:

    - explicit `--instance` wins
    - otherwise a preferred running owner instance may win
    - otherwise normal CLI instance resolution applies

    Wrapper-specific logic such as thread-lease owner discovery stays outside
    this module. Once the caller chooses a preferred owner instance, this helper
    resolves the resulting instance's data dir, runtime-discovered backend URL,
    and service token in one place.
    """

    preferred_instance = str(preferred_running_instance or "").strip().lower()
    resolved = _resolve_cli_runtime_base_target(
        explicit_instance=explicit_instance,
        preferred_running_instance=preferred_instance,
    )
    running_entry = resolved.running_entry
    data_dir = pathlib.Path(running_entry.data_dir) if running_entry is not None else resolved.data_dir
    if resolved.instance_name == DEFAULT_INSTANCE_NAME and default_instance_data_dir is not None:
        data_dir = pathlib.Path(default_instance_data_dir)
    if running_entry is not None:
        app_server_url = resolve_running_instance_app_server_url(
            running_entry,
            configured_app_server_url=configured_app_server_url,
        )
        service_token = running_entry.service_token
    else:
        app_server_url = resolve_effective_app_server_url(configured_app_server_url, data_dir=data_dir)
        service_token = ""
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
) -> CliInstanceTarget:
    if explicit_instance:
        return resolve_cli_instance_target(explicit_instance)
    if preferred_running_instance:
        running = load_running_instance(preferred_running_instance)
        if running is not None:
            return CliInstanceTarget(
                instance_name=running.instance_name,
                data_dir=pathlib.Path(running.data_dir),
                running_entry=running,
            )
    return resolve_cli_instance_target()
