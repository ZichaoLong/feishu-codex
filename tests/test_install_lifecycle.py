from __future__ import annotations

import pathlib
import tempfile
import unittest

from bot.install_lifecycle import (
    ManagedInstallLifecycleError,
    ManagedInstallLifecyclePorts,
    ManagedInstallLock,
    ManagedInstallTransaction,
    managed_install_lock_path,
)
from bot.file_lock import acquire_file_lock, release_file_lock
from bot.service_manager import ServiceStatus
from bot.service_control_plane import ServiceControlOutcomeUnknownError


class _Lease:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_acquire: bool = False,
    ) -> None:
        self._name = name
        self._events = events
        self._fail_acquire = fail_acquire

    def acquire(self) -> None:
        self._events.append(f"lease+:{self._name}")
        if self._fail_acquire:
            raise RuntimeError(f"lease busy: {self._name}")

    def release(self) -> None:
        self._events.append(f"lease-:{self._name}")


class _Harness:
    def __init__(self, *, running: set[str]) -> None:
        self.running = set(running)
        self.events: list[str] = []
        self.prepare_failures: dict[str, Exception] = {}
        self.invalid_prepare: set[str] = set()
        self.stop_failures: dict[str, Exception] = {}
        self.start_without_running: set[str] = set()
        self.lease_failures: set[str] = set()

    def status(self, instance_name: str) -> ServiceStatus:
        self.events.append(f"status:{instance_name}")
        return ServiceStatus(
            installed=True,
            running=instance_name in self.running,
            detail="active" if instance_name in self.running else "inactive",
        )

    def prepare(self, instance_name: str):
        self.events.append(f"prepare:{instance_name}")
        failure = self.prepare_failures.get(instance_name)
        if failure is not None:
            raise failure
        if instance_name in self.invalid_prepare:
            return {"instance_name": "other", "status": "prepared"}
        return {"instance_name": instance_name, "status": "prepared"}

    def cancel(self, instance_name: str) -> None:
        self.events.append(f"cancel:{instance_name}")

    def stop(self, instance_name: str) -> None:
        self.events.append(f"stop:{instance_name}")
        failure = self.stop_failures.get(instance_name)
        if failure is not None:
            raise failure
        self.running.discard(instance_name)

    def start(self, instance_name: str) -> None:
        self.events.append(f"start:{instance_name}")
        if instance_name not in self.start_without_running:
            self.running.add(instance_name)

    def lease(self, instance_name: str) -> _Lease:
        return _Lease(
            instance_name,
            self.events,
            fail_acquire=instance_name in self.lease_failures,
        )

    def ports(self) -> ManagedInstallLifecyclePorts:
        return ManagedInstallLifecyclePorts(
            service_status=self.status,
            prepare_offline_maintenance=self.prepare,
            cancel_offline_maintenance=self.cancel,
            stop_service=self.stop,
            start_service=self.start,
            maintenance_lease=self.lease,
        )


class ManagedInstallLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = pathlib.Path(self.tempdir.name)

    def transaction(
        self,
        harness: _Harness,
        *,
        names: tuple[str, ...] = ("default", "corp-a"),
    ) -> ManagedInstallTransaction:
        return ManagedInstallTransaction(
            operation="install",
            instance_names=names,
            lock=ManagedInstallLock(self.root / "managed-install.lock"),
            ports=harness.ports(),
            status_timeout_seconds=0,
            status_poll_seconds=0,
        )

    def test_machine_lock_rejects_a_second_holder(self) -> None:
        lock_path = managed_install_lock_path(self.root / "focus-data")
        first = ManagedInstallLock(lock_path)
        second = ManagedInstallLock(lock_path)

        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaisesRegex(
            ManagedInstallLifecycleError,
            "已有 install、uninstall 或 purge",
        ):
            second.acquire()

        first.release()
        second.acquire()
        second.release()

    def test_handoff_barrier_blocks_new_lifecycle_after_primary_is_released(self) -> None:
        lock_path = managed_install_lock_path(self.root / "focus-data")
        parent = ManagedInstallLock(lock_path)
        contender = ManagedInstallLock(lock_path)
        parent.acquire()
        parent.yield_handoff_barrier()

        helper_handle = parent.handoff_path.open("a+", encoding="utf-8")
        acquire_file_lock(helper_handle, blocking=False)
        try:
            parent.release()

            with self.assertRaisesRegex(
                ManagedInstallLifecycleError,
                "Windows 删除 helper",
            ):
                contender.acquire()
        finally:
            release_file_lock(helper_handle)
            helper_handle.close()
        contender.acquire()
        contender.release()

    def test_all_stopped_instances_never_receive_prepare_stop_or_start(self) -> None:
        harness = _Harness(running=set())
        transaction = self.transaction(harness)

        with transaction:
            harness.events.append("body")

        self.assertEqual(transaction.originally_running_instances, ())
        self.assertEqual(transaction.restored_instances, ())
        self.assertNotIn("prepare:default", harness.events)
        self.assertNotIn("stop:default", harness.events)
        self.assertFalse(any(event.startswith("start:") for event in harness.events))
        self.assertLess(harness.events.index("lease+:default"), harness.events.index("body"))
        self.assertLess(harness.events.index("body"), harness.events.index("lease-:default"))

    def test_success_stops_and_restores_only_the_original_running_set(self) -> None:
        harness = _Harness(running={"default"})
        transaction = self.transaction(harness)

        with transaction:
            harness.events.append("body")
            self.assertEqual(harness.running, set())

        self.assertEqual(transaction.originally_running_instances, ("default",))
        self.assertEqual(transaction.restored_instances, ("default",))
        self.assertEqual(harness.running, {"default"})
        self.assertIn("prepare:default", harness.events)
        self.assertIn("stop:default", harness.events)
        self.assertIn("start:default", harness.events)
        self.assertNotIn("prepare:corp-a", harness.events)
        self.assertNotIn("start:corp-a", harness.events)
        self.assertLess(harness.events.index("prepare:default"), harness.events.index("stop:default"))
        self.assertLess(harness.events.index("stop:default"), harness.events.index("body"))
        self.assertLess(harness.events.index("body"), harness.events.index("start:default"))

    def test_busy_instance_cancels_earlier_admission_before_any_stop(self) -> None:
        harness = _Harness(running={"default", "corp-a"})
        harness.prepare_failures["corp-a"] = RuntimeError("active turn")
        transaction = self.transaction(harness)

        with self.assertRaisesRegex(ManagedInstallLifecycleError, "active turn"):
            with transaction:
                self.fail("body must not run")

        self.assertIn("cancel:default", harness.events)
        self.assertFalse(any(event.startswith("stop:") for event in harness.events))
        self.assertFalse(any(event.startswith("lease+:") for event in harness.events))
        self.assertFalse(any(event.startswith("start:") for event in harness.events))

    def test_unsupported_prepare_is_explicit_and_leaves_install_surface_untouched(self) -> None:
        harness = _Harness(running={"default"})
        harness.prepare_failures["default"] = RuntimeError(
            "未知控制面方法：service/prepare-offline-maintenance"
        )

        with self.assertRaisesRegex(
            ManagedInstallLifecycleError,
            "请先手工停止所有实例",
        ):
            self.transaction(harness).prepare()

        self.assertFalse(any(event.startswith("stop:") for event in harness.events))
        self.assertFalse(any(event.startswith("lease+:") for event in harness.events))

    def test_unknown_prepare_outcome_attempts_exact_cancel_before_failing(self) -> None:
        harness = _Harness(running={"default"})
        harness.prepare_failures["default"] = ServiceControlOutcomeUnknownError(
            "response lost"
        )

        with self.assertRaisesRegex(ManagedInstallLifecycleError, "response lost"):
            self.transaction(harness).prepare()

        self.assertIn("cancel:default", harness.events)
        self.assertNotIn("stop:default", harness.events)

    def test_stop_failure_does_not_cancel_or_restart_after_stop_phase_begins(self) -> None:
        harness = _Harness(running={"default"})
        harness.stop_failures["default"] = RuntimeError("stop failed")

        with self.assertRaisesRegex(ManagedInstallLifecycleError, "不会自动重启"):
            self.transaction(harness).prepare()

        self.assertNotIn("cancel:default", harness.events)
        self.assertNotIn("start:default", harness.events)

    def test_maintenance_lease_failure_releases_earlier_lease_without_restart(self) -> None:
        harness = _Harness(running={"default"})
        harness.lease_failures.add("corp-a")

        with self.assertRaisesRegex(ManagedInstallLifecycleError, "lease busy"):
            self.transaction(harness).prepare()

        self.assertIn("lease-:default", harness.events)
        self.assertNotIn("start:default", harness.events)
        self.assertEqual(harness.running, set())

    def test_install_body_failure_aborts_and_leaves_original_service_stopped(self) -> None:
        harness = _Harness(running={"default"})
        transaction = self.transaction(harness)

        with self.assertRaisesRegex(RuntimeError, "pip failed"):
            with transaction:
                raise RuntimeError("pip failed")

        self.assertIn("lease-:default", harness.events)
        self.assertNotIn("start:default", harness.events)
        self.assertEqual(harness.running, set())
        self.assertEqual(transaction.restored_instances, ())

    def test_restore_failure_is_nonzero_and_never_claims_restored(self) -> None:
        harness = _Harness(running={"default"})
        harness.start_without_running.add("default")
        transaction = self.transaction(harness)

        with self.assertRaisesRegex(ManagedInstallLifecycleError, "服务恢复未能证明完成"):
            with transaction:
                harness.events.append("body")

        self.assertEqual(transaction.restored_instances, ())
        self.assertNotIn("default", harness.running)


if __name__ == "__main__":
    unittest.main()
