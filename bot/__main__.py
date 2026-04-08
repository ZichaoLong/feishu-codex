"""
feishu-codex 启动入口。
"""

import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from bot.config import load_config
from bot.standalone import CodexBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config_dir = os.environ.get("FC_CONFIG_DIR")
    if config_dir:
        system_path = Path(config_dir) / "system.yaml"
        if not system_path.exists():
            raise FileNotFoundError(
                f"系统配置文件不存在: {system_path}\n"
                "请复制 config/system.yaml.example 为 system.yaml 并填入飞书应用凭证。"
            )
        cfg = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}
        if not cfg.get("app_id") or not cfg.get("app_secret"):
            raise ValueError(f"{system_path} 中 app_id 和 app_secret 不能为空")
    else:
        cfg = load_config()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    bot = CodexBot(
        cfg["app_id"],
        cfg["app_secret"],
        request_timeout_seconds=float(cfg.get("request_timeout_seconds", 10)),
        system_config=cfg,
    )
    bot.start()


if __name__ == "__main__":
    main()
