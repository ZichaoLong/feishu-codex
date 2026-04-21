import multiprocessing
import os
import pathlib
import queue
import tempfile
import unittest

from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry


def _register_instances_worker(
    root_dir: str,
    *,
    parent_pid: int,
    start_event,
    instance_names: tuple[str, ...],
    error_queue,
) -> None:
    try:
        store = InstanceRegistryStore(pathlib.Path(root_dir))
        if not start_event.wait(timeout=10):
            raise RuntimeError("worker start_event timed out")
        for instance_name in instance_names:
            store.register(
                build_instance_registry_entry(
                    instance_name=instance_name,
                    service_token=f"token-{instance_name}",
                    control_socket_path=f"/tmp/{instance_name}.sock",
                    app_server_url=f"ws://127.0.0.1:{9100 + len(instance_name)}",
                    config_dir=pathlib.Path(f"/tmp/{instance_name}/config"),
                    data_dir=pathlib.Path(f"/tmp/{instance_name}/data"),
                    owner_pid=parent_pid,
                )
            )
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

    def test_concurrent_process_register_preserves_all_instances(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root_dir = pathlib.Path(tempdir.name)
        store = InstanceRegistryStore(root_dir)
        ctx = multiprocessing.get_context("fork")
        start_event = ctx.Event()
        error_queue = ctx.Queue()
        parent_pid = os.getpid()
        instance_groups = [
            tuple(f"corp-{worker}-{index}" for index in range(5))
            for worker in range(4)
        ]
        expected_instance_names = {instance_name for group in instance_groups for instance_name in group}
        processes = [
            ctx.Process(
                target=_register_instances_worker,
                kwargs={
                    "root_dir": str(root_dir),
                    "parent_pid": parent_pid,
                    "start_event": start_event,
                    "instance_names": group,
                    "error_queue": error_queue,
                },
            )
            for group in instance_groups
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
        self.assertEqual(
            {entry.instance_name for entry in store.list_instances()},
            expected_instance_names,
        )


if __name__ == "__main__":
    unittest.main()
