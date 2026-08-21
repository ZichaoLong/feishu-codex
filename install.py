#!/usr/bin/env python3
"""Install a validated Focus bundle from Releases or a local artifact."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import venv
from collections.abc import Iterator
from typing import Any


_GITHUB_REPOSITORY = "ZichaoLong/focus"
_GITHUB_API_ROOT = f"https://api.github.com/repos/{_GITHUB_REPOSITORY}"
_DEVELOPMENT_RELEASE_TAG = "development-builds"
_GITHUB_API_VERSION = "2022-11-28"
_MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_CHANNEL_MANIFEST_BYTES = 64 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从已验证的制品安装或修复 FOCUS、命令 wrapper 与 service 定义。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "来源：\n"
            "  默认等同于 --channel stable，下载最新正式 GitHub Release 的 Focus bundle。\n"
            "  --channel development 下载显式发布的最新 development bundle。\n"
            "  --artifact PATH 使用已下载或本地构建的 bundle，不访问 GitHub。\n"
            "\n"
            "bundle 是 GitHub Release 可下载的 Focus ZIP 制品，包含 Focus wheel（含 Web）、\n"
            "Python 依赖锁和校验 manifest；它不包含 Python 解释器或第三方依赖 wheelhouse。\n"
            "因此 --artifact 不下载 Focus 制品，但 pip 仍可能按用户配置联网解析依赖。\n"
            "HTTP_PROXY、HTTPS_PROXY、NO_PROXY 以及 pip 自身配置会原样生效。\n"
            "\n"
            "开发者本地构建：先在 web/ 执行 npm run build，再执行\n"
            "  python scripts/build_install_bundle.py\n"
            "随后把输出的 ZIP 传给 --artifact；构建本身不会发布到 GitHub。"
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--channel",
        choices=("stable", "development"),
        help="从对应 GitHub Release channel 下载；默认 stable。",
    )
    source.add_argument(
        "--artifact",
        type=pathlib.Path,
        metavar="PATH",
        help="使用本地 Focus bundle ZIP；此模式不访问 GitHub。",
    )
    parser.add_argument(
        "--migrate-from-feishu-codex",
        action="store_true",
        help="安装新 FOCUS 后执行一次性旧 feishu-codex 迁移。",
    )
    args = parser.parse_args(argv)
    if args.channel is None and args.artifact is None:
        args.channel = "stable"
    return args


def _ensure_supported_python() -> None:
    if sys.implementation.name != "cpython" or sys.version_info < (3, 11):
        raise SystemExit("需要 CPython 3.11 或更高版本。")


def _venv_python_path(venv_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_cfg_path(venv_dir: pathlib.Path) -> pathlib.Path:
    return venv_dir / "pyvenv.cfg"


def _venv_is_complete(venv_dir: pathlib.Path) -> bool:
    return _venv_cfg_path(venv_dir).exists() and _venv_python_path(venv_dir).exists()


def _venv_uses_supported_python(venv_dir: pathlib.Path) -> bool:
    """Prove that an existing managed environment satisfies the installer ABI."""

    venv_python = _venv_python_path(venv_dir)
    try:
        result = subprocess.run(
            [
                str(venv_python),
                "-c",
                (
                    "import sys; raise SystemExit(0 if "
                    "sys.implementation.name == 'cpython' and "
                    "sys.version_info >= (3, 11) else 1)"
                ),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _recreate_venv(venv_dir: pathlib.Path) -> None:
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    except subprocess.CalledProcessError as exc:
        versioned_package = f"python{sys.version_info.major}.{sys.version_info.minor}-venv"
        raise SystemExit(
            "创建受管 .venv 时无法引导 pip；当前 Python 可能缺少 venv/ensurepip 组件。"
            "Debian/Ubuntu 通常需要安装 `python3-venv` 或与解释器匹配的 "
            f"`{versioned_package}`。target={venv_dir} exit_code={exc.returncode}"
        ) from exc


def _run_checked(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def _run_pip_install(venv_python: pathlib.Path, *args: str) -> None:
    command = [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", *args]
    try:
        _run_checked(command)
    except subprocess.CalledProcessError as exc:
        # Index choice is a package-supply-chain authority.  Pip already
        # resolves its default, environment, and configuration-file sources;
        # silently adding another index after a failure can cross a private
        # repository boundary and can select a different artifact for the
        # same version.  Keep the failure explicit and let the operator repair
        # the configured source or cache before retrying the one-step install.
        raise SystemExit(
            "受管环境依赖安装失败；请先查看上方 pip 原始错误，并检查已配置的 package index、网络、"
            "证书或本地缓存；目标平台没有可用 wheel 时还需要本地编译链。"
            "安装器不会在失败后静默追加另一个 package index。"
        ) from exc


def _venv_has_pip(venv_python: pathlib.Path) -> bool:
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _ensure_venv_pip(venv_python: pathlib.Path) -> None:
    if _venv_has_pip(venv_python):
        return
    try:
        _run_checked([str(venv_python), "-m", "ensurepip", "--upgrade"])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "当前 Python 无法在受管 .venv 中引导 pip；"
            "请确认已安装该 Python 对应的 venv/ensurepip 组件，"
            "或删除受管 .venv 后重试。"
        ) from exc
    if not _venv_has_pip(venv_python):
        raise SystemExit("已尝试使用 ensurepip 修复受管 .venv，但其中仍然缺少 pip。")


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "focus-installer",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _read_response_bytes(
    request: urllib.request.Request,
    *,
    maximum: int,
    expected_size: int | None = None,
) -> bytes:
    try:
        with urllib.request.urlopen(  # noqa: S310 - URLs are fixed or GitHub-owned asset URLs.
            request,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    parsed_length = int(declared_length)
                except ValueError as exc:
                    raise SystemExit("GitHub响应包含无效Content-Length。") from exc
                if parsed_length > maximum:
                    raise SystemExit("GitHub响应超过安装器允许的大小。")
                if expected_size is not None and parsed_length != expected_size:
                    raise SystemExit("GitHub制品Content-Length与channel manifest不一致。")
            payload = response.read(maximum + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise SystemExit(f"无法从GitHub读取安装制品：{exc}") from exc
    if len(payload) > maximum:
        raise SystemExit("GitHub响应超过安装器允许的大小。")
    if expected_size is not None and len(payload) != expected_size:
        raise SystemExit("GitHub制品实际大小与channel manifest不一致。")
    return payload


def _download_github_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_github_headers())
    raw = _read_response_bytes(request, maximum=_MAX_RELEASE_RESPONSE_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("GitHub Release API没有返回有效JSON。") from exc
    if not isinstance(payload, dict):
        raise SystemExit("GitHub Release API响应必须是object。")
    return payload


def _asset_download_url(asset: dict[str, Any], *, expected_name: str) -> tuple[str, int]:
    name = asset.get("name")
    size = asset.get("size")
    url = asset.get("browser_download_url")
    if name != expected_name or type(size) is not int or size <= 0 or not isinstance(url, str):
        raise SystemExit(f"GitHub Release asset元数据无效：{expected_name}")
    parsed = urllib.parse.urlparse(url)
    expected_prefix = f"/{_GITHUB_REPOSITORY}/releases/download/"
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not parsed.path.startswith(expected_prefix):
        raise SystemExit(f"GitHub Release asset下载地址不受信任：{expected_name}")
    return url, size


def _release_asset(release: dict[str, Any], name: str) -> dict[str, Any]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise SystemExit("GitHub Release缺少assets列表。")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise SystemExit(f"GitHub Release必须恰好包含一个{name}，实际为{len(matches)}。")
    return matches[0]


def _release_for_channel(channel: str) -> dict[str, Any]:
    if channel == "stable":
        url = f"{_GITHUB_API_ROOT}/releases/latest"
    elif channel == "development":
        url = f"{_GITHUB_API_ROOT}/releases/tags/{_DEVELOPMENT_RELEASE_TAG}"
    else:
        raise SystemExit(f"不受支持的安装channel：{channel}")
    release = _download_github_json(url)
    tag = release.get("tag_name")
    if (
        release.get("draft") is not False
        or type(release.get("prerelease")) is not bool
        or not isinstance(tag, str)
        or not tag
    ):
        raise SystemExit("GitHub Release状态或tag无效。")
    if channel == "stable" and release["prerelease"]:
        raise SystemExit("stable channel不能使用prerelease。")
    if channel == "development" and (
        not release["prerelease"] or tag != _DEVELOPMENT_RELEASE_TAG
    ):
        raise SystemExit("development channel必须使用固定development prerelease。")
    return release


@contextlib.contextmanager
def _resolved_install_bundle(args: argparse.Namespace) -> Iterator[Any]:
    from scripts.build_support.install_bundle import (
        CHANNEL_MANIFEST_NAMES,
        InstallBundleError,
        parse_channel_manifest,
        sha256_file,
        validate_install_bundle,
    )

    with tempfile.TemporaryDirectory(
        prefix="focus-install-artifact-",
        ignore_cleanup_errors=True,
    ) as raw_root:
        root = pathlib.Path(raw_root)
        artifact = args.artifact
        expected_channel: str | None = None
        expected_version: str | None = None
        expected_build_id: str | None = None
        expected_source_revision: str | None = None
        if artifact is None:
            channel = args.channel
            release = _release_for_channel(channel)
            release_tag = release["tag_name"]
            channel_name = CHANNEL_MANIFEST_NAMES[channel]
            channel_asset = _release_asset(release, channel_name)
            channel_url, channel_size = _asset_download_url(
                channel_asset,
                expected_name=channel_name,
            )
            if channel_size > _MAX_CHANNEL_MANIFEST_BYTES:
                raise SystemExit("GitHub channel manifest超过安装器上限。")
            channel_raw = _read_response_bytes(
                urllib.request.Request(channel_url, headers={"User-Agent": "focus-installer"}),
                maximum=_MAX_CHANNEL_MANIFEST_BYTES,
                expected_size=channel_size,
            )
            manifest = parse_channel_manifest(channel_raw, expected_channel=channel)
            if manifest.release_tag != release_tag:
                raise SystemExit("channel manifest的release_tag与GitHub Release不一致。")
            bundle_asset = _release_asset(release, manifest.bundle.name)
            bundle_url, bundle_size = _asset_download_url(
                bundle_asset,
                expected_name=manifest.bundle.name,
            )
            if bundle_size != manifest.bundle.size:
                raise SystemExit("GitHub asset size与channel manifest不一致。")
            artifact = root / manifest.bundle.name
            artifact.write_bytes(
                _read_response_bytes(
                    urllib.request.Request(bundle_url, headers={"User-Agent": "focus-installer"}),
                    maximum=manifest.bundle.size,
                    expected_size=manifest.bundle.size,
                )
            )
            if sha256_file(artifact) != manifest.bundle.sha256:
                raise SystemExit("GitHub bundle SHA-256与channel manifest不一致。")
            expected_channel = manifest.channel
            expected_version = manifest.version
            expected_build_id = manifest.build_id
            expected_source_revision = manifest.source_revision
        else:
            artifact = artifact.expanduser()

        try:
            bundle = validate_install_bundle(
                artifact,
                extraction_dir=root / "validated",
                expected_channel=expected_channel,
                expected_version=expected_version,
                expected_build_id=expected_build_id,
                expected_source_revision=expected_source_revision,
            )
        except InstallBundleError as exc:
            raise SystemExit(str(exc)) from exc
        yield bundle


def _managed_install_transaction():
    # Import only after the source/Python preflight.  The installer itself must
    # remain runnable before the managed environment exists.
    from bot.manage_cli.install_surface import create_managed_install_transaction

    return create_managed_install_transaction(operation="install")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args([] if argv is None else argv)
    _ensure_supported_python()
    with _resolved_install_bundle(args) as bundle:
        # All remote I/O and artifact validation is complete before Focus owns
        # service state or mutates the managed environment.
        from bot.install_lifecycle import ManagedInstallLifecycleError
        from bot.platform_paths import default_data_root

        venv_dir = default_data_root() / ".venv"
        try:
            transaction = _managed_install_transaction()
            with transaction:
                if not _venv_is_complete(venv_dir) or not _venv_uses_supported_python(venv_dir):
                    _recreate_venv(venv_dir)
                venv_python = _venv_python_path(venv_dir)
                if not venv_python.exists():
                    raise SystemExit(f"受管 .venv 不完整，缺少解释器：{venv_python}")
                if not _venv_uses_supported_python(venv_dir):
                    raise SystemExit(
                        "受管 .venv 重建后仍不是 CPython 3.11+；"
                        "请检查 FOCUS data root、文件权限与所选 Python 后重试。"
                    )
                _ensure_venv_pip(venv_python)
                _run_pip_install(
                    venv_python,
                    "--constraint",
                    str(bundle.dependency_lock_path),
                    "--force-reinstall",
                    str(bundle.wheel_path),
                )
                command = [str(venv_python), "-I", "-m", "bot.manage_cli"]
                if args.migrate_from_feishu_codex:
                    _run_checked([*command, "migrate", "from-feishu-codex"])
                else:
                    _run_checked([*command, "bootstrap-install"])
        except ManagedInstallLifecycleError as exc:
            raise SystemExit(str(exc)) from exc

    print("安装完成。")
    print(
        "已安装 Focus "
        f"{bundle.metadata.version} ({bundle.metadata.channel}/{bundle.metadata.build_id})。"
    )
    if transaction.restored_instances:
        print(f"已恢复原运行实例: {', '.join(transaction.restored_instances)}")
    else:
        print("没有原运行实例需要恢复；service 运行状态保持不变。")


if __name__ == "__main__":
    main(sys.argv[1:])
