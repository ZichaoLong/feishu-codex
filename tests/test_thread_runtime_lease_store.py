import multiprocessing
import os
import pathlib
import queue
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

    def test_concurrent_process_acquire_preserves_all_holders(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        store = ThreadRuntimeLeaseStore(root_dir)
        ctx = multiprocessing.get_context("fork")
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


if __name__ == "__main__":
    unittest.main()
