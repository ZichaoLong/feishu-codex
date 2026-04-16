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
        socket_path = data_dir / "service-control.sock"

        metadata = lease.acquire(socket_path=socket_path)

        self.assertEqual(metadata.owner_pid, os.getpid())
        self.assertTrue(metadata.owner_token)
        self.assertEqual(metadata.socket_path, str(socket_path))
        self.assertTrue(lease.owns_socket_path(socket_path))
        self.assertIsNotNone(lease.load_metadata())

        lease.release()

        self.assertFalse(lease.owns_socket_path(socket_path))
        self.assertIsNone(lease.load_metadata())

    def test_second_acquire_fails_fast_with_existing_owner_metadata(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        socket_path = data_dir / "service-control.sock"
        first = ServiceInstanceLease(data_dir)
        second = ServiceInstanceLease(data_dir)

        first.acquire(socket_path=socket_path)
        self.addCleanup(first.release)
        self.addCleanup(second.release)

        with self.assertRaisesRegex(ServiceInstanceLeaseError, "owner_pid="):
            second.acquire(socket_path=socket_path)

    def test_release_does_not_delete_foreign_metadata(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        socket_path = data_dir / "service-control.sock"
        lease = ServiceInstanceLease(data_dir)

        lease.acquire(socket_path=socket_path)
        foreign_metadata = {
            "owner_pid": 999999,
            "owner_token": "foreign-token",
            "socket_path": str(socket_path),
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
        socket_path = data_dir / "service-control.sock"
        metadata_path = data_dir / "service-instance.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "owner_pid": 999999,
                    "owner_token": "stale-owner-token",
                    "socket_path": str(socket_path),
                    "started_at": 1.0,
                }
            ),
            encoding="utf-8",
        )
        lease = ServiceInstanceLease(data_dir)
        self.addCleanup(lease.release)

        metadata = lease.acquire(socket_path=socket_path)

        self.assertEqual(metadata.owner_pid, os.getpid())
        self.assertNotEqual(metadata.owner_token, "stale-owner-token")
        self.assertEqual(metadata.socket_path, str(socket_path))
        self.assertEqual(lease.load_metadata(), metadata)
