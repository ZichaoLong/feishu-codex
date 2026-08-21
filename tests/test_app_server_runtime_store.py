import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.instance_resolution import resolve_running_instance_app_server_url
from bot.service_control_plane import ServiceControlError
from bot.stores.app_server_runtime_store import (
    OWNED_PROCESS_KIND_DIRECT,
    AppServerRuntimeStore,
    OrphanedOwnedAppServerError,
)
from bot.stores.instance_registry_store import InstanceRegistryEntry


class AppServerRuntimeStoreTests(unittest.TestCase):
    _CLEANUP_TOKEN = "test-cleanup-token"

    def _write_cleanup_receipt(
        self,
        store: AppServerRuntimeStore,
        cleanup_token: str | None = None,
    ) -> None:
        token = cleanup_token or self._CLEANUP_TOKEN
        receipt_path = store.cleanup_receipt_path(token)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps({"cleanup_token": token}),
            encoding="utf-8",
        )

    def test_invalid_runtime_discovery_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            path = data_dir / "app_server_runtime.json"
            path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "app_server_runtime.json"):
                AppServerRuntimeStore(data_dir).load_owned_runtime()

            self.assertTrue(path.exists())

    def test_pid_reuse_does_not_publish_stale_app_server_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            with patch(
                "bot.stores.app_server_runtime_store.process_identity",
                return_value="old-incarnation",
            ):
                store.save_owned_runtime(
                    configured_url="ws://127.0.0.1:8765",
                    active_url="ws://127.0.0.1:43210",
                    owner_pid=123,
                    lifecycle_pid=456,
                    cleanup_token=self._CLEANUP_TOKEN,
                )
            self._write_cleanup_receipt(store)

            with patch(
                "bot.stores.app_server_runtime_store.process_exists",
                return_value=True,
            ):
                with patch(
                    "bot.stores.app_server_runtime_store.process_identity",
                    return_value="new-incarnation",
                ):
                    self.assertIsNone(store.load_owned_runtime())

            self.assertFalse((data_dir / "app_server_runtime.json").exists())

    def test_load_owned_runtime_clears_file_when_lifecycle_pid_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            self._write_cleanup_receipt(store)

            with patch(
                "bot.stores.app_server_runtime_store.process_exists",
                side_effect=[True, False],
            ) as mock_process_exists:
                self.assertIsNone(store.load_owned_runtime())

            self.assertEqual(
                mock_process_exists.call_args_list[0].args,
                (os.getpid(),),
            )
            self.assertEqual(
                mock_process_exists.call_args_list[1].args,
                (os.getpid(),),
            )
            self.assertFalse((data_dir / "app_server_runtime.json").exists())

    def test_dead_guardian_without_receipt_preserves_fail_closed_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )

            with patch.object(
                store,
                "_incarnation_status",
                side_effect=["gone", "gone"],
            ):
                self.assertIsNone(store.load_owned_runtime())

            self.assertTrue((data_dir / "app_server_runtime.json").exists())
            with patch.object(
                store,
                "_incarnation_status",
                side_effect=["gone", "gone"],
            ):
                with self.assertRaisesRegex(
                    OrphanedOwnedAppServerError,
                    "without a matching durable cleanup receipt",
                ) as raised:
                    store.prepare_for_owned_start(guardian_wait_seconds=0.0)

            self.assertIn(str(data_dir / "app_server_runtime.json"), str(raised.exception))
            self.assertIn("No typed recovery or force command", str(raised.exception))

    def test_clear_requires_matching_generation_and_cleanup_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AppServerRuntimeStore(Path(tmpdir))
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            self._write_cleanup_receipt(store)

            with self.assertRaisesRegex(
                OrphanedOwnedAppServerError,
                "different owned app-server runtime generation",
            ):
                store.clear_owned_runtime(
                    owner_pid=os.getpid(),
                    cleanup_token="different-token",
                )

            store.clear_owned_runtime(
                owner_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            self.assertFalse((Path(tmpdir) / "app_server_runtime.json").exists())
            self.assertFalse(
                store.cleanup_receipt_path(self._CLEANUP_TOKEN).exists()
            )

    def test_begin_guardian_generation_refuses_unresolved_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AppServerRuntimeStore(Path(tmpdir))
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )

            with self.assertRaisesRegex(
                OrphanedOwnedAppServerError,
                "prior runtime authority",
            ):
                store.begin_guardian_generation()

    def test_runtime_publication_is_idempotent_only_for_the_same_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = AppServerRuntimeStore(Path(tmpdir))
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:41001",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:41002",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            with self.assertRaisesRegex(
                OrphanedOwnedAppServerError,
                "different owned app-server runtime generation",
            ):
                store.save_owned_runtime(
                    configured_url="ws://127.0.0.1:8765",
                    active_url="ws://127.0.0.1:41003",
                    owner_pid=os.getpid(),
                    lifecycle_pid=os.getpid(),
                    cleanup_token="different-token",
                )

            runtime = store.load_owned_runtime()
            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.active_url, "ws://127.0.0.1:41002")

    def test_guardian_runtime_schema_requires_cleanup_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            with self.assertRaisesRegex(ValueError, "cleanup token"):
                store.save_owned_runtime(
                    configured_url="ws://127.0.0.1:8765",
                    active_url="ws://127.0.0.1:43210",
                    owner_pid=os.getpid(),
                    lifecycle_pid=os.getpid(),
                )

            runtime_path = data_dir / "app_server_runtime.json"
            runtime_path.write_text(
                json.dumps(
                    {
                        "configured_url": "ws://127.0.0.1:8765",
                        "active_url": "ws://127.0.0.1:43210",
                        "owner_pid": os.getpid(),
                        "owner_process_identity": "owner",
                        "lifecycle_pid": os.getpid(),
                        "lifecycle_process_identity": "guardian",
                        "lifecycle_kind": "guardian",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "guardian cleanup_token"):
                store.load_owned_runtime()

    def test_live_orphan_guardian_is_preserved_but_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            with patch.object(
                store,
                "_incarnation_status",
                side_effect=["gone", "same"],
            ):
                self.assertIsNone(store.load_owned_runtime())

            self.assertTrue((data_dir / "app_server_runtime.json").exists())

    def test_prepare_waits_for_stale_guardian_exit_before_clearing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            self._write_cleanup_receipt(store)
            with (
                patch.object(
                    store,
                    "_incarnation_status",
                    side_effect=["gone", "same", "gone"],
                ),
                patch("bot.stores.app_server_runtime_store.time.sleep"),
            ):
                store.prepare_for_owned_start(guardian_wait_seconds=1.0)

            self.assertFalse((data_dir / "app_server_runtime.json").exists())

    def test_prepare_rejects_live_legacy_direct_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                lifecycle_kind=OWNED_PROCESS_KIND_DIRECT,
            )
            with patch.object(
                store,
                "_incarnation_status",
                side_effect=["gone", "same"],
            ):
                with self.assertRaisesRegex(
                    OrphanedOwnedAppServerError,
                    "legacy direct app-server record",
                ):
                    store.prepare_for_owned_start(guardian_wait_seconds=0.0)

            self.assertTrue((data_dir / "app_server_runtime.json").exists())

    def test_dead_legacy_direct_record_is_never_retired_without_tree_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                lifecycle_kind=OWNED_PROCESS_KIND_DIRECT,
            )
            with patch.object(
                store,
                "_incarnation_status",
                side_effect=["gone", "gone"],
            ):
                self.assertIsNone(store.load_owned_runtime())
            self.assertTrue((data_dir / "app_server_runtime.json").exists())

            with patch.object(
                store,
                "_incarnation_status",
                side_effect=["gone", "gone"],
            ):
                with self.assertRaisesRegex(
                    OrphanedOwnedAppServerError,
                    "no process-tree cleanup proof",
                ):
                    store.prepare_for_owned_start(guardian_wait_seconds=0.0)

    def test_legacy_direct_record_cannot_use_normal_runtime_clear(self) -> None:
        attempts = (
            {},
            {"owner_pid": os.getpid()},
            {"cleanup_token": "unproved-cleanup-token"},
        )
        for clear_kwargs in attempts:
            with self.subTest(clear_kwargs=clear_kwargs):
                with tempfile.TemporaryDirectory() as tmpdir:
                    data_dir = Path(tmpdir)
                    store = AppServerRuntimeStore(data_dir)
                    store.save_owned_runtime(
                        configured_url="ws://127.0.0.1:8765",
                        active_url="ws://127.0.0.1:43210",
                        owner_pid=os.getpid(),
                        lifecycle_pid=os.getpid(),
                        lifecycle_kind=OWNED_PROCESS_KIND_DIRECT,
                    )

                    with self.assertRaisesRegex(
                        OrphanedOwnedAppServerError,
                        "legacy direct app-server runtime",
                    ):
                        store.clear_owned_runtime(**clear_kwargs)

                    self.assertTrue(
                        (data_dir / "app_server_runtime.json").exists()
                    )

    def test_stale_reader_cannot_delete_new_runtime_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            stale_store = AppServerRuntimeStore(data_dir)
            fresh_store = AppServerRuntimeStore(data_dir)
            stale_check_entered = threading.Event()
            release_stale_check = threading.Event()

            def process_exists(pid: int) -> bool:
                if pid == 101:
                    stale_check_entered.set()
                    self.assertTrue(release_stale_check.wait(timeout=1))
                    return False
                if pid == 102:
                    return False
                return True

            with patch(
                "bot.stores.app_server_runtime_store.process_identity",
                side_effect=lambda pid: f"identity:{pid}",
            ):
                stale_store.save_owned_runtime(
                    configured_url="ws://127.0.0.1:8765",
                    active_url="ws://127.0.0.1:10001",
                    owner_pid=101,
                    lifecycle_pid=102,
                    cleanup_token=self._CLEANUP_TOKEN,
                )
                self._write_cleanup_receipt(stale_store)
                with patch(
                    "bot.stores.app_server_runtime_store.process_exists",
                    side_effect=process_exists,
                ):
                    loader = threading.Thread(target=stale_store.load_owned_runtime)
                    loader.start()
                    self.assertTrue(stale_check_entered.wait(timeout=1))
                    saver = threading.Thread(
                        target=lambda: fresh_store.save_owned_runtime(
                            configured_url="ws://127.0.0.1:8765",
                            active_url="ws://127.0.0.1:10002",
                            owner_pid=202,
                            lifecycle_pid=203,
                            cleanup_token=self._CLEANUP_TOKEN,
                        )
                    )
                    saver.start()
                    release_stale_check.set()
                    loader.join(timeout=1)
                    saver.join(timeout=1)

                    self.assertFalse(loader.is_alive())
                    self.assertFalse(saver.is_alive())
                    runtime = fresh_store.load_owned_runtime()

            self.assertIsNotNone(runtime)
            assert runtime is not None
            self.assertEqual(runtime.active_url, "ws://127.0.0.1:10002")
            self.assertEqual(runtime.owner_pid, 202)

    def test_resolve_running_instance_app_server_url_ignores_lifecycle_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            entry = InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=os.getpid(),
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9393",
                app_server_url="ws://127.0.0.1:8765",
                config_dir="/tmp/config-explorer",
                data_dir=str(data_dir),
                started_at=1.0,
                updated_at=1.0,
            )

            self.assertEqual(resolve_running_instance_app_server_url(entry), "")

    def test_resolve_running_instance_app_server_url_prefers_control_plane_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            entry = InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=os.getpid(),
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9393",
                app_server_url="ws://127.0.0.1:8765",
                config_dir="/tmp/config-explorer",
                data_dir=str(data_dir),
                started_at=1.0,
                updated_at=1.0,
            )

            with patch(
                "bot.instance_resolution.control_request",
                return_value={"app_server_url": "ws://127.0.0.1:45555"},
            ) as mock_control_request:
                self.assertEqual(
                    resolve_running_instance_app_server_url(entry),
                    "ws://127.0.0.1:45555",
                )

            self.assertEqual(mock_control_request.call_args.args[1], "service/status")

    def test_resolve_running_instance_app_server_url_ignores_unproved_registry_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            entry = InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=os.getpid(),
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9393",
                app_server_url="ws://127.0.0.1:45555",
                config_dir="/tmp/config-explorer",
                data_dir=str(data_dir),
                started_at=1.0,
                updated_at=1.0,
            )

            self.assertEqual(
                resolve_running_instance_app_server_url(entry),
                "",
            )

    def test_resolve_running_instance_app_server_url_fails_closed_after_control_plane_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=self._CLEANUP_TOKEN,
            )
            entry = InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=os.getpid(),
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9393",
                app_server_url="ws://127.0.0.1:8765",
                config_dir="/tmp/config-explorer",
                data_dir=str(data_dir),
                started_at=1.0,
                updated_at=1.0,
            )

            with patch(
                "bot.instance_resolution.control_request",
                side_effect=ServiceControlError("boom"),
            ):
                self.assertEqual(
                    resolve_running_instance_app_server_url(entry),
                    "",
                )


if __name__ == "__main__":
    unittest.main()
