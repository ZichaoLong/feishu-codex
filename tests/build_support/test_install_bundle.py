from __future__ import annotations

import json
import pathlib
import stat
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts.build_support.install_bundle import (
    BUNDLE_MANIFEST_NAME,
    CHANNEL_MANIFEST_NAMES,
    InstallBundleError,
    build_install_bundle,
    development_release_tag,
    parse_bundle_manifest,
    parse_channel_manifest,
    sha256_file,
    validate_install_bundle,
)


class InstallBundleTests(unittest.TestCase):
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
            (dist / name).write_text(f"{name}\n", encoding="utf-8")
        (dist.parent / "THIRD_PARTY_NOTICES.md").write_text(
            "notice\n",
            encoding="utf-8",
        )
        (source / "requirements.lock").write_text(
            "aiohttp==3.14.3\n",
            encoding="utf-8",
        )
        return source

    @staticmethod
    def _fake_wheel_builder(**kwargs) -> pathlib.Path:
        wheel = kwargs["output_dir"] / "focus-4.0.0.dev0-py3-none-any.whl"
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
                "focus-4.0.0.dev0.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: focus\nVersion: 4.0.0.dev0\n\n",
            )
        return wheel

    def _build(
        self,
        root: pathlib.Path,
        *,
        channel: str = "local",
        output_name: str = "output",
    ):
        source = root / "source"
        if not source.exists():
            source = self._write_source(root)
        kwargs = {}
        if channel == "stable":
            kwargs["release_tag"] = "4.0.0"
        with patch(
            "scripts.build_support.install_bundle.build_validated_wheel",
            side_effect=self._fake_wheel_builder,
        ):
            return build_install_bundle(
                source_dir=source,
                output_dir=root / output_name,
                channel=channel,
                source_revision="a" * 40,
                **kwargs,
            )

    def test_local_build_is_reproducible_and_validates_complete_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            first = self._build(root, output_name="first")
            second = self._build(root, output_name="second")
            self.assertIsNone(first.channel_manifest_path)
            self.assertEqual(first.bundle_path.name, second.bundle_path.name)
            self.assertEqual(first.bundle_path.read_bytes(), second.bundle_path.read_bytes())

            validated = validate_install_bundle(
                first.bundle_path,
                extraction_dir=root / "extracted",
            )

            self.assertEqual(validated.metadata.channel, "local")
            self.assertEqual(validated.metadata.version, "4.0.0.dev0")
            self.assertTrue(validated.wheel_path.is_file())
            self.assertEqual(
                validated.dependency_lock_path.read_text(encoding="utf-8"),
                "aiohttp==3.14.3\n",
            )

    def test_remote_build_emits_matching_closed_channel_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            built = self._build(root, channel="development")
            self.assertIsNotNone(built.channel_manifest_path)
            assert built.channel_manifest_path is not None
            self.assertEqual(
                built.channel_manifest_path.name,
                CHANNEL_MANIFEST_NAMES["development"],
            )
            parsed = parse_channel_manifest(
                built.channel_manifest_path.read_bytes(),
                expected_channel="development",
            )
            self.assertEqual(
                parsed.release_tag,
                development_release_tag(parsed.build_id),
            )
            self.assertEqual(parsed.bundle.name, built.bundle_path.name)
            self.assertEqual(parsed.bundle.size, built.bundle_path.stat().st_size)
            self.assertEqual(parsed.bundle.sha256, sha256_file(built.bundle_path))

    def test_build_requires_production_web_assets_before_wheel_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            source = root / "source"
            source.mkdir()
            (source / "requirements.lock").write_text(
                "aiohttp==3.14.3\n",
                encoding="utf-8",
            )
            with patch(
                "scripts.build_support.install_bundle.build_validated_wheel"
            ) as wheel_builder:
                with self.assertRaisesRegex(InstallBundleError, "npm run build"):
                    build_install_bundle(
                        source_dir=source,
                        output_dir=root / "output",
                        channel="local",
                        source_revision="a" * 40,
                    )
            wheel_builder.assert_not_called()

    def test_generated_web_delivery_paths_are_ignored_and_untracked(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        paths = (
            "bot/web_assets/dist/index.html",
            "bot/web_assets/THIRD_PARTY_NOTICES.md",
        )
        for relative in paths:
            with self.subTest(path=relative):
                ignored = subprocess.run(
                    ["git", "check-ignore", "--quiet", relative],
                    cwd=repo_root,
                    check=False,
                )
                tracked = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", relative],
                    cwd=repo_root,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.assertEqual(ignored.returncode, 0)
                self.assertNotEqual(tracked.returncode, 0)

    def test_bundle_manifest_rejects_unknown_fields_and_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(InstallBundleError, "闭合schema"):
            parse_bundle_manifest(
                b'{"schema":"focus-install-bundle","unknown":true}'
            )
        with self.assertRaisesRegex(InstallBundleError, "JSON"):
            parse_bundle_manifest(
                b'{"schema":"focus-install-bundle","schema":"other"}'
            )

    def test_validation_rejects_content_not_matching_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            built = self._build(root)
            corrupted = root / "corrupted.zip"
            with zipfile.ZipFile(built.bundle_path) as source:
                entries = {name: source.read(name) for name in source.namelist()}
            entries["requirements.lock"] = b"aiohttp==0\n"
            with zipfile.ZipFile(corrupted, "w") as target:
                for name, payload in entries.items():
                    target.writestr(name, payload)
            with self.assertRaisesRegex(
                InstallBundleError,
                "size不匹配|内容校验失败",
            ):
                validate_install_bundle(corrupted, extraction_dir=root / "extract")

    def test_validation_rejects_duplicate_and_unsafe_archive_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            for case in ("duplicate", "traversal", "symlink"):
                with self.subTest(case=case):
                    artifact = root / f"{case}.zip"
                    with zipfile.ZipFile(artifact, "w") as archive:
                        archive.writestr(BUNDLE_MANIFEST_NAME, "{}")
                        if case == "duplicate":
                            archive.writestr(BUNDLE_MANIFEST_NAME, "{}")
                            archive.writestr("focus.whl", "x")
                        elif case == "traversal":
                            archive.writestr("../focus.whl", "x")
                            archive.writestr("requirements.lock", "x==1")
                        else:
                            info = zipfile.ZipInfo("focus.whl")
                            info.create_system = 3
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                            archive.writestr(info, "target")
                            archive.writestr("requirements.lock", "x==1")
                    with self.assertRaises(InstallBundleError):
                        validate_install_bundle(
                            artifact,
                            extraction_dir=root / f"extract-{case}",
                        )

    def test_validation_binds_bundle_identity_to_channel_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            built = self._build(root, channel="development")
            with self.assertRaisesRegex(InstallBundleError, "build_id"):
                validate_install_bundle(
                    built.bundle_path,
                    extraction_dir=root / "extract",
                    expected_channel="development",
                    expected_version="4.0.0.dev0",
                    expected_build_id="another-build",
                    expected_source_revision="a" * 40,
                )

    def test_stable_build_requires_release_tag_to_equal_wheel_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            with self.assertRaisesRegex(InstallBundleError, "wheel version"):
                self._build(root, channel="stable")

    def test_development_build_derives_release_tag_and_rejects_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            source = self._write_source(root)
            with patch(
                "scripts.build_support.install_bundle.build_validated_wheel",
                side_effect=self._fake_wheel_builder,
            ):
                with self.assertRaisesRegex(InstallBundleError, "不能声明release_tag"):
                    build_install_bundle(
                        source_dir=source,
                        output_dir=root / "output",
                        channel="development",
                        source_revision="a" * 40,
                        build_id="build-1",
                        release_tag="development-custom",
                    )

    def test_channel_manifest_rejects_cross_channel_and_unknown_fields(self) -> None:
        payload = {
            "build_id": "build-1",
            "bundle": {"name": "focus.zip", "sha256": "0" * 64, "size": 12},
            "channel": "development",
            "release_tag": development_release_tag("build-1"),
            "schema": "focus-install-channel",
            "schema_version": 1,
            "source_revision": "a" * 40,
            "version": "4.0.0-dev",
        }
        with self.assertRaisesRegex(InstallBundleError, "预期"):
            parse_channel_manifest(
                json.dumps(payload).encode(),
                expected_channel="stable",
            )
        payload["unknown"] = True
        with self.assertRaisesRegex(InstallBundleError, "闭合schema"):
            parse_channel_manifest(
                json.dumps(payload).encode(),
                expected_channel="development",
            )


if __name__ == "__main__":
    unittest.main()
