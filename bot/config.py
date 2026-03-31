"""
配置加载模块

从配置目录读取系统配置和组件配置。
配置目录优先从环境变量 FC_CONFIG_DIR 读取，回落到项目根目录下的 config/。
- system.yaml: 飞书应用凭证（app_id, app_secret）
- {name}.yaml: 各组件独立配置
"""

import os
from pathlib import Path

import yaml

_CONFIG_DIR = (
    Path(os.environ["FC_CONFIG_DIR"])
    if "FC_CONFIG_DIR" in os.environ
    else Path(__file__).parent.parent / "config"  # fallback：仅开发环境原地运行时有效
)


def load_config() -> dict:
    """加载全局系统配置 (system.yaml)"""
    path = _CONFIG_DIR / "system.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"系统配置文件不存在: {path}\n"
            "请运行 bash install.sh 初始化配置，或手动复制 config/system.yaml.example 并填入实际值。"
        )

    with open(path) as f:
        config = yaml.safe_load(f) or {}

    if not config.get("app_id") or not config.get("app_secret"):
        raise ValueError(f"{path} 中 app_id 和 app_secret 不能为空")

    return config


def load_config_file(name: str) -> dict:
    """加载指定组件的配置 ({name}.yaml)

    文件不存在时返回空字典，组件将使用各自的默认值。
    """
    path = _CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}
