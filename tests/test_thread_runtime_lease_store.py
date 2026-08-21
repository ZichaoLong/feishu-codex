import json
import multiprocessing
import os
import pathlib
import queue
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
    ThreadRuntimeLeaseStoreUnavailable,
)


def _holder(*, instance_name: str, holder_id: str, service_token: str, owner_pid: int | None = None):
    return ThreadRuntimeLeaseHolder(
        holder_id=holder_id,
        holder_type="service" if holder_id.startswith("service:") else "fcodex",
        instance_name=instance_name,
        owner_pid=os.getpid() if owner_pid is None else owner_pid,
        owner_service_token=service_token,
        control_endpoint=f"tcp://127.0.0.1:{9100 if instance_name == 'corp-a' else 9200}",
        backend_url=f"ws://127.0.0.1:{9100 if instance_name == 'corp-a' else 9200}",
        updated_at=time.time(),
    )


def _acquire_thread_runtime_holders_worker(
    root_dir: str,
    *,
    parent_pid: int,
    start_event,
    holder_ids: tuple[str, ...],
    error_queue,
) -> None:
    try:
        store = ThreadRuntimeLeaseStore(pathlib.Path(root_dir))
        if not start_event.wait(timeout=10):
            raise RuntimeError("worker start_event timed out")
        for holder_id in holder_ids:
            result = store.acquire(
                "thread-1",
                _holder(
                    instance_name="corp-a",
                    holder_id=holder_id,
                    service_token="token-a",
                    owner_pid=parent_pid,
                ),
            )
            if not result.granted:
                raise RuntimeError(f"unexpected lease rejection for {holder_id}")
    except Exception as exc:
        error_queue.put(str(exc))
        raise


def _drain_error_queue(error_queue) -> list[str]:
    errors: list[str] = []
    while True:
        try:
            errors.append(error_queue.get_nowait())
        except queue.Empty:
            return errors


def _write_raw_lease(
    root_dir: pathlib.Path,
    *,
    thread_id: str,
    holders: list[dict[str, object]],
    owner_instance: str,
    owner_service_token: str,
    transfer: dict[str, object] | None = None,
) -> None:
    now = time.time()
    payload: dict[str, object] = {
        "thread_id": thread_id,
        "owner_instance": owner_instance,
        "owner_service_token": owner_service_token,
        "control_endpoint": "tcp://127.0.0.1:9100",
        "backend_url": "ws://127.0.0.1:9100",
        "attached_at": now,
        "holders": holders,
    }
    if transfer is not None:
        payload["transfer"] = transfer
    (root_dir / "thread_runtime_leases.json").write_text(
        json.dumps({thread_id: payload}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
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

    def test_same_instance_different_service_token_is_rejected(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-old"))
        result = store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:new", service_token="token-new"))

        self.assertFalse(result.granted)
        lease = store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_service_token, "token-old")
        self.assertEqual({item.owner_service_token for item in lease.holders}, {"token-old"})

    def test_release_last_holder_clears_lease(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"))

        released = store.release("thread-1", "service:one")

        self.assertTrue(released)
        self.assertIsNone(store.load("thread-1"))

    def test_exact_release_preserves_independent_fcodex_holder(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        service_result = store.acquire(
            "thread-1",
            _holder(
                instance_name="corp-a",
                holder_id="service:one",
                service_token="token-a",
            ),
        )
        store.acquire(
            "thread-1",
            _holder(
                instance_name="corp-a",
                holder_id="fcodex:123",
                service_token="token-a",
            ),
        )
        assert service_result.lease is not None
        expected_holder = next(
            holder
            for holder in service_result.lease.holders
            if holder.holder_id == "service:one"
        )

        released = store.release_if_matches("thread-1", expected_holder)

        self.assertTrue(released)
        lease = store.load("thread-1")
        assert lease is not None
        self.assertEqual(
            {holder.holder_id for holder in lease.holders},
            {"fcodex:123"},
        )

    def test_exact_release_rejects_same_id_refreshed_successor(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        first = store.acquire(
            "thread-1",
            _holder(
                instance_name="corp-a",
                holder_id="service:one",
                service_token="token-a",
            ),
        )
        assert first.lease is not None
        expected_holder = first.lease.holders[0]
        successor = replace(
            expected_holder,
            backend_url="ws://127.0.0.1:9191",
            updated_at=expected_holder.updated_at + 1.0,
        )
        refreshed = store.acquire("thread-1", successor)

        released = store.release_if_matches("thread-1", expected_holder)

        self.assertTrue(refreshed.granted)
        self.assertFalse(refreshed.acquired)
        self.assertFalse(released)
        lease = store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.holders, (successor,))

    def test_generation_cleanup_releases_all_matching_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        store.acquire(
            "thread-1",
            _holder(
                instance_name="corp-a",
                holder_id="service:current",
                service_token="token-current",
            ),
        )
        store.acquire(
            "thread-1",
            _holder(
                instance_name="corp-a",
                holder_id="fcodex:process-local",
                service_token="token-current",
                owner_pid=0,
            ),
        )
        store.acquire(
            "thread-other",
            _holder(
                instance_name="corp-b",
                holder_id="service:other",
                service_token="token-other",
            ),
        )

        changed = store.release_holders_for_service_generation(
            instance_name="corp-a",
            owner_service_token="token-current",
        )

        self.assertEqual(changed, ["thread-1"])
        self.assertIsNone(store.load("thread-1"))
        other = store.load("thread-other")
        assert other is not None
        self.assertEqual(
            {holder.holder_id for holder in other.holders},
            {"service:other"},
        )

    def test_purge_instance_removes_matching_owner_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))

        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="service:one", service_token="token-a"))
        store.acquire("thread-1", _holder(instance_name="corp-a", holder_id="fcodex:123", service_token="token-a"))

        purged = store.purge_instance("thread-1", instance_name="corp-a")

        self.assertTrue(purged)
        self.assertIsNone(store.load("thread-1"))

    def test_purge_instance_also_removes_stale_same_instance_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        store = ThreadRuntimeLeaseStore(root_dir)
        now = time.time()
        pid = os.getpid()
        _write_raw_lease(
            root_dir,
            thread_id="thread-1",
            owner_instance="corp-a",
            owner_service_token="token-old",
            holders=[
                {
                    "holder_id": "fcodex:123",
                    "holder_type": "fcodex",
                    "instance_name": "corp-a",
                    "owner_pid": pid,
                    "owner_service_token": "token-old",
                    "control_endpoint": "tcp://127.0.0.1:9100",
                    "backend_url": "ws://127.0.0.1:9100",
                    "updated_at": now,
                },
                {
                    "holder_id": "service:new",
                    "holder_type": "service",
                    "instance_name": "corp-a",
                    "owner_pid": pid,
                    "owner_service_token": "token-new",
                    "control_endpoint": "tcp://127.0.0.1:9101",
                    "backend_url": "ws://127.0.0.1:9101",
                    "updated_at": now,
                },
            ],
        )

        purged = store.purge_instance("thread-1", instance_name="corp-a")

        self.assertTrue(purged)
        self.assertIsNone(store.load("thread-1"))

    def test_purge_all_for_instance_removes_stale_holders_and_legacy_transfer(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        store = ThreadRuntimeLeaseStore(root_dir)
        now = time.time()
        pid = os.getpid()
        _write_raw_lease(
            root_dir,
            thread_id="thread-1",
            owner_instance="corp-a",
            owner_service_token="token-old",
            holders=[
                {
                    "holder_id": "fcodex:123",
                    "holder_type": "fcodex",
                    "instance_name": "corp-a",
                    "owner_pid": pid,
                    "owner_service_token": "token-old",
                    "control_endpoint": "tcp://127.0.0.1:9100",
                    "backend_url": "ws://127.0.0.1:9100",
                    "updated_at": now,
                },
                {
                    "holder_id": "service:new",
                    "holder_type": "service",
                    "instance_name": "corp-a",
                    "owner_pid": pid,
                    "owner_service_token": "token-new",
                    "control_endpoint": "tcp://127.0.0.1:9101",
                    "backend_url": "ws://127.0.0.1:9101",
                    "updated_at": now,
                },
            ],
            transfer={
                "thread_id": "thread-1",
                "owner_instance": "corp-a",
                "owner_service_token": "token-old",
                "target_instance": "corp-b",
                "target_service_token": "token-b",
                "reserved_at": now,
                "expires_at": now + 60.0,
            },
        )

        removed = store.purge_all_for_instance(instance_name="corp-a")

        self.assertEqual(removed, ["thread-1"])
        self.assertIsNone(store.load("thread-1"))
        raw = json.loads((root_dir / "thread_runtime_leases.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["records"], {})

    def test_concurrent_process_acquire_preserves_all_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        store = ThreadRuntimeLeaseStore(root_dir)
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        error_queue = ctx.Queue()
        parent_pid = os.getpid()
        holder_groups = [
            tuple(f"service:{worker}:{index}" for index in range(6))
            for worker in range(4)
        ]
        expected_holder_ids = {holder_id for group in holder_groups for holder_id in group}
        processes = [
            ctx.Process(
                target=_acquire_thread_runtime_holders_worker,
                kwargs={
                    "root_dir": str(root_dir),
                    "parent_pid": parent_pid,
                    "start_event": start_event,
                    "holder_ids": group,
                    "error_queue": error_queue,
                },
            )
            for group in holder_groups
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                self.fail(f"worker pid={process.pid} did not exit in time")
            self.assertEqual(process.exitcode, 0, msg="\n".join(_drain_error_queue(error_queue)))

        self.assertEqual(_drain_error_queue(error_queue), [])
        lease = store.load("thread-1")
        assert lease is not None
        self.assertEqual({item.holder_id for item in lease.holders}, expected_holder_ids)

    def test_load_strips_legacy_transfer_field_from_persisted_lease(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        store = ThreadRuntimeLeaseStore(root_dir)
        now = time.time()
        _write_raw_lease(
            root_dir,
            thread_id="thread-1",
            owner_instance="corp-a",
            owner_service_token="token-a",
            holders=[
                {
                    "holder_id": "service:one",
                    "holder_type": "service",
                    "instance_name": "corp-a",
                    "owner_pid": os.getpid(),
                    "owner_service_token": "token-a",
                    "control_endpoint": "tcp://127.0.0.1:9100",
                    "backend_url": "ws://127.0.0.1:9100",
                    "updated_at": now,
                },
            ],
            transfer={
                "thread_id": "thread-1",
                "owner_instance": "corp-a",
                "owner_service_token": "token-a",
                "target_instance": "corp-b",
                "target_service_token": "token-b",
                "reserved_at": now,
                "expires_at": now + 60.0,
            },
        )

        lease = store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.owner_instance, "corp-a")
        payload = json.loads((root_dir / "thread_runtime_leases.json").read_text(encoding="utf-8"))
        self.assertNotIn("transfer", payload["records"]["thread-1"])

    def test_corrupted_json_fails_closed_without_read_rewrite(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        path = root_dir / "thread_runtime_leases.json"
        original = b"{broken"
        path.write_bytes(original)
        store = ThreadRuntimeLeaseStore(root_dir)

        with self.assertRaises(ThreadRuntimeLeaseStoreUnavailable):
            store.load("thread-1")
        with self.assertRaises(ThreadRuntimeLeaseStoreUnavailable):
            store.acquire(
                "thread-1",
                _holder(
                    instance_name="corp-a",
                    holder_id="service:one",
                    service_token="token-a",
                ),
            )

        self.assertEqual(path.read_bytes(), original)

    def test_future_schema_fails_closed_without_overwrite(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        path = root_dir / "thread_runtime_leases.json"
        original = b'{"schema_version": 999, "records": {}}\n'
        path.write_bytes(original)
        store = ThreadRuntimeLeaseStore(root_dir)

        with self.assertRaises(ThreadRuntimeLeaseStoreUnavailable):
            store.acquire(
                "thread-1",
                _holder(
                    instance_name="corp-a",
                    holder_id="service:one",
                    service_token="token-a",
                ),
            )

        self.assertEqual(path.read_bytes(), original)

    def test_pid_reuse_identity_prunes_old_holder(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        now = time.time()
        _write_raw_lease(
            root_dir,
            thread_id="thread-1",
            owner_instance="corp-a",
            owner_service_token="token-a",
            holders=[
                {
                    "holder_id": "service:one",
                    "holder_type": "service",
                    "instance_name": "corp-a",
                    "owner_pid": os.getpid(),
                    "owner_service_token": "token-a",
                    "control_endpoint": "tcp://127.0.0.1:9100",
                    "backend_url": "ws://127.0.0.1:9100",
                    "updated_at": now,
                    "owner_process_identity": "old-incarnation",
                },
            ],
        )
        store = ThreadRuntimeLeaseStore(root_dir)

        with patch("bot.stores.thread_runtime_lease_store.process_exists", return_value=True):
            with patch(
                "bot.stores.thread_runtime_lease_store.process_identity",
                return_value="new-incarnation",
            ):
                self.assertIsNone(store.load("thread-1"))


if __name__ == "__main__":
    unittest.main()
