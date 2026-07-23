import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from bot.runtime_state import (
    BACKEND_THREAD_LOOKUP_ERROR,
    BACKEND_THREAD_STATUS_IDLE,
    BACKEND_THREAD_STATUS_NOT_LOADED,
)
from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseHolder, ThreadRuntimeLeaseStore
from bot.thread_runtime_coordination import (
    acquire_thread_runtime_holder_or_raise,
    inspect_thread_global_loaded_presence,
    preview_thread_runtime_holder_acquire,
    preview_thread_global_loaded_gate,
)


def _holder(*, instance_name: str, holder_id: str, service_token: str) -> ThreadRuntimeLeaseHolder:
    return ThreadRuntimeLeaseHolder(
        holder_id=holder_id,
        holder_type="service" if holder_id.startswith("service:") else "fcodex",
        instance_name=instance_name,
        owner_pid=os.getpid(),
        owner_service_token=service_token,
        control_endpoint=f"tcp://127.0.0.1:{9100 if instance_name == 'corp-a' else 9200}",
        backend_url=f"ws://127.0.0.1:{9100 if instance_name == 'corp-a' else 9200}",
        updated_at=time.time(),
    )


class ThreadRuntimeCoordinationTests(unittest.TestCase):
    def test_loaded_presence_can_include_or_exclude_current_instance(self) -> None:
        current = build_instance_registry_entry(
            instance_name="corp-a",
            service_token="token-a",
            control_endpoint="tcp://127.0.0.1:32001",
            app_server_url="http://127.0.0.1:1234",
            config_dir=pathlib.Path("/tmp/corp-a-config"),
            data_dir=pathlib.Path("/tmp/corp-a-data"),
            owner_pid=os.getpid(),
        )
        other = build_instance_registry_entry(
            instance_name="corp-b",
            service_token="token-b",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="http://127.0.0.1:2234",
            config_dir=pathlib.Path("/tmp/corp-b-config"),
            data_dir=pathlib.Path("/tmp/corp-b-data"),
            owner_pid=os.getpid(),
        )

        with patch(
            "bot.thread_runtime_coordination.control_request",
            side_effect=[
                {"backend_thread_status": BACKEND_THREAD_STATUS_IDLE},
                {"backend_thread_status": BACKEND_THREAD_STATUS_NOT_LOADED},
            ],
        ):
            including_current = inspect_thread_global_loaded_presence(
                thread_id="thread-1",
                running_instances=[current, other],
            )
            excluding_current = inspect_thread_global_loaded_presence(
                thread_id="thread-1",
                running_instances=[current, other],
                excluded_instance_names=("corp-a",),
            )

        self.assertFalse(including_current.verified_clear)
        self.assertEqual(including_current.blocking_instance, "corp-a")
        self.assertEqual(including_current.blocking_status, BACKEND_THREAD_STATUS_IDLE)
        self.assertTrue(excluding_current.verified_clear)

    def test_loaded_presence_uses_one_total_timeout_budget(self) -> None:
        entries = [
            build_instance_registry_entry(
                instance_name=instance_name,
                service_token=f"token-{instance_name}",
                control_endpoint=f"tcp://127.0.0.1:{port}",
                app_server_url=f"http://127.0.0.1:{port}",
                config_dir=pathlib.Path(f"/tmp/{instance_name}-config"),
                data_dir=pathlib.Path(f"/tmp/{instance_name}-data"),
                owner_pid=os.getpid(),
            )
            for instance_name, port in (("corp-a", 32001), ("corp-b", 32002))
        ]

        with patch(
            "bot.thread_runtime_coordination.time.monotonic",
            side_effect=[0.0, 0.1, 4.0],
        ):
            with patch(
                "bot.thread_runtime_coordination.control_request",
                return_value={"backend_thread_status": BACKEND_THREAD_STATUS_NOT_LOADED},
            ) as mock_control:
                presence = inspect_thread_global_loaded_presence(
                    thread_id="thread-1",
                    running_instances=entries,
                    timeout_seconds=3.0,
                )

        self.assertFalse(presence.verified_clear)
        self.assertEqual(presence.blocking_instance, "corp-b")
        self.assertIn("total timeout", presence.diagnostic)
        self.assertEqual(mock_control.call_count, 1)

    def test_global_loaded_gate_allows_when_other_running_instances_report_not_loaded(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        other_data_dir = root_dir / "corp-b-data"
        other_data_dir.mkdir()
        registry_store = InstanceRegistryStore(root_dir)
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-b",
                service_token="token-b",
                control_endpoint="tcp://127.0.0.1:32002",
                app_server_url="http://127.0.0.1:2234",
                config_dir=other_data_dir / "config",
                data_dir=other_data_dir,
                owner_pid=os.getpid(),
            )
        )

        with patch(
            "bot.thread_runtime_coordination.control_request",
            return_value={"backend_thread_status": BACKEND_THREAD_STATUS_NOT_LOADED},
        ) as mock_control:
            preview = preview_thread_global_loaded_gate(
                thread_id="thread-1",
                current_instance_name="corp-a",
                registry_store=registry_store,
            )

        self.assertTrue(preview.allowed)
        self.assertEqual(mock_control.call_args.args[1], "thread/loaded-status")

    def test_global_loaded_gate_rejects_when_other_running_instance_reports_loaded(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        other_data_dir = root_dir / "corp-b-data"
        other_data_dir.mkdir()
        registry_store = InstanceRegistryStore(root_dir)
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-b",
                service_token="token-b",
                control_endpoint="tcp://127.0.0.1:32002",
                app_server_url="http://127.0.0.1:2234",
                config_dir=other_data_dir / "config",
                data_dir=other_data_dir,
                owner_pid=os.getpid(),
            )
        )

        with patch(
            "bot.thread_runtime_coordination.control_request",
            return_value={"backend_thread_status": BACKEND_THREAD_STATUS_IDLE},
        ):
            preview = preview_thread_global_loaded_gate(
                thread_id="thread-1",
                current_instance_name="corp-a",
                registry_store=registry_store,
            )

        self.assertFalse(preview.allowed)
        self.assertEqual(preview.blocking_instance, "corp-b")
        self.assertEqual(preview.blocking_status, BACKEND_THREAD_STATUS_IDLE)
        self.assertIn("仍由运行中的实例 `corp-b` 保持为 loaded", preview.reason_text)
        self.assertIn("拒绝跨实例继续", preview.reason_text)

    def test_global_loaded_gate_rejects_when_other_instance_status_is_unverified(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        other_data_dir = root_dir / "corp-b-data"
        other_data_dir.mkdir()
        registry_store = InstanceRegistryStore(root_dir)
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-b",
                service_token="token-b",
                control_endpoint="tcp://127.0.0.1:32002",
                app_server_url="http://127.0.0.1:2234",
                config_dir=other_data_dir / "config",
                data_dir=other_data_dir,
                owner_pid=os.getpid(),
            )
        )

        with patch(
            "bot.thread_runtime_coordination.control_request",
            return_value={"backend_thread_status": BACKEND_THREAD_LOOKUP_ERROR},
        ):
            preview = preview_thread_global_loaded_gate(
                thread_id="thread-1",
                current_instance_name="corp-a",
                registry_store=registry_store,
            )

        self.assertFalse(preview.allowed)
        self.assertEqual(preview.blocking_instance, "corp-b")
        self.assertEqual(preview.blocking_status, BACKEND_THREAD_LOOKUP_ERROR)
        self.assertIn("不可验证的状态", preview.reason_text)

    def test_cross_instance_conflict_rejects_when_owner_service_holds_runtime(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )
        with patch("bot.thread_runtime_coordination.control_request") as mock_control_request:
            with self.assertRaisesRegex(RuntimeError, "不支持跨实例继续"):
                acquire_thread_runtime_holder_or_raise(
                    thread_id="thread-1",
                    holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                    lease_store=lease_store,
                )

        mock_control_request.assert_not_called()
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")

    def test_same_instance_new_service_generation_is_denied_fail_closed(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        data_dir = root_dir / "corp-a-data"
        data_dir.mkdir()
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-old"),
        )
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-a",
                service_token="token-new",
                control_endpoint="tcp://127.0.0.1:32001",
                app_server_url="http://127.0.0.1:1234",
                config_dir=data_dir / "config",
                data_dir=data_dir,
                owner_pid=os.getpid(),
            )
        )

        preview = preview_thread_runtime_holder_acquire(
            thread_id="thread-1",
            holder=_holder(instance_name="corp-a", holder_id="service:new", service_token="token-new"),
            lease_store=lease_store,
        )

        self.assertFalse(preview.allowed)
        self.assertIn("上一代 service 持有 live runtime", preview.reason_text)

    def test_new_service_generation_can_recover_after_instance_purge(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        data_dir = root_dir / "corp-a-data"
        data_dir.mkdir()
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-old"),
        )
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-old"),
        )
        lease_store.release("thread-1", "service:one")
        # Simulate a historical mixed-generation lease that already persisted
        # before the acquire-side guard was tightened.
        raw_path = root_dir / "thread_runtime_leases.json"
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        payload["thread-1"]["holders"].append(
            {
                "holder_id": "service:new",
                "holder_type": "service",
                "instance_name": "corp-a",
                "owner_pid": os.getpid(),
                "owner_service_token": "token-new",
                "control_endpoint": "tcp://127.0.0.1:32001",
                "backend_url": "http://127.0.0.1:1234",
                "updated_at": time.time(),
            }
        )
        raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        lease_store.purge_all_for_instance(instance_name="corp-a")
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-a",
                service_token="token-new",
                control_endpoint="tcp://127.0.0.1:32001",
                app_server_url="http://127.0.0.1:1234",
                config_dir=data_dir / "config",
                data_dir=data_dir,
                owner_pid=os.getpid(),
            )
        )

        preview = preview_thread_runtime_holder_acquire(
            thread_id="thread-1",
            holder=_holder(instance_name="corp-a", holder_id="service:new", service_token="token-new"),
            lease_store=lease_store,
        )

        self.assertTrue(preview.allowed)

    def test_cross_instance_conflict_rejects_when_owner_has_service_and_fcodex_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"),
        )

        with patch("bot.thread_runtime_coordination.control_request") as mock_control_request:
            with self.assertRaisesRegex(RuntimeError, "本地 `fcodex` 持有 live runtime"):
                acquire_thread_runtime_holder_or_raise(
                    thread_id="thread-1",
                    holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                    lease_store=lease_store,
                )

        mock_control_request.assert_not_called()
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"fcodex:123", "service:one"})

    def test_cross_instance_conflict_rejects_when_owner_has_only_fcodex_holder(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"),
        )

        with patch("bot.thread_runtime_coordination.control_request") as mock_control_request:
            with self.assertRaisesRegex(RuntimeError, "本地 `fcodex` 持有 live runtime"):
                acquire_thread_runtime_holder_or_raise(
                    thread_id="thread-1",
                    holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                    lease_store=lease_store,
                )

        mock_control_request.assert_not_called()
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"fcodex:123"})

    def test_cross_instance_conflict_keeps_live_owner_without_registry_entry(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )

        with self.assertRaisesRegex(RuntimeError, "不支持跨实例继续"):
            acquire_thread_runtime_holder_or_raise(
                thread_id="thread-1",
                holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                lease_store=lease_store,
            )

        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"service:one"})


if __name__ == "__main__":
    unittest.main()
