import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from bot.service_control_plane import ServiceControlError
from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseHolder, ThreadRuntimeLeaseStore
from bot.thread_runtime_coordination import acquire_thread_runtime_holder_or_raise


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
    def test_cross_instance_transfer_reserves_handoff_until_target_acquires(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        owner_data_dir = root_dir / "corp-a-data"
        owner_data_dir.mkdir()
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-a",
                service_token="token-a",
                control_endpoint="tcp://127.0.0.1:32001",
                app_server_url="http://127.0.0.1:1234",
                config_dir=owner_data_dir / "config",
                data_dir=owner_data_dir,
                owner_pid=os.getpid(),
            )
        )
        unsubscribe_calls: list[tuple[pathlib.Path, str, dict]] = []

        def fake_control_request(data_dir, method, params, *, timeout_seconds=3.0):
            del timeout_seconds
            unsubscribe_calls.append((pathlib.Path(data_dir), method, dict(params)))
            if method == "thread/status":
                return {
                    "thread_id": "thread-1",
                    "bound_binding_ids": ["p2p:ou_user:c1"],
                    "attached_binding_ids": ["p2p:ou_user:c1"],
                    "released_binding_ids": [],
                    "unsubscribe_available": True,
                    "unsubscribe_reason": "",
                }
            self.assertEqual(method, "thread/unsubscribe")
            lease_store.release("thread-1", "service:one")
            return {"thread_id": "thread-1", "changed": True}

        with patch("bot.thread_runtime_coordination.control_request", side_effect=fake_control_request):
            outcome = acquire_thread_runtime_holder_or_raise(
                thread_id="thread-1",
                holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                lease_store=lease_store,
                registry_store=registry_store,
            )

        self.assertTrue(outcome.result.granted)
        self.assertEqual(outcome.transferred_from, "corp-a")
        self.assertEqual(
            unsubscribe_calls,
            [
                (owner_data_dir, "thread/status", {"thread_id": "thread-1"}),
                (owner_data_dir, "thread/unsubscribe", {"thread_id": "thread-1"}),
            ],
        )
        self.assertIsNone(lease_store.load_transfer_reservation("thread-1"))
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-b")

    def test_transfer_reservation_is_cleared_when_owner_unsubscribe_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        owner_data_dir = root_dir / "corp-a-data"
        owner_data_dir.mkdir()
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-a",
                service_token="token-a",
                control_endpoint="tcp://127.0.0.1:32001",
                app_server_url="http://127.0.0.1:1234",
                config_dir=owner_data_dir / "config",
                data_dir=owner_data_dir,
                owner_pid=os.getpid(),
            )
        )

        with patch(
            "bot.thread_runtime_coordination.control_request",
            side_effect=ServiceControlError("当前有飞书侧 turn 正在运行，不能释放 runtime。"),
        ):
            with self.assertRaisesRegex(RuntimeError, "不能释放 runtime"):
                acquire_thread_runtime_holder_or_raise(
                    thread_id="thread-1",
                    holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                    lease_store=lease_store,
                    registry_store=registry_store,
                )

        self.assertIsNone(lease_store.load_transfer_reservation("thread-1"))
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")

    def test_transfer_rejects_when_owner_still_has_other_live_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        owner_data_dir = root_dir / "corp-a-data"
        owner_data_dir.mkdir()
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"),
        )
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-a",
                service_token="token-a",
                control_endpoint="tcp://127.0.0.1:32001",
                app_server_url="http://127.0.0.1:1234",
                config_dir=owner_data_dir / "config",
                data_dir=owner_data_dir,
                owner_pid=os.getpid(),
            )
        )

        with patch("bot.thread_runtime_coordination.control_request") as mock_control_request:
            with self.assertRaisesRegex(RuntimeError, "本地 `fcodex` 持有 live runtime"):
                acquire_thread_runtime_holder_or_raise(
                    thread_id="thread-1",
                    holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                    lease_store=lease_store,
                    registry_store=registry_store,
                )

        mock_control_request.assert_not_called()
        self.assertIsNone(lease_store.load_transfer_reservation("thread-1"))
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"fcodex:123", "service:one"})

    def test_transfer_rejects_cleanly_when_owner_has_only_fcodex_holder(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        owner_data_dir = root_dir / "corp-a-data"
        owner_data_dir.mkdir()
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"),
        )
        registry_store.register(
            build_instance_registry_entry(
                instance_name="corp-a",
                service_token="token-a",
                control_endpoint="tcp://127.0.0.1:32001",
                app_server_url="http://127.0.0.1:1234",
                config_dir=owner_data_dir / "config",
                data_dir=owner_data_dir,
                owner_pid=os.getpid(),
            )
        )

        with patch("bot.thread_runtime_coordination.control_request") as mock_control_request:
            with self.assertRaisesRegex(RuntimeError, "本地 `fcodex` 持有 live runtime"):
                acquire_thread_runtime_holder_or_raise(
                    thread_id="thread-1",
                    holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                    lease_store=lease_store,
                    registry_store=registry_store,
                )

        mock_control_request.assert_not_called()
        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"fcodex:123"})

    def test_transfer_keeps_live_owner_when_service_registry_entry_is_missing(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        lease_store = ThreadRuntimeLeaseStore(root_dir)
        registry_store = InstanceRegistryStore(root_dir)
        lease_store.acquire(
            "thread-1",
            _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"),
        )

        with self.assertRaisesRegex(RuntimeError, "owner 实例当前未注册"):
            acquire_thread_runtime_holder_or_raise(
                thread_id="thread-1",
                holder=_holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"),
                lease_store=lease_store,
                registry_store=registry_store,
            )

        lease = lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"service:one"})


if __name__ == "__main__":
    unittest.main()
