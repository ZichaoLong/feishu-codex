import ast
import pathlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import CodexRpcError
from bot.instance_resolution import CliInstanceTarget
from bot.runtime_admin.offline_lifecycle import (
    RuntimeAdminOfflineLifecycle,
    RuntimeAdminOfflineLifecyclePorts,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.service_instance_lease import (
    ServiceInstanceLease,
    ServiceInstanceMaintenanceLeaseError,
)


def _unexpected_port(*_args, **_kwargs):
    raise AssertionError("unexpected offline lifecycle port call")


def _lifecycle(**overrides) -> RuntimeAdminOfflineLifecycle:
    ports = {
        "resolve_target_instance": _unexpected_port,
        "request": _unexpected_port,
        "attached_endpoint_adapter": _unexpected_port,
        "lifecycle_control_timeout_seconds": lambda *_args, **_kwargs: 3.0,
        "lease_owner_instance": lambda _thread_id: "",
        "list_running_instances": lambda: [],
        "list_known_instance_names": lambda: [],
        "resolve_instance_paths": _unexpected_port,
    }
    ports.update(overrides)
    return RuntimeAdminOfflineLifecycle(RuntimeAdminOfflineLifecyclePorts(**ports))


def _binding_state(thread_id: str) -> dict[str, str]:
    return {
        "working_dir": "/tmp/project",
        "current_thread_id": thread_id,
        "current_thread_title": "demo",
        "feishu_runtime_state": "detached",
        "approval_policy": "never",
        "permissions_profile_id": ":danger-full-access",
        "model": "",
        "reasoning_effort": "",
    }


class RuntimeAdminOfflineLifecycleTests(unittest.TestCase):
    def test_cleanup_archived_bindings_clears_stopped_known_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "explorer-data"
            binding = ("ou_user", "chat-1")
            ChatBindingStore(data_dir).save(binding, _binding_state("thread-1"))
            lifecycle = _lifecycle(
                list_known_instance_names=lambda: ["default", "explorer"],
                resolve_instance_paths=lambda _name: CliInstanceTarget(
                    instance_name="explorer",
                    data_dir=data_dir,
                ),
            )

            results, failures = (
                lifecycle.cleanup_archived_thread_bindings_in_other_instances(
                    "thread-1",
                    target_instance_name="default",
                    target_data_dir=Path("/tmp/default-data"),
                )
            )

            self.assertIsNone(ChatBindingStore(data_dir).load(binding))
        self.assertEqual(failures, [])
        self.assertEqual(
            results,
            [
                {
                    "instance_name": "explorer",
                    "mode": "local-store",
                    "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                }
            ],
        )

    def test_archived_binding_cleanup_dry_run_preserves_stopped_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "explorer-data"
            binding = ("ou_user", "chat-1")
            store = ChatBindingStore(data_dir)
            store.save(binding, _binding_state("thread-1"))

            cleared = _lifecycle().clear_archived_thread_bindings_from_store(
                data_dir,
                "thread-1",
                dry_run=True,
            )

            self.assertEqual(cleared, ["p2p:ou_user:chat-1"])
            self.assertIsNotNone(store.load(binding))

    def test_archived_binding_cleanup_explicit_scope_uses_only_target(self) -> None:
        entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/explorer-data"),
            running_entry=entry,
        )
        list_running = Mock(side_effect=AssertionError("unexpected fan-out"))

        def _request(data_dir: Path, method: str, params: dict[str, object]):
            self.assertEqual(data_dir, Path("/tmp/explorer-data"))
            self.assertEqual(method, "thread/clear-archived-bindings")
            self.assertEqual(params, {"thread_id": "thread-1", "dry_run": True})
            return {
                "thread_id": "thread-1",
                "would_clear_binding_ids": ["p2p:ou_user:chat-1"],
            }

        lifecycle = _lifecycle(
            resolve_target_instance=lambda _name: target,
            request=_request,
            list_running_instances=list_running,
        )

        results, failures = lifecycle.cleanup_archived_thread_bindings_in_scope(
            "thread-1",
            explicit_instance="explorer",
            dry_run=True,
        )

        list_running.assert_not_called()
        self.assertEqual(failures, [])
        self.assertEqual(
            results,
            [
                {
                    "instance_name": "explorer",
                    "mode": "control-plane",
                    "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                }
            ],
        )

    def test_list_archived_thread_ids_pages_with_archived_filter(self) -> None:
        class PagedArchivedAdapter:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.stopped = False

            def list_threads(self, **kwargs):
                self.calls.append(kwargs)
                thread_id = "thread-1" if kwargs.get("cursor") is None else "thread-2"
                next_cursor = (
                    "cursor-1" if kwargs.get("cursor") is None else None
                )
                return [
                    ThreadSummary(
                        thread_id=thread_id,
                        cwd="/tmp/project",
                        name=thread_id,
                        preview="",
                        created_at=1,
                        updated_at=1,
                        source="cli",
                        status="notLoaded",
                    )
                ], next_cursor

            def stop(self) -> None:
                self.stopped = True

        adapter = PagedArchivedAdapter()
        entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        lifecycle = _lifecycle(
            attached_endpoint_adapter=lambda *_args, **_kwargs: (
                adapter,
                object(),
                "ws://127.0.0.1:9002",
            )
        )

        thread_ids = lifecycle.list_archived_thread_ids_from_running_instance(
            Path("/tmp/explorer-data"),
            running_entry=entry,
        )

        self.assertEqual(thread_ids, ["thread-1", "thread-2"])
        self.assertTrue(adapter.stopped)
        self.assertTrue(adapter.calls[0]["archived"])
        self.assertEqual(adapter.calls[0]["model_providers"], [])
        self.assertEqual(adapter.calls[1]["cursor"], "cursor-1")

    def test_archived_store_cleanup_only_clears_matching_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            matched = ("ou_user", "chat-1")
            retained = ("ou_user", "chat-2")
            store.save(matched, _binding_state("thread-1"))
            store.save(retained, _binding_state("thread-2"))

            cleared = _lifecycle().clear_archived_thread_bindings_from_store(
                data_dir,
                "thread-1",
            )

            self.assertEqual(cleared, ["p2p:ou_user:chat-1"])
            self.assertIsNone(store.load(matched))
            self.assertIsNotNone(store.load(retained))

    def test_archived_store_cleanup_keeps_marker_when_lease_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            binding = ("ou_user", "chat-1")
            store = ChatBindingStore(data_dir)
            store.save(binding, _binding_state("thread-1"))

            with patch.object(
                RuntimeAdminOfflineLifecycle,
                "_release_offline_binding_interaction_lease",
                side_effect=OSError("lease cleanup failed"),
            ):
                with self.assertRaisesRegex(OSError, "lease cleanup failed"):
                    _lifecycle().clear_archived_thread_bindings_from_store(
                        data_dir,
                        "thread-1",
                    )

            self.assertIsNotNone(store.load(binding))

    def test_offline_store_cleanup_refuses_running_service_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            service_lease = ServiceInstanceLease(data_dir)
            service_lease.acquire()
            self.addCleanup(service_lease.release)

            with self.assertRaises(ServiceInstanceMaintenanceLeaseError):
                _lifecycle().clear_archived_thread_bindings_from_store(
                    data_dir,
                    "thread-1",
                )

    def test_stale_store_cleanup_only_clears_missing_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            stale = ("ou_user", "chat-stale")
            retained = ("ou_user", "chat-live")
            unknown = ("ou_user", "chat-unknown")
            store.save(stale, _binding_state("thread-stale"))
            store.save(retained, _binding_state("thread-live"))
            store.save(unknown, _binding_state("thread-unknown"))

            def _presence(thread_id: str):
                if thread_id == "thread-stale":
                    return "stale", "not found"
                if thread_id == "thread-unknown":
                    return "unknown", "timeout"
                return "present", ""

            result = _lifecycle().clear_stale_bindings_from_store(
                data_dir,
                _presence,
            )

            self.assertEqual(
                result["cleared_binding_ids"],
                ["p2p:ou_user:chat-stale"],
            )
            self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
            self.assertEqual(
                result["unknown_threads"],
                [{"thread_id": "thread-unknown", "reason": "timeout"}],
            )
            self.assertIsNone(store.load(stale))
            self.assertIsNotNone(store.load(retained))
            self.assertIsNotNone(store.load(unknown))

    def test_stale_store_cleanup_keeps_marker_when_lease_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            binding = ("ou_user", "chat-stale")
            store = ChatBindingStore(data_dir)
            store.save(binding, _binding_state("thread-stale"))

            with patch.object(
                RuntimeAdminOfflineLifecycle,
                "_release_offline_binding_interaction_lease",
                side_effect=OSError("lease cleanup failed"),
            ):
                with self.assertRaisesRegex(OSError, "lease cleanup failed"):
                    _lifecycle().clear_stale_bindings_from_store(
                        data_dir,
                        lambda _thread_id: ("stale", "not found"),
                    )

            self.assertIsNotNone(store.load(binding))

    def test_stale_store_cleanup_dry_run_preserves_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            binding = ("ou_user", "chat-stale")
            store = ChatBindingStore(data_dir)
            store.save(binding, _binding_state("thread-stale"))

            result = _lifecycle().clear_stale_bindings_from_store(
                data_dir,
                lambda _thread_id: ("stale", "not found"),
                dry_run=True,
            )

            self.assertEqual(result["cleared_binding_ids"], [])
            self.assertEqual(
                result["would_clear_binding_ids"],
                ["p2p:ou_user:chat-stale"],
            )
            self.assertIsNotNone(store.load(binding))

    def test_thread_presence_checker_uses_metadata_only_read(self) -> None:
        class FakeAdapter:
            def __init__(self) -> None:
                self.calls: list[tuple[str, bool]] = []

            def read_thread(self, thread_id: str, *, include_turns: bool = False):
                self.calls.append((thread_id, include_turns))
                raise CodexRpcError(
                    "thread/read",
                    {"message": f"thread not loaded: {thread_id}"},
                )

        adapter = FakeAdapter()
        entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        lifecycle = _lifecycle(
            attached_endpoint_adapter=lambda *_args, **_kwargs: (
                adapter,
                object(),
                "ws://127.0.0.1:9002",
            )
        )

        attached_adapter, check = lifecycle.build_thread_presence_checker(
            Path("/tmp/explorer-data"),
            running_entry=entry,
        )

        self.assertIs(attached_adapter, adapter)
        self.assertEqual(check("thread-stale")[0], "stale")
        self.assertEqual(adapter.calls, [("thread-stale", False)])

    def test_cli_does_not_reown_offline_lifecycle_implementation(self) -> None:
        cli_path = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "runtime_admin"
            / "cli.py"
        )
        module = ast.parse(cli_path.read_text(encoding="utf-8"))
        module_functions = {
            node.name
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        forbidden = {
            "_build_thread_presence_checker",
            "_cleanup_archived_thread_bindings_in_scope",
            "_cleanup_archived_thread_bindings_in_other_instances",
            "_clear_archived_thread_bindings_from_store",
            "_clear_stale_bindings_from_store",
            "_list_archived_thread_ids_from_running_instance",
            "_release_offline_binding_interaction_lease",
            "_resolve_archived_thread_listing_target",
            "_resolve_thread_archive_name",
            "_thread_binding_locations",
            "_validate_delete_binding_preflight",
            "_validate_lifecycle_control_result",
            "_validate_unarchive_binding_preflight",
        }

        self.assertEqual(module_functions & forbidden, set())

    def test_owner_does_not_absorb_cli_presentation_or_input(self) -> None:
        owner_path = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "runtime_admin"
            / "offline_lifecycle.py"
        )
        module = ast.parse(owner_path.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in module.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        builtin_io_calls = {
            node.func.id
            for node in ast.walk(module)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"input", "print"}
        }
        stdin_access = {
            ast.unparse(node)
            for node in ast.walk(module)
            if isinstance(node, ast.Attribute) and node.attr == "stdin"
        }

        self.assertNotIn("argparse", imported_roots)
        self.assertEqual(builtin_io_calls, set())
        self.assertEqual(stdin_access, set())


if __name__ == "__main__":
    unittest.main()
