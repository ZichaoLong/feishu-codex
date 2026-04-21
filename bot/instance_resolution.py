"""
Shared local CLI helpers for multi-instance resolution.
"""

from __future__ import annotations

import os

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
