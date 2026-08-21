from __future__ import annotations

import ast
import pathlib
import tempfile
import threading
import types
import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.reason_codes import PROMPT_DENIED_BINDING_NOT_FOUND, ReasonedCheck
from bot.runtime_admin.binding_application import (
    RuntimeAdminBindingApplication,
    RuntimeAdminBindingApplicationPorts,
)
from bot.stores.chat_binding_store import ChatBindingStore
from tests.runtime_admin_test_support import make_binding_runtime


class RuntimeAdminBindingApplicationTests(unittest.TestCase):
    def _make_application(self, **overrides):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        store = ChatBindingStore(data_dir)
        _interaction_store, binding_runtime = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=store,
        )
        summaries: dict[str, ThreadSummary] = {}
        submitted: list[dict[str, object]] = []
        values = {
            "read_thread": lambda thread_id: ThreadSnapshot(
                summary=summaries[thread_id]
            ),
            "read_thread_for_stale_cleanup": lambda thread_id: ThreadSnapshot(
                summary=summaries[thread_id]
            ),
            "unsubscribe_thread": lambda _thread_id: None,
            "attach_binding": lambda _binding, thread_id: summaries[thread_id],
            "get_thread_goal": lambda _thread_id: None,
            "clear_all_stored_bindings": store.clear_all,
            "deactivate_binding_and_invalidate_queue_locked": lambda _binding: None,
            "deactivate_bindings_and_invalidate_queues_locked": (
                lambda _bindings, _errors: None
            ),
            "release_service_thread_runtime_lease": lambda _thread_id: None,
            "instance_name": lambda: "default",
            "load_thread_runtime_lease": lambda _thread_id: None,
            "submit_prompt_for_control": lambda binding, **kwargs: (
                submitted.append({"binding": binding, **kwargs})
                or {"started": True}
            ),
            "prompt_write_denial_check": (
                lambda _binding, _chat_id, _thread_id, **_kwargs: (
                    ReasonedCheck.allow()
                )
            ),
            "external_control_write_denial_check": (
                lambda _thread_id, **_kwargs: ReasonedCheck.allow()
            ),
            "all_mode_thread_exclusivity_check": (
                lambda _chat_id, _thread_id, **_kwargs: ReasonedCheck.allow()
            ),
            "detached_runtime_attach_check": (
                lambda _thread_id: ReasonedCheck.allow()
            ),
            "resolve_binding_chat_display_name": lambda **_kwargs: "",
            "is_thread_not_found_error": lambda exc: isinstance(exc, KeyError),
            "is_thread_not_loaded_error": lambda _exc: False,
            "invalidate_feishu_execution_queue_locked": lambda _binding: None,
            "invalidate_all_feishu_execution_queues_locked": lambda: 0,
            "operational_status": lambda: {"status": "ok"},
            "thread_has_pending_request_locked": lambda _thread_id: False,
            "binding_has_pending_request_locked": lambda _binding: False,
        }
        values.update(overrides)
        application = RuntimeAdminBindingApplication(
            lock=lock,
            binding_runtime=binding_runtime,
            thread_lifecycle=types.SimpleNamespace(),
            ports=RuntimeAdminBindingApplicationPorts(**values),
        )
        return application, lock, binding_runtime, summaries, submitted

    @staticmethod
    def _bind_thread(lock, binding_runtime, binding, thread_id: str) -> None:
        with lock:
            binding_runtime._get_or_create_runtime_state_locked(binding)
            binding_runtime.bind_thread_locked(
                binding_runtime.resident_session_snapshot_locked(binding).handle,
                thread_id=thread_id,
                thread_title="stored title",
                working_dir="/workspace",
            )

    def test_list_response_owns_name_enrichment_and_cache_miss_projection(
        self,
    ) -> None:
        application, lock, binding_runtime, summaries, _submitted = (
            self._make_application()
        )
        binding = ("ou-user", "chat-a")
        self._bind_thread(lock, binding_runtime, binding, "thread-1")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/workspace",
            name="Authoritative name",
            preview="ignored preview",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        result = application.binding_list_response()

        self.assertEqual(result["chat_display_name_cache_miss_count"], 1)
        self.assertEqual(result["bindings"][0]["thread_name"], "Authoritative name")
        self.assertEqual(result["bindings"][0]["chat_display_name"], "")

    def test_prompt_command_rejects_missing_binding_before_effect(self) -> None:
        application, _lock, _runtime, _summaries, submitted = (
            self._make_application()
        )

        result = application.submit_binding_prompt_for_control(
            ("ou-missing", "chat-missing"),
            text="continue",
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], PROMPT_DENIED_BINDING_NOT_FOUND)
        self.assertEqual(submitted, [])

    def test_attach_rechecks_detached_runtime_immediately_before_effect(self) -> None:
        checks: list[str] = []
        attach_calls: list[tuple[tuple[str, str], str]] = []

        def stateful_check(thread_id: str) -> ReasonedCheck:
            checks.append(thread_id)
            if len(checks) == 1:
                return ReasonedCheck.allow()
            return ReasonedCheck.deny(
                "test_runtime_became_live",
                "runtime became live before attach effect",
            )

        application, lock, binding_runtime, summaries, _submitted = (
            self._make_application(
                detached_runtime_attach_check=stateful_check,
                attach_binding=lambda binding, thread_id: (
                    attach_calls.append((binding, thread_id))
                    or summaries[thread_id]
                ),
            )
        )
        binding = ("ou-user", "chat-a")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/workspace",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        self._bind_thread(lock, binding_runtime, binding, "thread-1")
        with lock:
            binding_runtime.detach_binding_locked(binding)

        with self.assertRaisesRegex(ValueError, "became live"):
            application.attach_binding(binding, writer_binding=binding)

        self.assertEqual(checks, ["thread-1", "thread-1"])
        self.assertEqual(attach_calls, [])

    def test_attach_runs_once_after_both_detached_runtime_checks_allow(self) -> None:
        checks: list[str] = []
        attach_calls: list[tuple[tuple[str, str], str]] = []

        def allow_check(thread_id: str) -> ReasonedCheck:
            checks.append(thread_id)
            return ReasonedCheck.allow()

        application, lock, binding_runtime, summaries, _submitted = (
            self._make_application(
                detached_runtime_attach_check=allow_check,
                attach_binding=lambda binding, thread_id: (
                    attach_calls.append((binding, thread_id))
                    or summaries[thread_id]
                ),
            )
        )
        binding = ("ou-user", "chat-a")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/workspace",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        self._bind_thread(lock, binding_runtime, binding, "thread-1")
        with lock:
            binding_runtime.detach_binding_locked(binding)

        result = application.attach_binding(binding, writer_binding=binding)

        self.assertTrue(result["changed"])
        self.assertEqual(checks, ["thread-1", "thread-1"])
        self.assertEqual(attach_calls, [(binding, "thread-1")])

    def test_controller_binding_facades_only_delegate_to_application(self) -> None:
        path = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "runtime_admin"
            / "controller.py"
        )
        module = ast.parse(path.read_text(encoding="utf-8"))
        controller = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef)
            and node.name == "RuntimeAdminController"
        )
        facade_names = {
            "binding_inventory_locked",
            "clear_all_bindings_for_control",
            "binding_status_snapshot",
            "detach_binding",
            "attach_binding",
            "fail_close_service_attached_runtime",
            "detach_thread",
        }
        methods = {
            node.name: node
            for node in controller.body
            if isinstance(node, ast.FunctionDef) and node.name in facade_names
        }

        self.assertEqual(set(methods), facade_names)
        for name, method in methods.items():
            with self.subTest(name=name):
                self.assertEqual(len(method.body), 1)
                self.assertIn("_binding_application", ast.unparse(method))


if __name__ == "__main__":
    unittest.main()
