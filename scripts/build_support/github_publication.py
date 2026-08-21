"""Publish validated Focus bundles to one repository Release authority."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from scripts.build_support.install_bundle import (
    CHANNEL_MANIFEST_NAMES,
    InstallBundleError,
    ChannelManifest,
    parse_channel_manifest,
    sha256_file,
    validate_install_bundle,
)


DEVELOPMENT_RELEASE_TAG = "development-builds"
DEFAULT_REPOSITORY = "ZichaoLong/focus"
DEFAULT_DEVELOPMENT_RETENTION = 5

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_BUNDLE_ASSET = re.compile(r"focus-install-.+\.zip\Z")


class GitHubPublicationError(RuntimeError):
    """Raised when publication cannot prove a safe or complete outcome."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    size: int
    digest: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ReleaseState:
    tag: str
    draft: bool
    prerelease: bool
    assets: tuple[ReleaseAsset, ...]

    def asset(self, name: str) -> ReleaseAsset | None:
        matches = tuple(asset for asset in self.assets if asset.name == name)
        if len(matches) > 1:
            raise GitHubPublicationError(
                f"GitHub Release包含重复asset name，无法判定authority：{name}"
            )
        return matches[0] if matches else None


@dataclass(frozen=True, slots=True)
class PublicationInput:
    channel: str
    bundle_path: pathlib.Path
    channel_manifest_path: pathlib.Path
    manifest: ChannelManifest


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    stdout: str
    stderr: str


class GitHubReleaseClient:
    """Small ``gh`` adapter with read-back reconciliation after writes."""

    def __init__(self, *, repository: str = DEFAULT_REPOSITORY) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise GitHubPublicationError("GitHub repository必须是owner/name")
        self.repository = repository

    def _run(self, *args: str) -> _CommandResult:
        try:
            result = subprocess.run(
                ["gh", *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise GitHubPublicationError("无法执行GitHub CLI `gh`") from exc
        return _CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    @staticmethod
    def _json_object(result: _CommandResult, *, operation: str) -> dict[str, Any]:
        if result.returncode != 0:
            raise GitHubPublicationError(
                f"GitHub {operation}失败：{result.stderr.strip() or 'unknown error'}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubPublicationError(f"GitHub {operation}没有返回有效JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubPublicationError(f"GitHub {operation}响应必须是object")
        return payload

    def release(self, tag: str) -> ReleaseState | None:
        result = self._run(
            "api",
            f"repos/{self.repository}/releases/tags/{tag}",
        )
        if result.returncode != 0:
            if "HTTP 404" in result.stderr:
                return None
            raise GitHubPublicationError(
                f"无法读取GitHub Release {tag}：{result.stderr.strip() or 'unknown error'}"
            )
        payload = self._json_object(result, operation=f"Release {tag}读取")
        if (
            payload.get("tag_name") != tag
            or type(payload.get("draft")) is not bool
            or type(payload.get("prerelease")) is not bool
            or not isinstance(payload.get("assets"), list)
        ):
            raise GitHubPublicationError(f"GitHub Release {tag}响应字段无效")
        assets: list[ReleaseAsset] = []
        for raw_asset in payload["assets"]:
            if not isinstance(raw_asset, dict):
                raise GitHubPublicationError(f"GitHub Release {tag} asset不是object")
            name = raw_asset.get("name")
            size = raw_asset.get("size")
            digest = raw_asset.get("digest")
            created_at = raw_asset.get("created_at", "")
            if (
                not isinstance(name, str)
                or not name
                or type(size) is not int
                or size <= 0
                or (digest is not None and not isinstance(digest, str))
                or not isinstance(created_at, str)
            ):
                raise GitHubPublicationError(f"GitHub Release {tag} asset字段无效")
            assets.append(
                ReleaseAsset(
                    name=name,
                    size=size,
                    digest=digest,
                    created_at=created_at,
                )
            )
        return ReleaseState(
            tag=tag,
            draft=payload["draft"],
            prerelease=payload["prerelease"],
            assets=tuple(assets),
        )

    def ensure_development_release(self) -> ReleaseState:
        release = self.release(DEVELOPMENT_RELEASE_TAG)
        if release is None:
            result = self._run(
                "release",
                "create",
                DEVELOPMENT_RELEASE_TAG,
                "--repo",
                self.repository,
                "--target",
                "main",
                "--title",
                "Focus development builds",
                "--notes",
                (
                    "Explicitly published installable development bundles. "
                    "Use `install.sh --channel development`."
                ),
                "--prerelease",
            )
            # A timeout or transport failure can still have committed the
            # release. Read back the authoritative state before classifying it.
            release = self.release(DEVELOPMENT_RELEASE_TAG)
            if release is None:
                raise GitHubPublicationError(
                    "创建development Release后无法证明其存在："
                    f"{result.stderr.strip() or 'unknown outcome'}"
                )
        self._validate_release(release, channel="development")
        comparison = self._json_object(
            self._run(
                "api",
                f"repos/{self.repository}/compare/{DEVELOPMENT_RELEASE_TAG}...main",
            ),
            operation="development tag与main ancestry比较",
        )
        if comparison.get("status") not in {"behind", "identical"} or comparison.get(
            "ahead_by"
        ) != 0:
            raise GitHubPublicationError(
                "development-builds tag必须指向main历史，不能锚住feature历史"
            )
        return release

    @staticmethod
    def _validate_release(release: ReleaseState, *, channel: str) -> None:
        if release.draft:
            raise GitHubPublicationError("不能向draft Release发布install bundle")
        if channel == "stable" and release.prerelease:
            raise GitHubPublicationError("stable bundle不能发布到prerelease")
        if channel == "development" and (
            not release.prerelease or release.tag != DEVELOPMENT_RELEASE_TAG
        ):
            raise GitHubPublicationError(
                "development bundle只能发布到固定development prerelease"
            )

    def stable_release(self, tag: str) -> ReleaseState:
        release = self.release(tag)
        if release is None:
            raise GitHubPublicationError(
                f"stable Release {tag}不存在；请先显式创建正式Release"
            )
        self._validate_release(release, channel="stable")
        return release

    def _download_asset(self, *, tag: str, name: str, output_dir: pathlib.Path) -> pathlib.Path:
        result = self._run(
            "release",
            "download",
            tag,
            "--repo",
            self.repository,
            "--pattern",
            name,
            "--dir",
            str(output_dir),
        )
        if result.returncode != 0:
            raise GitHubPublicationError(
                f"无法下载GitHub asset {name}进行reconciliation："
                f"{result.stderr.strip() or 'unknown error'}"
            )
        path = output_dir / name
        if not path.is_file():
            raise GitHubPublicationError(f"GitHub asset下载后不存在：{name}")
        return path

    def asset_matches(
        self,
        *,
        release: ReleaseState,
        asset: ReleaseAsset,
        local_path: pathlib.Path,
    ) -> bool:
        if asset.size != local_path.stat().st_size:
            return False
        local_digest = sha256_file(local_path)
        if asset.digest is not None:
            return asset.digest == f"sha256:{local_digest}"
        with tempfile.TemporaryDirectory(
            prefix="focus-release-reconcile-",
            ignore_cleanup_errors=True,
        ) as raw_dir:
            remote = self._download_asset(
                tag=release.tag,
                name=asset.name,
                output_dir=pathlib.Path(raw_dir),
            )
            return sha256_file(remote) == local_digest

    def upload_asset(
        self,
        *,
        release: ReleaseState,
        local_path: pathlib.Path,
        clobber: bool,
    ) -> ReleaseState:
        arguments = [
            "release",
            "upload",
            release.tag,
            str(local_path),
            "--repo",
            self.repository,
        ]
        if clobber:
            arguments.append("--clobber")
        result = self._run(*arguments)
        refreshed = self.release(release.tag)
        if refreshed is None:
            raise GitHubPublicationError(
                f"上传{local_path.name}后Release消失，external outcome未知"
            )
        remote = refreshed.asset(local_path.name)
        if remote is None or not self.asset_matches(
            release=refreshed,
            asset=remote,
            local_path=local_path,
        ):
            raise GitHubPublicationError(
                f"上传{local_path.name}后无法reconcile相同内容："
                f"{result.stderr.strip() or 'unknown outcome'}"
            )
        return refreshed

    def delete_asset_best_effort(self, *, tag: str, name: str) -> str | None:
        result = self._run(
            "release",
            "delete-asset",
            tag,
            name,
            "--repo",
            self.repository,
            "--yes",
        )
        if result.returncode == 0:
            return None
        return result.stderr.strip() or "unknown cleanup error"


def validate_publication_input(
    *,
    channel: str,
    bundle_path: pathlib.Path,
    channel_manifest_path: pathlib.Path,
) -> PublicationInput:
    if channel not in {"stable", "development"}:
        raise GitHubPublicationError("publication channel必须是stable或development")
    bundle_path = bundle_path.resolve()
    channel_manifest_path = channel_manifest_path.resolve()
    if channel_manifest_path.name != CHANNEL_MANIFEST_NAMES[channel]:
        raise GitHubPublicationError("channel manifest文件名与publication channel不一致")
    try:
        manifest = parse_channel_manifest(
            channel_manifest_path.read_bytes(),
            expected_channel=channel,
        )
    except (OSError, InstallBundleError) as exc:
        raise GitHubPublicationError(str(exc)) from exc
    if manifest.bundle.name != bundle_path.name:
        raise GitHubPublicationError("channel manifest没有指向传入bundle")
    if not bundle_path.is_file() or bundle_path.stat().st_size != manifest.bundle.size:
        raise GitHubPublicationError("传入bundle的size与channel manifest不一致")
    if sha256_file(bundle_path) != manifest.bundle.sha256:
        raise GitHubPublicationError("传入bundle的SHA-256与channel manifest不一致")
    if _COMMIT_SHA.fullmatch(manifest.source_revision) is None:
        raise GitHubPublicationError("发布制品的source_revision必须是完整40位commit SHA")
    if channel == "development" and manifest.release_tag != DEVELOPMENT_RELEASE_TAG:
        raise GitHubPublicationError("development publication必须使用固定release tag")
    if channel == "stable":
        normalized_tag = (
            manifest.release_tag[1:]
            if manifest.release_tag.startswith("v")
            else manifest.release_tag
        )
        if normalized_tag != manifest.version:
            raise GitHubPublicationError("stable release tag与bundle version不一致")
    with tempfile.TemporaryDirectory(
        prefix="focus-publication-preflight-",
        ignore_cleanup_errors=True,
    ) as raw_dir:
        try:
            validate_install_bundle(
                bundle_path,
                extraction_dir=pathlib.Path(raw_dir),
                expected_channel=manifest.channel,
                expected_version=manifest.version,
                expected_build_id=manifest.build_id,
                expected_source_revision=manifest.source_revision,
            )
        except InstallBundleError as exc:
            raise GitHubPublicationError(str(exc)) from exc
    return PublicationInput(
        channel=channel,
        bundle_path=bundle_path,
        channel_manifest_path=channel_manifest_path,
        manifest=manifest,
    )


def _publish_one(
    *,
    client: GitHubReleaseClient,
    release: ReleaseState,
    local_path: pathlib.Path,
    replace: bool,
) -> ReleaseState:
    existing = release.asset(local_path.name)
    if existing is not None:
        if client.asset_matches(
            release=release,
            asset=existing,
            local_path=local_path,
        ):
            return release
        if not replace:
            raise GitHubPublicationError(
                f"Release已有不同内容的immutable asset：{local_path.name}"
            )
    return client.upload_asset(
        release=release,
        local_path=local_path,
        clobber=replace,
    )


def publish_install_bundle(
    publication: PublicationInput,
    *,
    client: GitHubReleaseClient,
    development_retention: int = DEFAULT_DEVELOPMENT_RETENTION,
) -> tuple[str, ...]:
    if type(development_retention) is not int or development_retention <= 0:
        raise GitHubPublicationError("development retention必须是正整数")
    if publication.channel == "development":
        release = client.ensure_development_release()
    else:
        release = client.stable_release(publication.manifest.release_tag)

    # Unique bundle first. Its presence alone is not channel authority.
    release = _publish_one(
        client=client,
        release=release,
        local_path=publication.bundle_path,
        replace=False,
    )
    # The descriptor is the publication commit point. Development may replace
    # only this one pointer; stable descriptors are immutable.
    release = _publish_one(
        client=client,
        release=release,
        local_path=publication.channel_manifest_path,
        replace=publication.channel == "development",
    )

    cleanup_warnings: list[str] = []
    if publication.channel == "development":
        bundles = [
            asset
            for asset in release.assets
            if _BUNDLE_ASSET.fullmatch(asset.name) is not None
        ]
        current = publication.bundle_path.name
        ordered = sorted(
            (asset for asset in bundles if asset.name != current),
            key=lambda asset: (asset.created_at, asset.name),
            reverse=True,
        )
        keep = {asset.name for asset in ([release.asset(current)] + ordered)[:development_retention] if asset}
        for asset in bundles:
            if asset.name in keep:
                continue
            warning = client.delete_asset_best_effort(
                tag=release.tag,
                name=asset.name,
            )
            if warning is not None:
                cleanup_warnings.append(f"{asset.name}: {warning}")
    return tuple(cleanup_warnings)
