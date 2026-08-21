import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.stores.service_instance_lease import (
    ServiceInstanceLease,
    ServiceInstanceLeaseError,
    ServiceInstanceMaintenanceLease,
    ServiceInstanceMaintenanceLeaseError,
)


class ServiceInstanceLeaseTests(unittest.TestCase):
    def test_acquire_writes_metadata_and_release_cleans_it(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lease = ServiceInstanceLease(data_dir)
        control_endpoint = "tcp://127.0.0.1:32001"

        metadata = lease.acquire(control_endpoint=control_endpoint)

        self.assertEqual(metadata.owner_pid, os.getpid())
        self.assertTrue(metadata.owner_token)
        self.assertEqual(metadata.control_endpoint, control_endpoint)
        self.assertTrue(lease.owns_current_lease())
        self.assertIsNotNone(lease.load_metadata())

        lease.release()

        self.assertFalse(lease.owns_current_lease())
        self.assertIsNone(lease.load_metadata())

    def test_second_acquire_fails_fast_with_existing_owner_metadata(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        control_endpoint = "tcp://127.0.0.1:32001"
        first = ServiceInstanceLease(data_dir)
        second = ServiceInstanceLease(data_dir)

        first.acquire(control_endpoint=control_endpoint)
        self.addCleanup(first.release)
        self.addCleanup(second.release)

        with self.assertRaisesRegex(ServiceInstanceLeaseError, "owner_pid="):
            second.acquire(control_endpoint=control_endpoint)

    def test_release_does_not_delete_foreign_metadata(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        control_endpoint = "tcp://127.0.0.1:32001"
        lease = ServiceInstanceLease(data_dir)

        lease.acquire(control_endpoint=control_endpoint)
        foreign_metadata = {
            "owner_pid": 999999,
            "owner_token": "foreign-token",
            "control_endpoint": control_endpoint,
            "started_at": 1.0,
        }
        metadata_path = data_dir / "service-instance.json"
        metadata_path.write_text(json.dumps(foreign_metadata), encoding="utf-8")

        lease.release()

        self.assertTrue(metadata_path.exists())

    def test_reentrant_acquire_rejects_missing_or_foreign_metadata(self) -> None:
        for replacement in (None, "foreign-token"):
            with self.subTest(replacement=replacement):
                tempdir = tempfile.TemporaryDirectory()
                self.addCleanup(tempdir.cleanup)
                data_dir = pathlib.Path(tempdir.name)
                lease = ServiceInstanceLease(data_dir)
                self.addCleanup(lease.release)
                lease.acquire()
                original_handle = lease._lock_file
                metadata_path = data_dir / "service-instance.json"
                if replacement is None:
                    metadata_path.unlink()
                else:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    metadata["owner_token"] = replacement
                    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                with self.assertRaisesRegex(
                    ServiceInstanceLeaseError,
                    "matching metadata",
                ):
                    lease.acquire()

                self.assertIs(lease._lock_file, original_handle)

    def test_acquire_replaces_stale_metadata_from_dead_owner(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        control_endpoint = "tcp://127.0.0.1:32001"
        metadata_path = data_dir / "service-instance.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "owner_pid": 999999,
                    "owner_token": "stale-owner-token",
                    "control_endpoint": control_endpoint,
                    "started_at": 1.0,
                }
            ),
            encoding="utf-8",
        )
        lease = ServiceInstanceLease(data_dir)
        self.addCleanup(lease.release)

        metadata = lease.acquire(control_endpoint=control_endpoint)

        self.assertEqual(metadata.owner_pid, os.getpid())
        self.assertNotEqual(metadata.owner_token, "stale-owner-token")
        self.assertEqual(metadata.control_endpoint, control_endpoint)
        self.assertEqual(lease.load_metadata(), metadata)

    def test_acquire_metadata_failure_releases_os_lock_immediately(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        failed = ServiceInstanceLease(data_dir)
        contender = ServiceInstanceLease(data_dir)
        self.addCleanup(failed.release)
        self.addCleanup(contender.release)
        publication_error = OSError("metadata publication failed")

        with patch.object(
            failed,
            "_write_metadata_unlocked",
            side_effect=publication_error,
        ):
            with self.assertRaises(OSError) as raised:
                failed.acquire()

        self.assertIs(raised.exception, publication_error)
        self.assertEqual(failed.owner_token, "")
        contender.acquire()
        self.assertTrue(contender.owns_current_lease())

    def test_release_metadata_failure_retains_os_lock_for_retry(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner = ServiceInstanceLease(data_dir)
        contender = ServiceInstanceLease(data_dir)
        self.addCleanup(owner.release)
        self.addCleanup(contender.release)
        owner.acquire()
        deletion_error = OSError("metadata deletion failed")

        with patch.object(
            owner,
            "_delete_metadata_unlocked",
            side_effect=deletion_error,
        ):
            with self.assertRaises(OSError) as raised:
                owner.release()

        self.assertIs(raised.exception, deletion_error)
        self.assertTrue(owner.owns_current_lease())
        with self.assertRaises(ServiceInstanceLeaseError):
            contender.acquire()

        owner.release()
        contender.acquire()
        self.assertTrue(contender.owns_current_lease())

    def test_release_unlock_failure_closes_handle_and_releases_os_lock(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner = ServiceInstanceLease(data_dir)
        contender = ServiceInstanceLease(data_dir)
        self.addCleanup(owner.release)
        self.addCleanup(contender.release)
        owner.acquire()
        unlock_error = OSError("explicit unlock failed")

        with patch(
            "bot.stores.service_instance_lease.release_file_lock",
            side_effect=unlock_error,
        ):
            owner.release()

        self.assertEqual(owner.owner_token, "")
        contender.acquire()
        self.assertTrue(contender.owns_current_lease())

    def test_release_close_failure_retains_handle_for_retry(self) -> None:
        class _FailFirstClose:
            def __init__(self, wrapped) -> None:
                self._wrapped = wrapped
                self._failed = False

            @property
            def closed(self) -> bool:
                return self._wrapped.closed

            def close(self) -> None:
                if not self._failed:
                    self._failed = True
                    raise OSError("close failed")
                self._wrapped.close()

            def __getattr__(self, name: str):
                return getattr(self._wrapped, name)

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner = ServiceInstanceLease(data_dir)
        contender = ServiceInstanceLease(data_dir)
        self.addCleanup(owner.release)
        self.addCleanup(contender.release)
        owner.acquire()
        retained_token = owner.owner_token
        flaky_handle = _FailFirstClose(owner._lock_file)
        owner._lock_file = flaky_handle

        unlock_error = OSError("explicit unlock failed")
        # Neither the explicit unlock nor the first close proves release, so
        # the exact handle/token must remain available for a second close.
        with patch(
            "bot.stores.service_instance_lease.release_file_lock",
            side_effect=[unlock_error, unlock_error],
        ):
            with self.assertRaisesRegex(OSError, "explicit unlock failed"):
                owner.release()

            self.assertIs(owner._lock_file, flaky_handle)
            self.assertEqual(owner.owner_token, retained_token)
            owner.release()

        self.assertIsNone(owner._lock_file)
        self.assertEqual(owner.owner_token, "")
        contender.acquire()
        self.assertTrue(contender.owns_current_lease())

    def test_maintenance_lease_blocks_service_without_publishing_metadata(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        maintenance = ServiceInstanceMaintenanceLease(data_dir)
        service = ServiceInstanceLease(data_dir)
        self.addCleanup(maintenance.release)
        self.addCleanup(service.release)

        maintenance.acquire()

        self.assertIsNone(service.load_metadata())
        with self.assertRaises(ServiceInstanceLeaseError):
            service.acquire()

    def test_running_service_blocks_maintenance_lease(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        service = ServiceInstanceLease(data_dir)
        maintenance = ServiceInstanceMaintenanceLease(data_dir)
        self.addCleanup(service.release)
        self.addCleanup(maintenance.release)
        service.acquire()

        with self.assertRaises(ServiceInstanceMaintenanceLeaseError):
            maintenance.acquire()
