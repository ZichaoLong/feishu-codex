#!/usr/bin/env python3
"""
Bootstrap installer for local feishu-codex development checkouts.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import venv

from bot.platform_paths import default_data_root


def _ensure_supported_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或更高版本。")


def _venv_python_path(venv_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def main() -> None:
    _ensure_supported_python()
    install_dir = pathlib.Path(__file__).resolve().parent
    venv_dir = default_data_root() / ".venv"
    if not venv_dir.exists():
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    venv_python = _venv_python_path(venv_dir)
    subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(venv_python), "-m", "pip", "install", str(install_dir)], check=True)
    subprocess.run([str(venv_python), "-m", "bot.manage_cli", "bootstrap-install"], check=True)


if __name__ == "__main__":
    main()
