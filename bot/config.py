"""
配置加载模块

从配置目录读取系统配置和组件配置。
配置目录优先从环境变量 FC_CONFIG_DIR 读取，回落到项目根目录下的 config/。
- system.yaml: 飞书应用凭证（app_id, app_secret）
- {name}.yaml: 各组件独立配置
"""

import os
import secrets
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = (
    Path(os.environ["FC_CONFIG_DIR"])
    if "FC_CONFIG_DIR" in os.environ
    else Path(__file__).parent.parent / "config"  # fallback：仅开发环境原地运行时有效
)
_INIT_TOKEN_FILENAME = "init.token"


def config_dir() -> Path:
    return _CONFIG_DIR


def system_config_path() -> Path:
    return _CONFIG_DIR / "system.yaml"


def init_token_path() -> Path:
    return _CONFIG_DIR / _INIT_TOKEN_FILENAME


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


def load_system_config_raw() -> dict[str, Any]:
    return _load_yaml_file(system_config_path())


def save_system_config(config: dict[str, Any]) -> Path:
    path = system_config_path()
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, rendered)
    return path


def save_system_config_updates(updates: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    config = load_system_config_raw()
    config.update(updates)
    return config, save_system_config(config)


def ensure_init_token() -> str:
    path = init_token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    _atomic_write_text(path, f"{token}\n", mode=0o600)
    return token


def load_config() -> dict:
    """加载全局系统配置 (system.yaml)"""
    path = system_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"系统配置文件不存在: {path}\n"
            "请运行 bash install.sh 初始化配置，或手动复制 config/system.yaml.example 并填入实际值。"
        )

    config = _load_yaml_file(path)

    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError(f"{path} 中 app_id 和 app_secret 不能为空")

    return config


def load_config_file(name: str) -> dict:
    """加载指定组件的配置 ({name}.yaml)

    文件不存在时返回空字典，组件将使用各自的默认值。
    """
    path = _CONFIG_DIR / f"{name}.yaml"
    return _load_yaml_file(path)
