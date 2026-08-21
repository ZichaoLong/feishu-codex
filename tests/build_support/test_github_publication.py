from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts.build_support.github_publication import (
    DEVELOPMENT_RELEASE_TAG,
    GitHubPublicationError,
    ReleaseAsset,
    ReleaseState,
    publish_install_bundle,
    validate_publication_input,
)
from scripts.build_support.install_bundle import build_install_bundle, sha256_file


class _FakeGitHubClient:
    def __init__(self, release: ReleaseState, stored: dict[str, bytes]) -> None:
        self.current = release
        self.stored = dict(stored)
        self.uploads: list[tuple[str, bool]] = []
        self.deletions: list[str] = []
        self.cleanup_failure: str | None = None

    def ensure_development_release(self) -> ReleaseState:
        return self.current

    def stable_release(self, tag: str) -> ReleaseState:
        if self.current.tag != tag:
            raise AssertionError((self.current.tag, tag))
        return self.current

    def asset_matches(
        self,
        *,
        release: ReleaseState,
        asset: ReleaseAsset,
        local_path: pathlib.Path,
    ) -> bool:
        del release
        return self.stored.get(asset.name) == local_path.read_bytes()

    def upload_asset(
        self,
        *,
        release: ReleaseState,
        local_path: pathlib.Path,
        clobber: bool,
    ) -> ReleaseState:
        self.uploads.append((local_path.name, clobber))
        self.stored[local_path.name] = local_path.read_bytes()
        replacement = ReleaseAsset(
            name=local_path.name,
            size=local_path.stat().st_size,
            digest=f"sha256:{sha256_file(local_path)}",
            created_at=f"9999-01-01T00:00:{len(self.uploads):02d}Z",
        )
        assets = [item for item in release.assets if item.name != local_path.name]
        assets.append(replacement)
        self.current = ReleaseState(
            tag=release.tag,
            draft=release.draft,
            prerelease=release.prerelease,
            assets=tuple(assets),
        )
        return self.current

    def delete_asset_best_effort(self, *, tag: str, name: str) -> str | None:
        if tag != self.current.tag:
            raise AssertionError((tag, self.current.tag))
        self.deletions.append(name)
        if self.cleanup_failure is not None:
            return self.cleanup_failure
        self.stored.pop(name, None)
        self.current = ReleaseState(
            tag=self.current.tag,
            draft=self.current.draft,
            prerelease=self.current.prerelease,
            assets=tuple(item for item in self.current.assets if item.name != name),
        )
        return None


class GitHubPublicationTests(unittest.TestCase):
    @staticmethod
    def _write_source(root: pathlib.Path) -> pathlib.Path:
        source = root / "source"
        dist = source / "bot" / "web_assets" / "dist"
        dist.mkdir(parents=True)
        for name in (
            "index.html",
            "THIRD_PARTY_NOTICES.html",
            "THIRD_PARTY_NOTICES.md",
            "THIRD_PARTY_SBOM.json",
        ):
            (dist / name).write_text(name, encoding="utf-8")
        (dist.parent / "THIRD_PARTY_NOTICES.md").write_text("notice", encoding="utf-8")
        (source / "requirements.lock").write_text("aiohttp==3.14.3\n", encoding="utf-8")
        return source

    @staticmethod
    def _wheel_builder(version: str):
        def build(**kwargs) -> pathlib.Path:
            wheel = kwargs["output_dir"] / f"focus-{version}-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("bot/__init__.py", "")
                for name in (
                    "bot/web_assets/dist/index.html",
                    "bot/web_assets/dist/THIRD_PARTY_NOTICES.html",
                    "bot/web_assets/dist/THIRD_PARTY_NOTICES.md",
                    "bot/web_assets/dist/THIRD_PARTY_SBOM.json",
                    "bot/web_assets/THIRD_PARTY_NOTICES.md",
                ):
                    archive.writestr(name, name)
                archive.writestr(
                    f"focus-{version}.dist-info/METADATA",
                    f"Metadata-Version: 2.4\nName: focus\nVersion: {version}\n\n",
                )
            return wheel

        return build

    def _publication(self, root: pathlib.Path, *, channel: str):
        source = self._write_source(root)
        version = "4.0.0" if channel == "stable" else "4.0.0.dev0"
        release_tag = "4.0.0" if channel == "stable" else DEVELOPMENT_RELEASE_TAG
        with patch(
            "scripts.build_support.install_bundle.build_validated_wheel",
            side_effect=self._wheel_builder(version),
        ):
            built = build_install_bundle(
                source_dir=source,
                output_dir=root / "output",
                channel=channel,
                source_revision="a" * 40,
                build_id="build-1",
                release_tag=release_tag,
            )
        assert built.channel_manifest_path is not None
        return validate_publication_input(
            channel=channel,
            bundle_path=built.bundle_path,
            channel_manifest_path=built.channel_manifest_path,
        )

    @staticmethod
    def _release(
        *,
        channel: str,
        assets: tuple[ReleaseAsset, ...] = (),
    ) -> ReleaseState:
        return ReleaseState(
            tag="4.0.0" if channel == "stable" else DEVELOPMENT_RELEASE_TAG,
            draft=False,
            prerelease=channel == "development",
            assets=assets,
        )

    def test_development_publishes_bundle_then_pointer_and_retains_five(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="development")
            old_assets = tuple(
                ReleaseAsset(
                    name=f"focus-install-old-{index}.zip",
                    size=3,
                    digest=None,
                    created_at=f"2026-01-0{index + 1}T00:00:00Z",
                )
                for index in range(6)
            )
            old_manifest = ReleaseAsset(
                name=publication.channel_manifest_path.name,
                size=3,
                digest=None,
                created_at="2026-01-07T00:00:00Z",
            )
            stored = {asset.name: b"old" for asset in (*old_assets, old_manifest)}
            client = _FakeGitHubClient(
                self._release(
                    channel="development",
                    assets=(*old_assets, old_manifest),
                ),
                stored,
            )

            warnings = publish_install_bundle(publication, client=client)

        self.assertEqual(warnings, ())
        self.assertEqual(
            client.uploads,
            [
                (publication.bundle_path.name, False),
                (publication.channel_manifest_path.name, True),
            ],
        )
        self.assertEqual(client.deletions, ["focus-install-old-0.zip", "focus-install-old-1.zip"])

    def test_stable_publication_never_overwrites_different_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="stable")
            existing = ReleaseAsset(
                name=publication.bundle_path.name,
                size=5,
                digest=None,
                created_at="2026-01-01T00:00:00Z",
            )
            client = _FakeGitHubClient(
                self._release(channel="stable", assets=(existing,)),
                {existing.name: b"wrong"},
            )
            with self.assertRaisesRegex(GitHubPublicationError, "immutable"):
                publish_install_bundle(publication, client=client)
        self.assertEqual(client.uploads, [])

    def test_stable_publication_is_idempotent_for_identical_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="stable")
            paths = (publication.bundle_path, publication.channel_manifest_path)
            assets = tuple(
                ReleaseAsset(
                    name=path.name,
                    size=path.stat().st_size,
                    digest=f"sha256:{sha256_file(path)}",
                    created_at="2026-01-01T00:00:00Z",
                )
                for path in paths
            )
            client = _FakeGitHubClient(
                self._release(channel="stable", assets=assets),
                {path.name: path.read_bytes() for path in paths},
            )
            warnings = publish_install_bundle(publication, client=client)
        self.assertEqual(warnings, ())
        self.assertEqual(client.uploads, [])

    def test_cleanup_failure_does_not_revoke_published_development_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="development")
            old_assets = tuple(
                ReleaseAsset(
                    name=f"focus-install-old-{index}.zip",
                    size=3,
                    digest=None,
                    created_at=f"2025-01-0{index + 1}T00:00:00Z",
                )
                for index in range(5)
            )
            client = _FakeGitHubClient(
                self._release(channel="development", assets=old_assets),
                {asset.name: b"old" for asset in old_assets},
            )
            client.cleanup_failure = "permission denied"
            warnings = publish_install_bundle(publication, client=client)
        self.assertEqual(len(warnings), 1)
        self.assertIn("permission denied", warnings[0])
        self.assertEqual(client.uploads[-1][0], publication.channel_manifest_path.name)

    def test_publication_preflight_rejects_dirty_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="development")
            payload = json.loads(publication.channel_manifest_path.read_text(encoding="utf-8"))
            payload["source_revision"] = "a" * 40 + "+dirty"
            publication.channel_manifest_path.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(GitHubPublicationError, "40位commit SHA"):
                validate_publication_input(
                    channel="development",
                    bundle_path=publication.bundle_path,
                    channel_manifest_path=publication.channel_manifest_path,
                )


if __name__ == "__main__":
    unittest.main()
