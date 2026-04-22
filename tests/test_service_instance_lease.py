import json
import os
import pathlib
import tempfile
import unittest

from bot.stores.service_instance_lease import ServiceInstanceLease, ServiceInstanceLeaseError


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
