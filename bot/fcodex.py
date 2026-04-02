"""
fcodex 本地 wrapper。
"""

from __future__ import annotations

import os
import shlex
import sys

from bot.config import load_config_file
from bot.constants import DEFAULT_APP_SERVER_URL


def main() -> None:
    cfg = load_config_file("codex")
    codex_command = str(cfg.get("codex_command", "codex")).strip() or "codex"
    app_server_url = str(cfg.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL

    argv = [*shlex.split(codex_command)]
    user_args = sys.argv[1:]
    if "--remote" not in user_args:
        argv.extend(["--remote", app_server_url])
    argv.extend(user_args)
    os.execvpe(argv[0], argv, os.environ.copy())
