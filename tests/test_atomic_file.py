from __future__ import annotations

import concurrent.futures
import os
import pathlib
import stat
import tempfile
import unittest
from unittest.mock import patch

from bot.config import ensure_init_token, load_config_file
from bot.file_lock import acquire_file_lock, open_lock_file
from bot.local_websocket_auth import AppServerWebsocketAuthTokenStore


class AtomicTokenTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "directory fsync is a POSIX durability boundary")
    def test_atomic_write_syncs_file_and_parent_directory(self) -> None:
        from bot.atomic_file import atomic_write_text

        synced_directory_flags: list[bool] = []
        real_fsync = os.fsync

        def recording_fsync(file_descriptor: int) -> None:
            synced_directory_flags.append(
                stat.S_ISDIR(os.fstat(file_descriptor).st_mode)
            )
            real_fsync(file_descriptor)

        with tempfile.TemporaryDirectory() as raw:
            target = pathlib.Path(raw) / "authority.json"
            with patch("bot.atomic_file.os.fsync", side_effect=recording_fsync):
                atomic_write_text(target, "committed", mode=0o600)

            self.assertEqual(target.read_text(encoding="utf-8"), "committed")
            self.assertEqual(synced_directory_flags, [False, True])

    def test_concurrent_init_token_callers_receive_persisted_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_dir = pathlib.Path(raw)
            with patch.dict(os.environ, {"FOCUS_CONFIG_DIR": str(config_dir)}):
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                    tokens = list(executor.map(lambda _index: ensure_init_token(), range(64)))

            persisted = (config_dir / "init.token").read_text(encoding="utf-8").strip()
            self.assertEqual(set(tokens), {persisted})

    def test_concurrent_app_server_token_callers_receive_persisted_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = AppServerWebsocketAuthTokenStore(pathlib.Path(raw))
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
                tokens = list(executor.map(lambda _index: store.ensure(), range(64)))

            persisted = store.path.read_text(encoding="utf-8").strip()
            self.assertEqual(set(tokens), {persisted})

    @unittest.skipIf(os.name == "nt", "symlink behavior is platform-specific")
    def test_existing_token_symlink_is_rejected_without_following(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            outside = root / "outside-token"
            outside.write_text("outside\n", encoding="utf-8")
            token_path = root / "init.token"
            token_path.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "token path must be a regular file"):
                from bot.atomic_file import ensure_private_token

                ensure_private_token(token_path, lambda: "generated")

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    @unittest.skipIf(os.name == "nt", "symlink behavior is platform-specific")
    def test_lock_symlink_is_rejected_without_following(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            outside = root / "outside-lock"
            outside.write_text("outside\n", encoding="utf-8")
            lock_path = root / "authority.lock"
            lock_path.symlink_to(outside)

            with self.assertRaises(OSError):
                open_lock_file(lock_path)

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    @unittest.skipIf(os.name == "nt", "inode identity is POSIX-specific")
    def test_acquire_rejects_path_replacement_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            lock_path = root / "authority.lock"
            outside = root / "outside-lock"
            outside.write_text("outside\n", encoding="utf-8")
            lock_path.touch()
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                lock_path.unlink()
                lock_path.symlink_to(outside)
                with self.assertRaises(OSError):
                    acquire_file_lock(handle, blocking=False)
            finally:
                handle.close()

            self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")

    def test_existing_config_file_rejects_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_dir = pathlib.Path(raw)
            (config_dir / "codex.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "顶层必须是 YAML mapping"):
                load_config_file("codex", directory=config_dir)

    def test_empty_config_file_remains_an_empty_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            config_dir = pathlib.Path(raw)
            (config_dir / "codex.yaml").write_text("", encoding="utf-8")

            self.assertEqual(load_config_file("codex", directory=config_dir), {})


if __name__ == "__main__":
    unittest.main()
