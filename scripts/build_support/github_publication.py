"""Publish validated Focus bundles to one repository Release authority."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass
from typing import Any

from scripts.build_support.install_bundle import (
    CHANNEL_MANIFEST_NAMES,
    InstallBundleError,
    ChannelManifest,
    development_release_tag,
    is_development_release_tag,
    parse_channel_manifest,
    sha256_file,
    validate_install_bundle,
)


DEFAULT_REPOSITORY = "ZichaoLong/focus"
DEFAULT_DEVELOPMENT_RETENTION = 5

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


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
    release_id: int
    tag: str
    target_commitish: str
    draft: bool
    prerelease: bool
    published_at: str | None
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

    @staticmethod
    def _json_array(result: _CommandResult, *, operation: str) -> list[Any]:
        if result.returncode != 0:
            raise GitHubPublicationError(
                f"GitHub {operation}失败：{result.stderr.strip() or 'unknown error'}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubPublicationError(f"GitHub {operation}没有返回有效JSON") from exc
        if not isinstance(payload, list):
            raise GitHubPublicationError(f"GitHub {operation}响应必须是array")
        return payload

    @staticmethod
    def _release_state(payload: dict[str, Any], *, expected_tag: str | None = None) -> ReleaseState:
        release_id = payload.get("id")
        tag = payload.get("tag_name")
        target_commitish = payload.get("target_commitish")
        published_at = payload.get("published_at")
        if (
            type(release_id) is not int
            or release_id <= 0
            or not isinstance(tag, str)
            or not tag
            or (expected_tag is not None and tag != expected_tag)
            or not isinstance(target_commitish, str)
            or not target_commitish
            or type(payload.get("draft")) is not bool
            or type(payload.get("prerelease")) is not bool
            or (published_at is not None and not isinstance(published_at, str))
            or not isinstance(payload.get("assets"), list)
        ):
            raise GitHubPublicationError(
                f"GitHub Release {expected_tag or tag or '<unknown>'}响应字段无效"
            )
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
            release_id=release_id,
            tag=tag,
            target_commitish=target_commitish,
            draft=payload["draft"],
            prerelease=payload["prerelease"],
            published_at=published_at,
            assets=tuple(assets),
        )

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
        return self._release_state(payload, expected_tag=tag)

    def tag_commit(self, tag: str) -> str | None:
        encoded_tag = urllib.parse.quote(tag, safe="")
        result = self._run(
            "api",
            f"repos/{self.repository}/git/ref/tags/{encoded_tag}",
        )
        if result.returncode != 0:
            if "HTTP 404" in result.stderr:
                return None
            raise GitHubPublicationError(
                f"无法解析GitHub tag {tag}：{result.stderr.strip() or 'unknown error'}"
            )
        payload = self._json_object(result, operation=f"tag {tag} ref读取")
        if payload.get("ref") != f"refs/tags/{tag}":
            raise GitHubPublicationError(f"GitHub tag {tag} ref与请求不一致")
        git_object = payload.get("object")
        for _ in range(5):
            if not isinstance(git_object, dict):
                raise GitHubPublicationError(f"GitHub tag {tag}缺少git object")
            object_type = git_object.get("type")
            revision = git_object.get("sha")
            if (
                not isinstance(revision, str)
                or _COMMIT_SHA.fullmatch(revision) is None
            ):
                raise GitHubPublicationError(f"GitHub tag {tag} object缺少完整SHA")
            if object_type == "commit":
                return revision
            if object_type != "tag":
                raise GitHubPublicationError(f"GitHub tag {tag}没有解析到commit")
            tag_payload = self._json_object(
                self._run(
                    "api",
                    f"repos/{self.repository}/git/tags/{revision}",
                ),
                operation=f"annotated tag {revision}读取",
            )
            git_object = tag_payload.get("object")
        raise GitHubPublicationError(f"GitHub tag {tag} annotated tag嵌套过深")

    def _validate_development_release(
        self,
        release: ReleaseState,
        *,
        source_revision: str,
        allow_draft: bool,
    ) -> None:
        if not is_development_release_tag(release.tag) or not release.prerelease:
            raise GitHubPublicationError(
                "development bundle只能发布到唯一命名的prerelease"
            )
        if release.draft and not allow_draft:
            raise GitHubPublicationError("development Release仍是draft")
        if release.target_commitish != source_revision:
            raise GitHubPublicationError(
                "development Release target_commitish与source_revision不一致"
            )
        if not release.draft:
            if not release.published_at:
                raise GitHubPublicationError("development prerelease缺少published_at")
            if self.tag_commit(release.tag) != source_revision:
                raise GitHubPublicationError(
                    "development Release tag与source_revision不一致"
                )

    def development_release(
        self,
        *,
        tag: str,
        source_revision: str,
        build_id: str,
    ) -> ReleaseState:
        if tag != development_release_tag(build_id):
            raise GitHubPublicationError("development Release tag与build_id不一致")
        existing_revision = self.tag_commit(tag)
        if existing_revision is not None and existing_revision != source_revision:
            raise GitHubPublicationError(
                "development Release tag已指向其他source revision"
            )
        release = self.release(tag)
        if release is None:
            result = self._run(
                "release",
                "create",
                tag,
                "--repo",
                self.repository,
                "--target",
                source_revision,
                "--title",
                f"Focus development build {build_id}",
                "--notes",
                (
                    "Explicitly published installable development build. "
                    f"Source revision: `{source_revision}`. "
                    "Use `install.sh --channel development`."
                ),
                "--draft",
                "--prerelease",
            )
            # A transport failure can still have created the draft. Read it
            # back before classifying the external outcome.
            release = self.release(tag)
            if release is None:
                raise GitHubPublicationError(
                    "创建development draft后无法证明其存在："
                    f"{result.stderr.strip() or 'unknown outcome'}"
                )
        self._validate_development_release(
            release,
            source_revision=source_revision,
            allow_draft=True,
        )
        return release

    def publish_development_release(
        self,
        release: ReleaseState,
        *,
        source_revision: str,
    ) -> ReleaseState:
        if release.draft:
            result = self._run(
                "release",
                "edit",
                release.tag,
                "--repo",
                self.repository,
                "--draft=false",
                "--prerelease",
            )
            refreshed = self.release(release.tag)
            if refreshed is None:
                raise GitHubPublicationError(
                    "发布development prerelease后无法证明其存在："
                    f"{result.stderr.strip() or 'unknown outcome'}"
                )
            release = refreshed
        self._validate_development_release(
            release,
            source_revision=source_revision,
            allow_draft=False,
        )
        return release

    def stable_release(self, tag: str, *, source_revision: str) -> ReleaseState:
        release = self.release(tag)
        if release is None:
            raise GitHubPublicationError(
                f"stable Release {tag}不存在；请先显式创建正式Release"
            )
        if release.draft or release.prerelease:
            raise GitHubPublicationError("stable bundle只能发布到正式非draft Release")
        if self.tag_commit(tag) != source_revision:
            raise GitHubPublicationError("stable Release tag与source_revision不一致")
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
    ) -> ReleaseState:
        result = self._run(
            "release",
            "upload",
            release.tag,
            str(local_path),
            "--repo",
            self.repository,
        )
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

    def development_releases(self) -> tuple[ReleaseState, ...]:
        payload = self._json_array(
            self._run(
                "api",
                f"repos/{self.repository}/releases?per_page=100",
            ),
            operation="development Release列表读取",
        )
        releases: list[ReleaseState] = []
        for raw_release in payload:
            if not isinstance(raw_release, dict):
                raise GitHubPublicationError("GitHub Release列表包含非object条目")
            tag = raw_release.get("tag_name")
            if not is_development_release_tag(tag):
                continue
            release = self._release_state(raw_release, expected_tag=tag)
            if release.draft:
                continue
            if not release.prerelease or not release.published_at:
                raise GitHubPublicationError(
                    f"development tag {release.tag}不是完整published prerelease"
                )
            releases.append(release)
        return tuple(releases)

    def delete_development_release_best_effort(
        self,
        release: ReleaseState,
    ) -> str | None:
        result = self._run(
            "release",
            "delete",
            release.tag,
            "--repo",
            self.repository,
            "--cleanup-tag",
            "--yes",
        )
        try:
            remaining_release = self.release(release.tag)
            remaining_revision = self.tag_commit(release.tag)
        except GitHubPublicationError as exc:
            return str(exc)
        if remaining_release is None and remaining_revision is None:
            return None
        state = []
        if remaining_release is not None:
            state.append("Release remains")
        if remaining_revision is not None:
            state.append("tag remains")
        details = result.stderr.strip() or "unknown cleanup outcome"
        return f"{details}; {', '.join(state)}"


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
    if channel == "development" and manifest.release_tag != development_release_tag(
        manifest.build_id
    ):
        raise GitHubPublicationError("development release tag必须与build_id一致")
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
) -> ReleaseState:
    existing = release.asset(local_path.name)
    if existing is not None:
        if client.asset_matches(
            release=release,
            asset=existing,
            local_path=local_path,
        ):
            return release
        raise GitHubPublicationError(
            f"Release已有不同内容的immutable asset：{local_path.name}"
        )
    return client.upload_asset(
        release=release,
        local_path=local_path,
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
        release = client.development_release(
            tag=publication.manifest.release_tag,
            source_revision=publication.manifest.source_revision,
            build_id=publication.manifest.build_id,
        )
    else:
        release = client.stable_release(
            publication.manifest.release_tag,
            source_revision=publication.manifest.source_revision,
        )

    # Unique bundle first. Its presence alone is not channel authority.
    release = _publish_one(
        client=client,
        release=release,
        local_path=publication.bundle_path,
    )
    # Both assets are immutable. A development build remains hidden as a draft
    # until both have been uploaded and reconciled.
    release = _publish_one(
        client=client,
        release=release,
        local_path=publication.channel_manifest_path,
    )

    cleanup_warnings: list[str] = []
    if publication.channel == "development":
        expected_assets = {
            publication.bundle_path.name,
            publication.channel_manifest_path.name,
        }
        actual_assets = {asset.name for asset in release.assets}
        if len(release.assets) != len(expected_assets) or actual_assets != expected_assets:
            raise GitHubPublicationError(
                "development draft必须只包含当前bundle与channel manifest"
            )
        release = client.publish_development_release(
            release,
            source_revision=publication.manifest.source_revision,
        )
        try:
            retained_releases = client.development_releases()
        except GitHubPublicationError as exc:
            cleanup_warnings.append(f"retention discovery: {exc}")
            return tuple(cleanup_warnings)
        existing = {
            item.tag: item
            for item in (*retained_releases, release)
        }
        ordered = sorted(
            (item for item in existing.values() if item.tag != release.tag),
            key=lambda item: (item.published_at or "", item.release_id),
            reverse=True,
        )
        keep = {release.tag}
        keep.update(item.tag for item in ordered[: development_retention - 1])
        for old_release in existing.values():
            if old_release.tag in keep:
                continue
            try:
                warning = client.delete_development_release_best_effort(old_release)
            except GitHubPublicationError as exc:
                warning = str(exc)
            if warning is not None:
                cleanup_warnings.append(f"{old_release.tag}: {warning}")
    return tuple(cleanup_warnings)
