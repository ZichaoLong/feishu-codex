"""
FOCUS daemon entrypoint.
"""

from __future__ import annotations

import argparse
import signal
import sys
import warnings

from bot.config import ensure_init_token, load_config
from bot.env_file import load_env_file
from bot.instance_layout import (
    DEFAULT_INSTANCE_NAME,
    apply_instance_environment,
    validate_instance_name,
)
from bot.logging_setup import configure_logging
from bot.version import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="focusd")
    parser.add_argument("--version", action="version", version=f"focusd {__version__}")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE_NAME)
    return parser


def _suppress_known_third_party_runtime_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"pkg_resources is deprecated as an API\..*",
        category=UserWarning,
        module=r"lark_oapi\.ws\.pb\.google",
    )


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    instance_name = validate_instance_name(args.instance)
    paths = apply_instance_environment(instance_name)
    load_env_file()
    configure_logging(data_dir=paths.data_dir)

    cfg = load_config(directory=paths.config_dir)

    ensure_init_token()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    _suppress_known_third_party_runtime_warnings()
    from bot.standalone import CodexBot

    bot = CodexBot(system_config=cfg)
    bot.start()


if __name__ == "__main__":
    main()
