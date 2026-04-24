"""
Shared local CLI helpers for multi-instance resolution.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from bot.instance_layout import DEFAULT_INSTANCE_NAME, current_instance_name, resolve_instance_paths, validate_instance_name
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


@dataclass(frozen=True, slots=True)
class CliInstanceTarget:
    instance_name: str
    data_dir: pathlib.Path
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
