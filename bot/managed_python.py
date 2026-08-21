"""Canonical isolated argv for Python modules in the Focus-managed venv."""

from __future__ import annotations

import pathlib


def isolated_python_module_command(
    python: pathlib.Path | str,
    module: str,
    *args: str,
) -> tuple[str, ...]:
    """Build argv that cannot import from cwd, user site, or ``PYTHONPATH``."""

    normalized_module = str(module or "").strip()
    if not normalized_module or not all(
        component.isidentifier() for component in normalized_module.split(".")
    ):
        raise ValueError(f"无效 Python module：{module!r}")
    return (
        str(pathlib.Path(python)),
        "-I",
        "-m",
        normalized_module,
        *(str(arg) for arg in args),
    )
