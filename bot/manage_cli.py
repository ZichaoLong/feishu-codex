"""
Cross-platform management CLI for local feishu-codex installation.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import shlex
import shutil
import subprocess
import sys
import time

from bot import __main__ as daemon_entry
from bot.env_file import ensure_env_template
from bot.file_permissions import ensure_private_file_permissions
from bot.instance_layout import DEFAULT_INSTANCE_NAME, apply_instance_environment, resolve_instance_paths, validate_instance_name
from bot.install_templates import CODEX_YAML_TEMPLATE, SYSTEM_YAML_TEMPLATE
from bot.platform_paths import default_config_root, default_data_root, default_log_file, default_user_bin_dir, is_windows
from bot.service_manager import ServiceManagerError, build_service_definition, current_service_manager


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu-codex")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("install")
    subparsers.add_parser("bootstrap-install")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("restart")
    subparsers.add_parser("status")
    subparsers.add_parser("run")

    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--lines", type=int, default=40)

    config_parser = subparsers.add_parser("config")
    config_parser.add_argument("target", nargs="?", choices=["system", "codex", "env", "init-token"])
    config_parser.add_argument("--open", action="store_true")

    subparsers.add_parser("uninstall")
    subparsers.add_parser("purge")
    return parser


def _venv_python() -> pathlib.Path:
    return pathlib.Path(sys.executable).resolve()


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _ensure_text_file(path: pathlib.Path, contents: str, *, overwrite: bool, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return
    path.write_text(contents, encoding="utf-8")
    if private:
        ensure_private_file_permissions(path)


def _ensure_init_token(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return
    path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
    ensure_private_file_permissions(path)


def _ensure_instance_scaffold(instance_name: str) -> None:
    paths = apply_instance_environment(instance_name)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.global_data_dir.mkdir(parents=True, exist_ok=True)
    _ensure_text_file(paths.config_dir / "system.yaml.example", SYSTEM_YAML_TEMPLATE, overwrite=True)
    _ensure_text_file(paths.config_dir / "codex.yaml.example", CODEX_YAML_TEMPLATE, overwrite=True)
    _ensure_text_file(paths.config_dir / "system.yaml", SYSTEM_YAML_TEMPLATE, overwrite=False, private=True)
    _ensure_text_file(paths.config_dir / "codex.yaml", CODEX_YAML_TEMPLATE, overwrite=False)
    ensure_env_template()
    _ensure_init_token(paths.config_dir / "init.token")


def _module_command(module_name: str, *args: str) -> tuple[str, ...]:
    return (str(_venv_python()), "-m", module_name, *args)


def _write_wrapper(path: pathlib.Path, module_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_windows():
        wrapper_path = path.with_suffix(".cmd")
        wrapper_path.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    f'"{_venv_python()}" -m {module_name} %*',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                f'exec "{_venv_python()}" -m {module_name} "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _install_wrappers() -> pathlib.Path:
    bin_dir = default_user_bin_dir()
    _write_wrapper(bin_dir / "feishu-codex", "bot.manage_cli")
    _write_wrapper(bin_dir / "feishu-codexd", "bot.__main__")
    _write_wrapper(bin_dir / "feishu-codexctl", "bot.feishu_codexctl")
    _write_wrapper(bin_dir / "fcodex", "bot.fcodex")
    return bin_dir


def _open_in_editor(path: pathlib.Path) -> int:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if is_windows() else "nano"
    argv = [*shlex.split(editor), str(path)]
    return subprocess.call(argv)


def _tail_log(path: pathlib.Path, *, lines: int) -> int:
    if not path.exists():
        print(f"log file not found: {path}", file=sys.stderr)
        return 2
    buffer = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in buffer[-max(lines, 0) :]:
        print(line)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="")
                    continue
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def _service_definition(instance_name: str):
    normalized = validate_instance_name(instance_name)
    paths = resolve_instance_paths(normalized)
    return build_service_definition(
        instance_name=normalized,
        paths=paths,
        daemon_command=_module_command("bot.__main__", "--instance", normalized),
    )


def _known_instance_names() -> list[str]:
    names = {DEFAULT_INSTANCE_NAME}
    config_root = default_config_root()
    data_root = default_data_root()
    for root in (config_root / "instances", data_root / "instances"):
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir():
                try:
                    names.add(validate_instance_name(child.name))
                except ValueError:
                    continue
    return sorted(names)


def _print_install_summary(bin_dir: pathlib.Path) -> None:
    print("安装完成。")
    print(f"配置根目录: {default_config_root()}")
    print(f"数据根目录: {default_data_root()}")
    print(f"命令目录: {bin_dir}")
    if not shutil.which("codex"):
        print("警告: 未检测到 `codex` 命令，请先安装 Codex CLI。")
    print("")
    print("下一步:")
    print(f"  1. 编辑配置: {resolve_instance_paths(DEFAULT_INSTANCE_NAME).config_dir / 'system.yaml'}")
    print(f"  2. 按需写入 provider 环境变量: {default_config_root() / 'feishu-codex.env'}")
    print("  3. 启动服务: feishu-codex start")
    print("  4. 查看初始化口令: feishu-codex config init-token")


def _handle_install() -> int:
    _ensure_instance_scaffold(DEFAULT_INSTANCE_NAME)
    bin_dir = _install_wrappers()
    _print_install_summary(bin_dir)
    return 0


def _handle_service_action(instance_name: str, action: str) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    definition = _service_definition(normalized)
    manager = current_service_manager()
    if action == "start":
        manager.start(definition)
        print(f"started service: {definition.identifier}")
        return 0
    if action == "stop":
        manager.stop(definition)
        print(f"stopped service: {definition.identifier}")
        return 0
    if action == "restart":
        manager.restart(definition)
        print(f"restarted service: {definition.identifier}")
        return 0
    if action == "status":
        status = manager.status(definition)
        print(f"service: {'installed' if status.installed else 'missing'}")
        print(f"running: {'yes' if status.running else 'no'}")
        if status.detail:
            print(f"detail: {status.detail}")
        return 0 if status.running else 3
    raise ValueError(f"unknown service action: {action}")


def _handle_run(instance_name: str) -> int:
    daemon_entry.main(["--instance", validate_instance_name(instance_name)])
    return 0


def _handle_config(instance_name: str, target: str | None, *, open_editor: bool) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    paths = resolve_instance_paths(normalized)
    candidates = {
        "system": paths.config_dir / "system.yaml",
        "codex": paths.config_dir / "codex.yaml",
        "env": default_config_root() / "feishu-codex.env",
        "init-token": paths.config_dir / "init.token",
    }
    if target is None:
        print(f"instance: {normalized}")
        for key, path in candidates.items():
            print(f"{key}: {path}")
        return 0
    resolved = candidates[target]
    print(resolved)
    if open_editor:
        return _open_in_editor(resolved)
    return 0


def _remove_wrappers() -> None:
    bin_dir = default_user_bin_dir()
    if is_windows():
        for name in ("feishu-codex", "feishu-codexd", "feishu-codexctl", "fcodex"):
            try:
                (bin_dir / f"{name}.cmd").unlink()
            except FileNotFoundError:
                pass
        return
    for name in ("feishu-codex", "feishu-codexd", "feishu-codexctl", "fcodex"):
        try:
            (bin_dir / name).unlink()
        except FileNotFoundError:
            pass


def _handle_uninstall(*, purge: bool) -> int:
    try:
        manager = current_service_manager()
    except ServiceManagerError:
        manager = None
    for instance_name in _known_instance_names():
        definition = _service_definition(instance_name)
        if manager is not None:
            try:
                manager.uninstall(definition)
            except ServiceManagerError:
                pass
    _remove_wrappers()
    if purge:
        shutil.rmtree(default_config_root(), ignore_errors=True)
        shutil.rmtree(default_data_root(), ignore_errors=True)
        print("已删除配置、数据、service 定义与命令包装器。")
    else:
        print("已删除 service 定义与命令包装器，配置和数据保留。")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        if args.command in {"install", "bootstrap-install"}:
            raise SystemExit(_handle_install())
        if args.command in {"start", "stop", "restart", "status"}:
            raise SystemExit(_handle_service_action(args.instance, args.command))
        if args.command == "run":
            raise SystemExit(_handle_run(args.instance))
        if args.command == "log":
            raise SystemExit(_tail_log(default_log_file(resolve_instance_paths(validate_instance_name(args.instance)).data_dir), lines=args.lines))
        if args.command == "config":
            raise SystemExit(_handle_config(args.instance, args.target, open_editor=args.open))
        if args.command == "uninstall":
            raise SystemExit(_handle_uninstall(purge=False))
        if args.command == "purge":
            raise SystemExit(_handle_uninstall(purge=True))
    except ServiceManagerError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
