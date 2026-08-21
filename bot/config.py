"""
配置加载模块。

配置目录按当前实例路径动态解析，避免模块导入时过早冻结目录状态。
"""

import os
import secrets
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from bot.atomic_file import atomic_write_text, ensure_private_token
from bot.instance_layout import default_config_root
from bot.system_config import SystemConfig

_INIT_TOKEN_FILENAME = "init.token"


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def config_dir() -> Path:
    raw = os.environ.get("FOCUS_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return default_config_root()


def system_config_path() -> Path:
    return config_dir() / "system.yaml"


def init_token_path() -> Path:
    return config_dir() / _INIT_TOKEN_FILENAME


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        rendered = path.read_text(encoding="utf-8")
        data = yaml.load(rendered, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: YAML 解析失败：{exc}") from exc
    if data is None:
        # PyYAML maps both an absent document and an explicit YAML null to
        # ``None``.  Only the former is an empty component override; a real
        # null scalar must fail closed like every other non-mapping document.
        if yaml.compose(rendered, Loader=yaml.SafeLoader) is None:
            return {}
        raise ValueError(f"{path} 顶层不能是 YAML null；必须是 YAML mapping")
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层必须是 YAML mapping")
    return data


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    atomic_write_text(path, text, mode=mode)


def load_system_config_raw() -> dict[str, Any]:
    return _load_yaml_file(system_config_path())


def save_system_config(config: dict[str, Any]) -> Path:
    SystemConfig.from_dict(config, require_credentials=True)
    path = system_config_path()
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, rendered, mode=0o600)
    return path


def save_system_config_updates(updates: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    config = load_system_config_raw()
    config.update(updates)
    return config, save_system_config(config)


def ensure_init_token() -> str:
    return ensure_private_token(init_token_path(), lambda: secrets.token_urlsafe(24))


def load_config(*, directory: Path | None = None) -> SystemConfig:
    """加载并严格校验全局系统配置 (system.yaml)。"""
    path = (directory or config_dir()) / "system.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"系统配置文件不存在: {path}\n"
            "请通过 macOS/Linux 的 `bash install.sh` 或 Windows PowerShell 的 `.\\install.ps1` 初始化配置，"
            "或手动复制 config/system.yaml.example 并填入实际值。"
        )

    config = _load_yaml_file(path)
    try:
        return SystemConfig.from_dict(config, require_credentials=True)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def load_config_file(name: str, *, directory: Path | None = None) -> dict:
    """加载指定组件的配置 ({name}.yaml)

    文件不存在时返回空字典，组件将使用各自的默认值。
    """
    path = (directory or config_dir()) / f"{name}.yaml"
    return _load_yaml_file(path)
