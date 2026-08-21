from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock
from unittest.mock import patch

import bot.focus_runtime.runtime as focus_runtime_module
import bot.focus_runtime.service_authority as service_authority_module
from bot.focus_runtime.service_authority import ServiceRuntimeAuthority
from bot.service_control_plane import ServiceControlError
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseStore
from bot.thread_runtime_coordination import (
    MANAGED_LOADED_INVENTORY_TOTAL_TIMEOUT_SECONDS,
    ThreadGlobalLoadedGatePreview,
    ThreadRuntimeAdmissionError,
)


_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_AUTHORITY_PATH = pathlib.Path(service_authority_module.__file__).resolve()
_EXTRACTED_ROOT_METHODS = {
    "_prepare_service_owned_state",
    "_register_instance_runtime",
    "_published_app_server_url",
    "_unregister_instance_runtime",
    "_release_service_authority_after_runtime_barrier",
    "_service_thread_runtime_holder",
    "_fcodex_runtime_holder",
    "_cross_instance_loaded_gate_check",
    "_detached_runtime_attach_check",
    "_lifecycle_loaded_gate_check",
    "_ensure_service_thread_runtime_lease",
    "_release_service_thread_runtime_lease",
}
_PUBLIC_AUTHORITY_METHODS = {
    "prepare_owned_state",
    "register_instance_runtime",
    "published_app_server_url",
    "unregister_instance_runtime",
    "release_service_authority_after_runtime_barrier",
    "service_thread_runtime_holder",
    "fcodex_runtime_holder",
    "cross_instance_loaded_gate_check",
    "detached_runtime_attach_check",
    "lifecycle_loaded_gate_check",
    "prepare_service_thread_runtime_preflight",
    "ensure_service_thread_runtime_lease",
    "managed_loaded_thread_inventory",
    "release_service_thread_runtime_lease",
    "prepare_service_thread_runtime_lease_release",
    "release_prepared_service_thread_runtime_lease",
}
_EXPECTED_DEPENDENCY_ATTRS = {
    "_data_dir",
    "_config_dir",
    "_instance_name",
    "_adapter",
    "_adapter_ingress_gate",
    "_app_server_runtime",
    "_service_instance_lease",
    "_feishu_app_connection_lease",
    "_instance_registry",
    "_thread_runtime_lease_store",
    "_service_control_plane",
    "_thread_runtime_preflight_token",
    "_thread_runtime_release_token",
}


def _class_node(path: pathlib.Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


class ServiceRuntimeAuthorityBoundaryTests(unittest.TestCase):
    def test_exact_service_methods_live_only_on_the_capability_owner(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        authority = _class_node(_AUTHORITY_PATH, "ServiceRuntimeAuthority")
        root_methods = {
            node.name
            for node in root.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        authority_methods = {
            node.name
            for node in authority.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "__init__"
        }

        self.assertEqual(root_methods & _EXTRACTED_ROOT_METHODS, set())
        self.assertEqual(authority_methods, _PUBLIC_AUTHORITY_METHODS)

    def test_owner_holds_only_explicit_dependencies_and_not_the_root(self) -> None:
        source = _AUTHORITY_PATH.read_text(encoding="utf-8")
        authority = _class_node(_AUTHORITY_PATH, "ServiceRuntimeAuthority")
        initializer = next(
            node
            for node in authority.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        assigned_attrs = {
            target.attr
            for node in ast.walk(initializer)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        }

        self.assertNotIn("FocusRuntime", source)
        self.assertEqual(assigned_attrs, _EXPECTED_DEPENDENCY_ATTRS)
        self.assertTrue(
            {"_bot", "_focus_runtime", "_handler", "_runtime", "_lock"}.isdisjoint(
                assigned_attrs
            )
        )

    def test_logging_category_remains_stable(self) -> None:
        self.assertEqual(service_authority_module.logger.name, "bot.focus_runtime")


class ServiceRuntimeAuthorityEffectTests(unittest.TestCase):
    @staticmethod
    def registry_entry(instance_name: str) -> InstanceRegistryEntry:
        return InstanceRegistryEntry(
            instance_name=instance_name,
            owner_pid=123,
            service_token=f"token-{instance_name}",
            control_endpoint="tcp://127.0.0.1:32001",
            app_server_url="ws://127.0.0.1:8765",
            config_dir=f"/focus-config/{instance_name}",
            data_dir=f"/focus-data/{instance_name}",
            started_at=1.0,
            updated_at=2.0,
        )

    def make_authority(
        self,
        events: list[str],
        *,
        runtime_lease_store=None,
    ):
        adapter = Mock()
        adapter.current_app_server_url.return_value = "ws://127.0.0.1:4321"
        ingress_gate = Mock()
        app_server_runtime = Mock()
        service_lease = Mock()
        service_lease.owner_token = "service-token"
        feishu_lease = Mock()
        instance_registry = Mock()
        mocked_runtime_lease_store = runtime_lease_store is None
        if runtime_lease_store is None:
            runtime_lease_store = Mock()
        control_plane = Mock()
        control_plane.control_endpoint = "tcp://127.0.0.1:32001"

        feishu_lease.acquire.side_effect = lambda *_args, **_kwargs: events.append(
            "feishu-acquire"
        )
        app_server_runtime.prepare_for_owned_start.side_effect = lambda: events.append(
            "backend-proof"
        )
        if mocked_runtime_lease_store:
            runtime_lease_store.purge_all_for_instance.side_effect = (
                lambda **_kwargs: events.append("runtime-purge")
            )
        instance_registry.unregister.side_effect = (
            lambda *_args, **_kwargs: events.append("registry-release")
        )
        if mocked_runtime_lease_store:
            runtime_lease_store.release_holders_for_service_generation.side_effect = (
                lambda **_kwargs: events.append("runtime-release")
            )
        feishu_lease.release.side_effect = lambda: events.append("feishu-release")
        service_lease.release.side_effect = lambda: events.append("service-release")

        authority = ServiceRuntimeAuthority(
            data_dir=pathlib.Path("/focus-data"),
            config_dir=pathlib.Path("/focus-config"),
            instance_name="default",
            adapter=adapter,
            adapter_ingress_gate=ingress_gate,
            app_server_runtime=app_server_runtime,
            service_instance_lease=service_lease,
            feishu_app_connection_lease=feishu_lease,
            instance_registry=instance_registry,
            thread_runtime_lease_store=runtime_lease_store,
            service_control_plane=control_plane,
        )
        return authority, service_lease, runtime_lease_store

    def test_prepare_owned_state_preserves_machine_effect_order(self) -> None:
        events: list[str] = []
        authority, _service_lease, _runtime_lease_store = self.make_authority(events)

        authority.prepare_owned_state("cli-app-id")

        self.assertEqual(
            events,
            ["feishu-acquire", "backend-proof", "runtime-purge"],
        )

    def test_release_preserves_machine_effect_order(self) -> None:
        events: list[str] = []
        authority, _service_lease, _runtime_lease_store = self.make_authority(events)

        authority.release_service_authority_after_runtime_barrier(
            context="test lifecycle"
        )

        self.assertEqual(
            events,
            [
                "registry-release",
                "runtime-release",
                "feishu-release",
                "service-release",
            ],
        )

    def test_release_failure_retains_authoritative_service_lease(self) -> None:
        events: list[str] = []
        authority, service_lease, runtime_lease_store = self.make_authority(events)
        runtime_lease_store.release_holders_for_service_generation.side_effect = (
            OSError("runtime holder store unavailable")
        )

        with self.assertLogs("bot.focus_runtime", level="ERROR"):
            with self.assertRaisesRegex(
                RuntimeError,
                "runtime holder store unavailable",
            ):
                authority.release_service_authority_after_runtime_barrier(
                    context="test lifecycle"
                )

        service_lease.release.assert_not_called()
        self.assertEqual(
            events,
            ["registry-release", "feishu-release"],
        )

    def test_loaded_gate_preserves_typed_blocking_instance_and_status(self) -> None:
        authority, _service_lease, runtime_lease_store = self.make_authority([])
        denied = ThreadGlobalLoadedGatePreview(
            allowed=False,
            reason_code="prompt_denied_by_live_runtime_owner",
            reason_text="loaded elsewhere",
            blocking_instance="explorer",
            blocking_status="idle",
        )

        with patch(
            "bot.focus_runtime.service_authority.preview_thread_global_loaded_gate",
            return_value=denied,
        ):
            gate = authority.cross_instance_loaded_gate_check("thread-1")
            with self.assertRaises(ThreadRuntimeAdmissionError) as caught:
                authority.ensure_service_thread_runtime_lease("thread-1")

        self.assertIs(gate, denied)
        self.assertEqual(caught.exception.blocking_instance, "explorer")
        self.assertEqual(caught.exception.blocking_status, "idle")
        self.assertEqual(
            caught.exception.reason_code,
            "prompt_denied_by_live_runtime_owner",
        )
        runtime_lease_store.acquire.assert_not_called()

    def test_prepared_service_release_preserves_fcodex_holder(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        runtime_lease_store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        authority, _service_lease, _store = self.make_authority(
            [],
            runtime_lease_store=runtime_lease_store,
        )
        runtime_lease_store.acquire(
            "thread-1",
            authority.service_thread_runtime_holder(),
        )
        runtime_lease_store.acquire(
            "thread-1",
            authority.fcodex_runtime_holder("fcodex:participant-1"),
        )

        receipt = authority.prepare_service_thread_runtime_lease_release(
            "thread-1"
        )
        assert receipt is not None
        released = authority.release_prepared_service_thread_runtime_lease(receipt)

        self.assertTrue(released)
        lease = runtime_lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(
            {holder.holder_id for holder in lease.holders},
            {"fcodex:participant-1"},
        )

    def test_prepared_service_release_rejects_refreshed_successor(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        runtime_lease_store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        authority, _service_lease, _store = self.make_authority(
            [],
            runtime_lease_store=runtime_lease_store,
        )
        runtime_lease_store.acquire(
            "thread-1",
            authority.service_thread_runtime_holder(),
        )
        receipt = authority.prepare_service_thread_runtime_lease_release(
            "thread-1"
        )
        assert receipt is not None
        lease = runtime_lease_store.load("thread-1")
        assert lease is not None
        current_holder = lease.holders[0]
        successor = replace(
            current_holder,
            updated_at=current_holder.updated_at + 1.0,
        )
        runtime_lease_store.acquire("thread-1", successor)

        released = authority.release_prepared_service_thread_runtime_lease(receipt)

        self.assertFalse(released)
        retained = runtime_lease_store.load("thread-1")
        assert retained is not None
        self.assertEqual(retained.holders, (successor,))

    def test_fcodex_only_runtime_has_no_service_release_receipt(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        runtime_lease_store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        authority, _service_lease, _store = self.make_authority(
            [],
            runtime_lease_store=runtime_lease_store,
        )
        runtime_lease_store.acquire(
            "thread-1",
            authority.fcodex_runtime_holder("fcodex:participant-1"),
        )

        receipt = authority.prepare_service_thread_runtime_lease_release(
            "thread-1"
        )

        self.assertIsNone(receipt)
        lease = runtime_lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(
            {holder.holder_id for holder in lease.holders},
            {"fcodex:participant-1"},
        )

    def test_matching_service_holder_id_with_wrong_metadata_fails_closed(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        runtime_lease_store = ThreadRuntimeLeaseStore(pathlib.Path(tempdir.name))
        authority, _service_lease, _store = self.make_authority(
            [],
            runtime_lease_store=runtime_lease_store,
        )
        mismatched_holder = replace(
            authority.service_thread_runtime_holder(),
            holder_type="fcodex",
        )
        runtime_lease_store.acquire("thread-1", mismatched_holder)

        with self.assertRaisesRegex(
            RuntimeError,
            "does not match this service generation",
        ):
            authority.prepare_service_thread_runtime_lease_release("thread-1")

        lease = runtime_lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(lease.holders[0].holder_type, "fcodex")

    def test_managed_loaded_inventory_keeps_verified_and_error_distinct(self) -> None:
        authority, _service_lease, _runtime_lease_store = self.make_authority([])
        authority._instance_registry.list_instances.return_value = [  # noqa: SLF001
            self.registry_entry("default"),
            self.registry_entry("explorer"),
            self.registry_entry("research"),
        ]

        def read_inventory(data_dir, method, params, *, timeout_seconds):
            self.assertEqual(method, "thread/loaded/list")
            self.assertEqual(params, {})
            self.assertGreater(timeout_seconds, 0)
            instance_name = pathlib.Path(data_dir).name
            if instance_name == "research":
                raise ServiceControlError("research unavailable")
            return {
                "instance_name": instance_name,
                "loaded_thread_ids": ["thread-idle", "thread-active"],
            }

        with patch(
            "bot.focus_runtime.service_authority.control_request",
            side_effect=read_inventory,
        ):
            snapshot = authority.managed_loaded_thread_inventory()

        self.assertEqual(snapshot.registry_error, "")
        self.assertEqual(
            [item.instance_name for item in snapshot.instances],
            ["explorer", "research"],
        )
        self.assertEqual(
            snapshot.instances[0].loaded_thread_ids,
            ("thread-active", "thread-idle"),
        )
        self.assertTrue(snapshot.instances[0].verified)
        self.assertFalse(snapshot.instances[1].verified)
        self.assertIn("research unavailable", snapshot.instances[1].error)

    def test_managed_loaded_inventory_uses_one_total_fanout_deadline(self) -> None:
        authority, _service_lease, _runtime_lease_store = self.make_authority([])
        authority._instance_registry.list_instances.return_value = [  # noqa: SLF001
            self.registry_entry("explorer"),
            self.registry_entry("research"),
        ]

        with (
            patch(
                "bot.focus_runtime.service_authority.control_request",
                return_value={
                    "instance_name": "explorer",
                    "loaded_thread_ids": [],
                },
            ),
            patch(
                "bot.focus_runtime.service_authority.wait",
                side_effect=lambda futures, timeout: (set(), set(futures)),
            ) as wait_for_fanout,
        ):
            snapshot = authority.managed_loaded_thread_inventory()

        wait_for_fanout.assert_called_once()
        self.assertEqual(
            wait_for_fanout.call_args.kwargs["timeout"],
            MANAGED_LOADED_INVENTORY_TOTAL_TIMEOUT_SECONDS,
        )
        self.assertEqual(len(snapshot.instances), 2)
        self.assertTrue(all("total timeout" in item.error for item in snapshot.instances))


if __name__ == "__main__":
    unittest.main()
