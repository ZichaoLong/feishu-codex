from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from scripts.build_support.github_publication import (
    GitHubPublicationError,
    GitHubReleaseClient,
    ReleaseAsset,
    ReleaseState,
    publish_install_bundle,
    validate_publication_input,
)
from scripts.build_support.install_bundle import (
    build_install_bundle,
    development_release_tag,
    sha256_file,
)


class _FakeGitHubClient:
    def __init__(
        self,
        release: ReleaseState,
        stored: dict[str, bytes],
        *,
        historical: tuple[ReleaseState, ...] = (),
    ) -> None:
        self.current = release
        self.stored = dict(stored)
        self.historical = historical
        self.events: list[str] = []
        self.uploads: list[str] = []
        self.deletions: list[str] = []
        self.cleanup_failure: str | None = None
        self.retention_failure: str | None = None

    def development_release(
        self,
        *,
        tag: str,
        source_revision: str,
        build_id: str,
    ) -> ReleaseState:
        if tag != development_release_tag(build_id):
            raise AssertionError((tag, build_id))
        if self.current.tag != tag or self.current.target_commitish != source_revision:
            raise AssertionError((self.current, tag, source_revision))
        self.events.append("open-draft")
        return self.current

    def stable_release(self, tag: str, *, source_revision: str) -> ReleaseState:
        if self.current.tag != tag or self.current.target_commitish != source_revision:
            raise AssertionError((self.current, tag, source_revision))
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
    ) -> ReleaseState:
        self.events.append(f"upload:{local_path.name}")
        self.uploads.append(local_path.name)
        self.stored[local_path.name] = local_path.read_bytes()
        replacement = ReleaseAsset(
            name=local_path.name,
            size=local_path.stat().st_size,
            digest=f"sha256:{sha256_file(local_path)}",
            created_at=f"9999-01-01T00:00:{len(self.uploads):02d}Z",
        )
        assets = [item for item in release.assets if item.name != local_path.name]
        assets.append(replacement)
        self.current = replace(release, assets=tuple(assets))
        return self.current

    def publish_development_release(
        self,
        release: ReleaseState,
        *,
        source_revision: str,
    ) -> ReleaseState:
        if release.target_commitish != source_revision:
            raise AssertionError((release.target_commitish, source_revision))
        self.events.append("publish")
        self.current = replace(
            release,
            draft=False,
            published_at="9999-01-01T00:00:00Z",
        )
        return self.current

    def development_releases(self) -> tuple[ReleaseState, ...]:
        if self.retention_failure is not None:
            raise GitHubPublicationError(self.retention_failure)
        return (*self.historical, self.current)

    def delete_development_release_best_effort(
        self,
        release: ReleaseState,
    ) -> str | None:
        self.deletions.append(release.tag)
        if self.cleanup_failure is not None:
            return self.cleanup_failure
        self.historical = tuple(
            item for item in self.historical if item.tag != release.tag
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
        release_tag = "4.0.0" if channel == "stable" else None
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
        release_id: int = 1,
        tag: str | None = None,
        draft: bool = False,
        published_at: str | None = "2026-01-01T00:00:00Z",
    ) -> ReleaseState:
        return ReleaseState(
            release_id=release_id,
            tag=tag
            or (
                "4.0.0"
                if channel == "stable"
                else development_release_tag("build-1")
            ),
            target_commitish="a" * 40,
            draft=draft,
            prerelease=channel == "development",
            published_at=published_at,
            assets=assets,
        )

    def test_development_publishes_bundle_then_pointer_and_retains_five(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="development")
            old_releases = tuple(
                self._release(
                    channel="development",
                    release_id=index + 10,
                    tag=development_release_tag(f"old-{index}"),
                    published_at=f"2026-01-0{index + 1}T00:00:00Z",
                )
                for index in range(6)
            )
            client = _FakeGitHubClient(
                self._release(
                    channel="development",
                    draft=True,
                    published_at=None,
                ),
                {},
                historical=old_releases,
            )

            warnings = publish_install_bundle(publication, client=client)

        self.assertEqual(warnings, ())
        self.assertEqual(
            client.uploads,
            [
                publication.bundle_path.name,
                publication.channel_manifest_path.name,
            ],
        )
        self.assertEqual(client.events[-1], "publish")
        self.assertEqual(
            client.deletions,
            [
                development_release_tag("old-0"),
                development_release_tag("old-1"),
            ],
        )

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

    def test_development_draft_rejects_unowned_assets_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="development")
            extra = ReleaseAsset(
                name="unowned.txt",
                size=5,
                digest=None,
                created_at="2026-01-01T00:00:00Z",
            )
            client = _FakeGitHubClient(
                self._release(
                    channel="development",
                    draft=True,
                    published_at=None,
                    assets=(extra,),
                ),
                {extra.name: b"extra"},
            )

            with self.assertRaisesRegex(GitHubPublicationError, "只包含"):
                publish_install_bundle(publication, client=client)

        self.assertNotIn("publish", client.events)

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
            old_releases = tuple(
                self._release(
                    channel="development",
                    release_id=index + 10,
                    tag=development_release_tag(f"old-{index}"),
                    published_at=f"2025-01-0{index + 1}T00:00:00Z",
                )
                for index in range(5)
            )
            client = _FakeGitHubClient(
                self._release(
                    channel="development",
                    draft=True,
                    published_at=None,
                ),
                {},
                historical=old_releases,
            )
            client.cleanup_failure = "permission denied"
            warnings = publish_install_bundle(publication, client=client)
        self.assertEqual(len(warnings), 1)
        self.assertIn("permission denied", warnings[0])
        self.assertFalse(client.current.draft)
        self.assertEqual(client.events[-1], "publish")

    def test_retention_discovery_failure_is_warning_after_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            publication = self._publication(root, channel="development")
            client = _FakeGitHubClient(
                self._release(
                    channel="development",
                    draft=True,
                    published_at=None,
                ),
                {},
            )
            client.retention_failure = "GitHub unavailable"

            warnings = publish_install_bundle(publication, client=client)

        self.assertEqual(len(warnings), 1)
        self.assertIn("retention discovery", warnings[0])
        self.assertFalse(client.current.draft)

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
            with self.assertRaisesRegex(GitHubPublicationError, "40位.*commit SHA"):
                validate_publication_input(
                    channel="development",
                    bundle_path=publication.bundle_path,
                    channel_manifest_path=publication.channel_manifest_path,
                )

    def test_github_client_creates_hidden_development_release_at_source(self) -> None:
        client = GitHubReleaseClient(repository="owner/repository")
        tag = development_release_tag("build-1")
        draft = self._release(
            channel="development",
            tag=tag,
            draft=True,
            published_at=None,
        )
        command_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(client, "tag_commit", return_value=None):
            with patch.object(client, "release", side_effect=(None, draft)):
                with patch.object(client, "_run", return_value=command_result) as run:
                    self.assertIs(
                        client.development_release(
                            tag=tag,
                            source_revision="a" * 40,
                            build_id="build-1",
                        ),
                        draft,
                    )

        arguments = run.call_args.args
        self.assertEqual(arguments[:3], ("release", "create", tag))
        self.assertEqual(arguments[arguments.index("--target") + 1], "a" * 40)
        self.assertIn("--draft", arguments)
        self.assertIn("--prerelease", arguments)

    def test_github_client_publishes_draft_before_accepting_tag_identity(self) -> None:
        client = GitHubReleaseClient(repository="owner/repository")
        draft = self._release(
            channel="development",
            draft=True,
            published_at=None,
        )
        published = replace(
            draft,
            draft=False,
            published_at="2026-08-21T10:00:00Z",
        )
        command_result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(client, "release", return_value=published):
            with patch.object(client, "tag_commit", return_value="a" * 40):
                with patch.object(client, "_run", return_value=command_result) as run:
                    self.assertIs(
                        client.publish_development_release(
                            draft,
                            source_revision="a" * 40,
                        ),
                        published,
                    )

        self.assertEqual(run.call_args.args[:3], ("release", "edit", draft.tag))
        self.assertIn("--draft=false", run.call_args.args)
        self.assertIn("--prerelease", run.call_args.args)

    def test_github_client_rejects_release_tag_at_another_commit(self) -> None:
        client = GitHubReleaseClient(repository="owner/repository")
        tag = development_release_tag("build-1")
        with patch.object(client, "tag_commit", return_value="b" * 40):
            with self.assertRaisesRegex(GitHubPublicationError, "其他source revision"):
                client.development_release(
                    tag=tag,
                    source_revision="a" * 40,
                    build_id="build-1",
                )

    def test_github_client_resolves_annotated_release_tag_to_commit(self) -> None:
        client = GitHubReleaseClient(repository="owner/repository")
        tag_sha = "b" * 40
        revision = "a" * 40
        responses = (
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ref": "refs/tags/4.0.0",
                        "object": {"type": "tag", "sha": tag_sha},
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"object": {"type": "commit", "sha": revision}}
                ),
                stderr="",
            ),
        )
        with patch.object(client, "_run", side_effect=responses) as run:
            self.assertEqual(client.tag_commit("4.0.0"), revision)

        self.assertIn("/git/ref/tags/4.0.0", run.call_args_list[0].args[1])
        self.assertIn(f"/git/tags/{tag_sha}", run.call_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
