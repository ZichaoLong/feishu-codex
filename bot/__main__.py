"""
feishu-codex daemon entrypoint.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

import yaml

from bot.config import ensure_init_token, load_config
from bot.env_file import load_env_file
from bot.instance_layout import DEFAULT_INSTANCE_NAME, apply_instance_environment, resolve_instance_paths, validate_instance_name
from bot.logging_setup import configure_logging
from bot.standalone import CodexBot


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu-codexd")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE_NAME)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    instance_name = validate_instance_name(args.instance)
    paths = apply_instance_environment(instance_name)
    load_env_file()
    configure_logging(data_dir=paths.data_dir)

    config_dir = Path(paths.config_dir)
    system_path = config_dir / "system.yaml"
    if not system_path.exists():
        raise FileNotFoundError(
            f"系统配置文件不存在: {system_path}\n"
            "请先运行 feishu-codex install 或复制 system.yaml.example 并填入飞书应用凭证。"
        )
    cfg = yaml.safe_load(system_path.read_text(encoding="utf-8")) or {}
    if not cfg.get("app_id") or not cfg.get("app_secret"):
        raise ValueError(f"{system_path} 中 app_id 和 app_secret 不能为空")

    ensure_init_token()
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
