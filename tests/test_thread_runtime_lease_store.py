import os
import pathlib
import tempfile
import time
import unittest

from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
)


def _holder(*, instance_name: str, holder_id: str, service_token: str, owner_pid: int | None = None):
    return ThreadRuntimeLeaseHolder(
        holder_id=holder_id,
        holder_type="service" if holder_id.startswith("service:") else "fcodex",
        instance_name=instance_name,
        owner_pid=owner_pid or os.getpid(),
        owner_service_token=service_token,
        control_socket_path=f"/tmp/{instance_name}.sock",
        backend_url=f"ws://127.0.0.1:{9100 if instance_name == 'corp-a' else 9200}",
        updated_at=time.time(),
    )


class ThreadRuntimeLeaseStoreTests(unittest.TestCase):
    def test_same_instance_can_hold_multiple_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        result_1 = store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"))
        result_2 = store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"))

        self.assertTrue(result_1.granted)
        self.assertTrue(result_2.granted)
        lease = store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        self.assertEqual({item.holder_id for item in lease.holders}, {"service:one", "fcodex:123"})

    def test_different_instance_is_rejected_while_owner_exists(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"))
        result = store.acquire("thread-1", _holder(instance_name="corp-b", holder_id="service:two", service_token="token-b"))

        self.assertFalse(result.granted)
        assert result.lease is not None
        self.assertEqual(result.lease.owner_instance, "corp-a")

    def test_release_last_holder_clears_lease(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"))

        released = store.release("thread-1", "service:one")

        self.assertTrue(released)
        self.assertIsNone(store.load("thread-1"))

    def test_purge_instance_removes_matching_owner_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"))
        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"))

        purged = store.purge_instance("thread-1", instance_name="corp-a", owner_service_token="token-a")

        self.assertTrue(purged)
        self.assertIsNone(store.load("thread-1"))


if __name__ == "__main__":
    unittest.main()
