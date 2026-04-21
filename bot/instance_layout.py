"""
Multi-instance filesystem layout helpers.

`feishu-codex` keeps one shared machine-level coordination area, while each
Feishu instance keeps its own config/data subtree. The default instance stays
path-compatible with the original single-instance layout.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass

from bot.constants import FC_DATA_DIR, PROJECT_ROOT

DEFAULT_INSTANCE_NAME = "default"
_INSTANCES_SEGMENT = "instances"
_GLOBAL_SEGMENT = "_global"
_INSTANCE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,63})$")


def _looks_like_dev_layout() -> bool:
    return (PROJECT_ROOT / "bot").is_dir() and (PROJECT_ROOT / "config").is_dir()


def default_config_root() -> pathlib.Path:
    raw = os.environ.get("FC_CONFIG_ROOT", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if _looks_like_dev_layout():
        return PROJECT_ROOT / "config"
    return pathlib.Path.home() / ".config" / "feishu-codex"


def default_data_root() -> pathlib.Path:
    raw = os.environ.get("FC_DATA_ROOT", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    if _looks_like_dev_layout():
        return FC_DATA_DIR
    return pathlib.Path.home() / ".local" / "share" / "feishu-codex"


def global_data_dir() -> pathlib.Path:
    raw = os.environ.get("FC_GLOBAL_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_data_root() / _GLOBAL_SEGMENT


def validate_instance_name(instance_name: str) -> str:
    normalized = str(instance_name or "").strip().lower()
    if not normalized:
        raise ValueError("instance 名称不能为空。")
    if normalized == DEFAULT_INSTANCE_NAME:
        return normalized
    if not _INSTANCE_NAME_RE.match(normalized):
        raise ValueError(
            "instance 名称只能包含小写字母、数字、点、下划线、连字符，且必须以字母或数字开头。"
        )
    return normalized


def instance_config_dir(instance_name: str) -> pathlib.Path:
    normalized = validate_instance_name(instance_name)
    root = default_config_root()
    if normalized == DEFAULT_INSTANCE_NAME:
        return root
    return root / _INSTANCES_SEGMENT / normalized


def instance_data_dir(instance_name: str) -> pathlib.Path:
    normalized = validate_instance_name(instance_name)
    root = default_data_root()
    if normalized == DEFAULT_INSTANCE_NAME:
        return root
    return root / _INSTANCES_SEGMENT / normalized


def infer_instance_name_from_config_dir(path: pathlib.Path | str | None) -> str | None:
    return _infer_instance_name_from_path(path, root=default_config_root())


def infer_instance_name_from_data_dir(path: pathlib.Path | str | None) -> str | None:
    return _infer_instance_name_from_path(path, root=default_data_root())


def _infer_instance_name_from_path(path: pathlib.Path | str | None, *, root: pathlib.Path) -> str | None:
    if path is None:
        return None
    normalized = pathlib.Path(path).expanduser()
    try:
        if normalized == root:
            return DEFAULT_INSTANCE_NAME
        relative = normalized.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) == 2 and parts[0] == _INSTANCES_SEGMENT:
        try:
            return validate_instance_name(parts[1])
        except ValueError:
            return None
    return None


def current_instance_name(
    *,
    config_dir: pathlib.Path | str | None = None,
    data_dir: pathlib.Path | str | None = None,
) -> str:
    explicit = str(os.environ.get("FC_INSTANCE", "") or "").strip().lower()
    if explicit:
        return validate_instance_name(explicit)
    inferred = infer_instance_name_from_data_dir(data_dir)
    if inferred:
        return inferred
    inferred = infer_instance_name_from_config_dir(config_dir)
    if inferred:
        return inferred
    return DEFAULT_INSTANCE_NAME


@dataclass(frozen=True, slots=True)
class InstancePaths:
    instance_name: str
    config_dir: pathlib.Path
    data_dir: pathlib.Path
    global_data_dir: pathlib.Path


def resolve_instance_paths(instance_name: str) -> InstancePaths:
    normalized = validate_instance_name(instance_name)
    return InstancePaths(
        instance_name=normalized,
        config_dir=instance_config_dir(normalized),
        data_dir=instance_data_dir(normalized),
        global_data_dir=global_data_dir(),
    )
