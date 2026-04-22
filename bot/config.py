"""
配置加载模块。

配置目录按当前实例路径动态解析，避免模块导入时过早冻结目录状态。
"""

import os
import secrets
from pathlib import Path
from typing import Any

import yaml

from bot.file_permissions import ensure_private_file_permissions
from bot.instance_layout import default_config_root

_INIT_TOKEN_FILENAME = "init.token"


def config_dir() -> Path:
    raw = os.environ.get("FC_CONFIG_DIR", "").strip()
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
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    if mode is not None:
        if mode == 0o600:
            ensure_private_file_permissions(tmp_path)
        else:
            os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


def load_system_config_raw() -> dict[str, Any]:
    return _load_yaml_file(system_config_path())


def save_system_config(config: dict[str, Any]) -> Path:
    path = system_config_path()
    rendered = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    _atomic_write_text(path, rendered, mode=0o600)
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
            "请运行仓库根目录的 install.py 初始化配置"
            "（也可通过 macOS/Linux 的 `bash install.sh` 或 Windows PowerShell 的 `.\\install.ps1` 调用），"
            "或手动复制 config/system.yaml.example 并填入实际值。"
        )

    config = _load_yaml_file(path)

    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError(f"{path} 中 app_id 和 app_secret 不能为空")

    return config


def load_config_file(name: str) -> dict:
    """加载指定组件的配置 ({name}.yaml)

    文件不存在时返回空字典，组件将使用各自的默认值。
    """
    path = config_dir() / f"{name}.yaml"
    return _load_yaml_file(path)
