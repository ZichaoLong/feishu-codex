import os
import pathlib
import tempfile
import unittest

from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry


class InstanceRegistryStoreTests(unittest.TestCase):
    def test_register_load_and_unregister_instance(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = InstanceRegistryStore(pathlib.Path(tempdir.name))

        entry = build_instance_registry_entry(
            instance_name="corp-a",
            service_token="token-a",
            control_socket_path="/tmp/corp-a.sock",
            app_server_url="ws://127.0.0.1:9101",
            config_dir=pathlib.Path("/tmp/config-a"),
            data_dir=pathlib.Path("/tmp/data-a"),
            owner_pid=os.getpid(),
        )
        store.register(entry)

        loaded = store.load("corp-a")

        self.assertEqual(loaded, entry)
        self.assertEqual([item.instance_name for item in store.list_instances()], ["corp-a"])

        store.unregister("corp-a", service_token="token-a")

        self.assertIsNone(store.load("corp-a"))
        self.assertEqual(store.list_instances(), [])

    def test_stale_owner_is_pruned_from_registry(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = InstanceRegistryStore(pathlib.Path(tempdir.name))

        stale = build_instance_registry_entry(
            instance_name="corp-b",
            service_token="token-b",
            control_socket_path="/tmp/corp-b.sock",
            app_server_url="ws://127.0.0.1:9102",
            config_dir=pathlib.Path("/tmp/config-b"),
            data_dir=pathlib.Path("/tmp/data-b"),
            owner_pid=999999,
        )
        store.register(stale)

        self.assertIsNone(store.load("corp-b"))
        self.assertEqual(store.list_instances(), [])


if __name__ == "__main__":
    unittest.main()
