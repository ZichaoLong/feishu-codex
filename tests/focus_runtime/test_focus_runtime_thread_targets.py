from __future__ import annotations

import ast
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import bot.focus_runtime.runtime as focus_runtime_module
import bot.focus_runtime.thread_targets as thread_targets_module
from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcTransportError,
)
from bot.focus_runtime.thread_targets import CodexThreadTargetService
from bot.turn_interrupt_audit import record_turn_interrupt_dispatch_attempt


_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_OWNER_PATH = pathlib.Path(thread_targets_module.__file__).resolve()
_EXTRACTED_ROOT_METHODS = {
    "_is_turn_thread_not_found_error",
    "_is_request_timeout_error",
    "_runtime_recovery_reason",
    "_interrupt_running_turn",
    "_resolve_thread_name_target_for_control",
    "_resolve_thread_target_for_control_params",
    "_read_thread_snapshot_authoritatively",
    "_read_direct_thread_summary_authoritatively",
    "_read_thread_summary_authoritatively",
    "_rename_direct_thread",
    "_begin_web_thread_page",
    "_lookup_thread_summary_in_bounded_list",
    "_is_thread_not_found_error",
    "_is_thread_not_loaded_error",
    "_is_pre_send_error",
    "_is_turn_interrupt_rejected_error",
    "_is_transport_disconnect",
    "_list_global_threads",
    "_list_visible_current_dir_threads",
}
_PUBLIC_OWNER_METHODS = {name.removeprefix("_") for name in _EXTRACTED_ROOT_METHODS}
_EXPECTED_DEPENDENCY_ATTRS = {
    "_adapter",
    "_binding_runtime",
    "_thread_runtime_authority",
    "_direct_thread_targets",
    "_thread_list_query_limit",
}


def _class_node(path: pathlib.Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


def _summary(
    thread_id: str = "thread-1",
    *,
    source: str = "appServer",
    name: str = "demo",
    subagent_kind: str | None = None,
    parent_thread_id: str | None = None,
) -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread_id,
        cwd="/tmp/project",
        name=name,
        preview="hello",
        created_at=1,
        updated_at=2,
        source=source,
        status="idle",
        subagent_kind=subagent_kind,
        parent_thread_id=parent_thread_id,
    )


def _wrapped(error: Exception) -> RuntimeError:
    outer = RuntimeError("presentation wrapper")
    outer.__cause__ = error
    return outer


class CodexThreadTargetBoundaryTests(unittest.TestCase):
    def test_exact_thread_target_methods_live_only_on_the_capability_owner(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        owner = _class_node(_OWNER_PATH, "CodexThreadTargetService")
        root_methods = {
            node.name
            for node in root.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        owner_methods = {
            node.name
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "__init__"
        }

        self.assertEqual(root_methods & _EXTRACTED_ROOT_METHODS, set())
        self.assertEqual(owner_methods, _PUBLIC_OWNER_METHODS)

    def test_owner_holds_only_explicit_dependencies_and_not_the_root(self) -> None:
        source = _OWNER_PATH.read_text(encoding="utf-8")
        owner = _class_node(_OWNER_PATH, "CodexThreadTargetService")
        initializer = next(
            node
            for node in owner.body
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
        self.assertNotIn("bot.focus_runtime.runtime", source)
        self.assertEqual(assigned_attrs, _EXPECTED_DEPENDENCY_ATTRS)
        self.assertTrue(
            {
                "_bot",
                "_focus_runtime",
                "_handler",
                "_runtime",
                "_lock",
                "_store",
                "_timer",
                "_ledger",
            }.isdisjoint(assigned_attrs)
        )


class CodexThreadTargetServiceTests(unittest.TestCase):
    def make_service(self, *, query_limit: int = 37):
        adapter = Mock()
        adapter.list_threads_all.return_value = []
        adapter.list_threads.return_value = ([], None)
        binding_runtime = Mock()
        binding_runtime.resolve_session.return_value = SimpleNamespace(
            working_dir="/tmp/current-project"
        )
        thread_runtime_authority = Mock()
        direct_thread_targets = Mock()
        service = CodexThreadTargetService(
            adapter=adapter,
            binding_runtime=binding_runtime,
            thread_runtime_authority=thread_runtime_authority,
            direct_thread_targets=direct_thread_targets,
            thread_list_query_limit=query_limit,
        )
        return (
            service,
            adapter,
            binding_runtime,
            thread_runtime_authority,
            direct_thread_targets,
        )

    def test_error_classifiers_preserve_chain_and_direct_only_boundaries(self) -> None:
        no_rollout = CodexRpcError(
            "thread/read",
            {"message": "No rollout found for thread id thread-1"},
        )
        turn_not_found = CodexRpcError(
            "turn/start",
            {"message": "Thread not found: thread-1"},
        )
        not_loaded = CodexRpcError(
            "thread/read",
            {"message": "Thread not loaded: thread-1"},
        )
        pre_send = CodexRpcPreSendError(
            "thread/resume",
            TimeoutError("initialize timed out"),
        )
        request_timeout = TimeoutError("Codex request timed out: thread/read")
        interrupt_rejected = CodexRpcError(
            "turn/interrupt",
            {"message": "no active turn to interrupt"},
        )
        interrupt_mismatched = CodexRpcError(
            "turn/interrupt",
            {"message": "expected active turn id turn-1 but found turn-2"},
        )

        self.assertTrue(CodexThreadTargetService.is_thread_not_found_error(no_rollout))
        self.assertTrue(
            CodexThreadTargetService.is_thread_not_found_error(_wrapped(no_rollout))
        )
        self.assertFalse(
            CodexThreadTargetService.is_thread_not_found_error(turn_not_found)
        )

        self.assertTrue(
            CodexThreadTargetService.is_turn_thread_not_found_error(turn_not_found)
        )
        self.assertFalse(
            CodexThreadTargetService.is_turn_thread_not_found_error(
                _wrapped(turn_not_found)
            )
        )
        self.assertTrue(CodexThreadTargetService.is_thread_not_loaded_error(not_loaded))
        self.assertFalse(
            CodexThreadTargetService.is_thread_not_loaded_error(_wrapped(not_loaded))
        )

        self.assertTrue(CodexThreadTargetService.is_pre_send_error(pre_send))
        self.assertFalse(CodexThreadTargetService.is_pre_send_error(_wrapped(pre_send)))
        self.assertTrue(
            CodexThreadTargetService.is_request_timeout_error(request_timeout)
        )
        self.assertFalse(
            CodexThreadTargetService.is_request_timeout_error(
                TimeoutError("request timed out")
            )
        )
        self.assertFalse(
            CodexThreadTargetService.is_request_timeout_error(
                _wrapped(request_timeout)
            )
        )
        self.assertTrue(
            CodexThreadTargetService.is_turn_interrupt_rejected_error(
                interrupt_rejected
            )
        )
        self.assertTrue(
            CodexThreadTargetService.is_turn_interrupt_rejected_error(
                interrupt_mismatched
            )
        )
        for other in (
            CodexRpcTransportError(
                "turn/interrupt",
                {"message": "no active turn to interrupt"},
            ),
            CodexRpcError(
                "turn/start",
                {"message": "no active turn to interrupt"},
            ),
            CodexRpcError("turn/interrupt", {"message": "other rejection"}),
            CodexRpcError(
                "turn/interrupt",
                {"message": "expected active turn id  but found "},
            ),
            _wrapped(interrupt_rejected),
        ):
            with self.subTest(interrupt_rejection=type(other).__name__):
                self.assertFalse(
                    CodexThreadTargetService.is_turn_interrupt_rejected_error(other)
                )

    def test_transport_classifier_accepts_typed_and_named_chain_errors_only(self) -> None:
        typed = CodexRpcTransportError(
            "thread/resume",
            {"code": -32000, "message": "transport reset"},
        )
        websocket_closed = CodexRpcError(
            "thread/read",
            {"message": "Codex websocket disconnected"},
        )
        app_server_closed = CodexRpcError(
            "thread/read",
            {"message": "Codex app-server closed"},
        )
        pre_send = CodexRpcPreSendError(
            "thread/resume",
            TimeoutError("initialize timed out"),
        )

        for error in (typed, websocket_closed, app_server_closed):
            with self.subTest(error=error):
                self.assertTrue(CodexThreadTargetService.is_transport_disconnect(error))
                self.assertTrue(
                    CodexThreadTargetService.is_transport_disconnect(_wrapped(error))
                )
        self.assertFalse(CodexThreadTargetService.is_transport_disconnect(pre_send))
        self.assertFalse(
            CodexThreadTargetService.is_transport_disconnect(
                CodexRpcError(
                    "thread/read",
                    {"message": "codex websocket disconnected"},
                )
            )
        )

    def test_runtime_recovery_reason_preserves_named_error_text(self) -> None:
        timeout = TimeoutError("Codex request timed out: thread/read")
        rpc_error = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )
        generic = RuntimeError("local recovery failed")

        self.assertEqual(
            CodexThreadTargetService.runtime_recovery_reason(timeout),
            "Codex request timed out: thread/read",
        )
        self.assertEqual(
            CodexThreadTargetService.runtime_recovery_reason(rpc_error),
            "thread not loaded: thread-1",
        )
        self.assertEqual(
            CodexThreadTargetService.runtime_recovery_reason(generic),
            "local recovery failed",
        )

    def test_authoritative_read_validates_before_remembering(self) -> None:
        service, adapter, _, _, direct_targets = self.make_service()
        root = _summary()
        child = _summary(
            "child-1",
            source="subAgent",
            subagent_kind="threadSpawn",
            parent_thread_id="thread-1",
        )
        events: list[str] = []

        adapter.read_thread.side_effect = lambda *_args, **_kwargs: (
            events.append("read") or ThreadSnapshot(summary=root)
        )
        direct_targets.remember.side_effect = (
            lambda _summary: events.append("remember")
        )

        result = service.read_direct_thread_summary_authoritatively(
            " thread-1 ",
            original_arg="demo",
            operation="测试",
        )

        self.assertIs(result, root)
        self.assertEqual(events, ["read", "remember"])
        adapter.read_thread.assert_called_once_with("thread-1", include_turns=False)

        for invalid in (
            child,
            _summary("different-thread"),
        ):
            with self.subTest(thread_id=invalid.thread_id):
                adapter.reset_mock()
                direct_targets.reset_mock()
                adapter.read_thread.return_value = ThreadSnapshot(summary=invalid)
                adapter.read_thread.side_effect = None
                with self.assertRaises(ValueError):
                    service.read_direct_thread_summary_authoritatively(
                        "child-1" if invalid is child else "thread-1",
                        original_arg="target",
                        operation="测试",
                    )
                direct_targets.remember.assert_not_called()

    def test_not_found_read_maps_exact_target_text_without_remembering(self) -> None:
        service, adapter, _, _, direct_targets = self.make_service()
        adapter.read_thread.side_effect = CodexRpcError(
            "thread/read",
            {"message": "no rollout found for thread id missing"},
        )

        with self.assertRaisesRegex(
            ValueError,
            r"未找到匹配的线程：`original-name`",
        ):
            service.read_direct_thread_summary_authoritatively(
                "missing",
                original_arg="original-name",
                operation="读取",
            )

        direct_targets.remember.assert_not_called()

    def test_rename_proves_and_remembers_before_upstream_mutation(self) -> None:
        service, adapter, _, _, direct_targets = self.make_service()
        root = _summary()
        events: list[str] = []
        adapter.read_thread.side_effect = lambda *_args, **_kwargs: (
            events.append("read") or ThreadSnapshot(summary=root)
        )
        direct_targets.remember.side_effect = (
            lambda _summary: events.append("remember")
        )
        adapter.rename_thread.side_effect = (
            lambda _thread_id, _name: events.append("rename")
        )

        service.rename_direct_thread("thread-1", "new title")

        self.assertEqual(events, ["read", "remember", "rename"])
        adapter.rename_thread.assert_called_once_with("thread-1", "new title")

        adapter.reset_mock()
        direct_targets.reset_mock()
        adapter.read_thread.return_value = ThreadSnapshot(
            summary=_summary(
                "child-1",
                source="subAgent",
                subagent_kind="threadSpawn",
                parent_thread_id="thread-1",
            )
        )
        adapter.read_thread.side_effect = None
        with self.assertRaisesRegex(ValueError, "ThreadSpawn"):
            service.rename_direct_thread("child-1", "forbidden")
        direct_targets.remember.assert_not_called()
        adapter.rename_thread.assert_not_called()

    def test_control_selectors_resolve_exactly_one_authoritative_target(self) -> None:
        service, adapter, _, _, direct_targets = self.make_service(query_limit=41)
        root = _summary(name="demo")
        adapter.list_threads.return_value = ([root], None)
        adapter.read_thread.return_value = ThreadSnapshot(summary=root)

        resolved = service.resolve_thread_target_for_control_params(
            {"thread_name": " demo "}
        )

        self.assertIs(resolved, root)
        adapter.list_threads.assert_called_once_with(
            limit=41,
            cursor=None,
            sort_key="updated_at",
            model_providers=[],
        )
        adapter.read_thread.assert_called_once_with("thread-1", include_turns=False)
        direct_targets.remember.assert_called_once_with(root)

        for invalid in ({}, {"thread_id": "thread-1", "thread_name": "demo"}):
            with self.subTest(params=invalid):
                with self.assertRaisesRegex(
                    ValueError,
                    "必须且只能提供",
                ):
                    service.resolve_thread_target_for_control_params(invalid)

        with self.assertRaisesRegex(ValueError, "thread_name 不能为空"):
            service.resolve_thread_name_target_for_control("  ")

    def test_global_and_current_directory_lists_keep_scope_and_query_limit(self) -> None:
        service, adapter, binding_runtime, _, _ = self.make_service(query_limit=23)
        root = _summary()
        adapter.list_threads_all.return_value = [root]

        self.assertEqual(service.list_global_threads(), [root])
        adapter.list_threads_all.assert_called_once_with(
            limit=23,
            sort_key="updated_at",
            model_providers=[],
            archived=None,
        )

        adapter.list_threads_all.reset_mock()
        self.assertEqual(
            service.list_visible_current_dir_threads(
                "ou-user",
                "chat-1",
                message_id="message-1",
            ),
            [root],
        )
        binding_runtime.resolve_session.assert_called_once_with(
            "ou-user",
            "chat-1",
            "message-1",
        )
        adapter.list_threads_all.assert_called_once_with(
            cwd="/tmp/current-project",
            limit=23,
            sort_key="updated_at",
            model_providers=[],
            archived=None,
        )

    def test_web_resume_forwards_options_and_classifies_cli_transport(self) -> None:
        service, adapter, _, runtime_authority, _ = self.make_service()
        cli_thread = _summary(source="cli")
        pending = object()
        adapter.list_threads_all.return_value = [cli_thread]
        runtime_authority.begin_resume_thread_page.return_value = pending

        result = service.begin_web_thread_page(
            "thread-1",
            original_arg="demo",
            limit=17,
            model="",
            config_overrides={"model_reasoning_effort": "high"},
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
        )

        self.assertIs(result, pending)
        runtime_authority.begin_resume_thread_page.assert_called_once_with(
            "thread-1",
            limit=17,
            config_overrides={"model_reasoning_effort": "high"},
            model=None,
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
        )

        transport = CodexRpcTransportError(
            "thread/resume",
            {"message": "transport reset"},
        )
        runtime_authority.begin_resume_thread_page.side_effect = transport
        with self.assertRaisesRegex(
            RuntimeError,
            "Codex 当前无法通过 app-server 恢复这个 CLI 线程",
        ) as caught:
            service.begin_web_thread_page(
                "thread-1",
                original_arg="demo",
                limit=17,
            )
        self.assertIs(caught.exception.__cause__, transport)

    def test_web_resume_prioritizes_not_found_and_does_not_mislabel_non_cli(self) -> None:
        service, adapter, _, runtime_authority, _ = self.make_service()
        adapter.list_threads_all.return_value = [_summary(source="cli")]
        missing = CodexRpcError(
            "thread/resume",
            {"message": "no rollout found for thread id thread-1"},
        )
        runtime_authority.begin_resume_thread_page.side_effect = missing

        with self.assertRaisesRegex(
            ValueError,
            r"未找到匹配的线程：`original-name`",
        ) as caught:
            service.begin_web_thread_page(
                "thread-1",
                original_arg="original-name",
                limit=10,
            )
        self.assertIs(caught.exception.__cause__, missing)

        transport = CodexRpcTransportError(
            "thread/resume",
            {"message": "transport reset"},
        )
        for listed_threads in ([_summary(source="appServer")], []):
            with self.subTest(listed_threads=listed_threads):
                adapter.list_threads_all.return_value = listed_threads
                runtime_authority.begin_resume_thread_page.side_effect = transport
                with self.assertRaises(CodexRpcTransportError) as propagated:
                    service.begin_web_thread_page(
                        "thread-1",
                        original_arg="thread-1",
                        limit=10,
                    )
                self.assertIs(propagated.exception, transport)

    def test_interrupt_normalizes_thread_id_and_preserves_turn_id(self) -> None:
        service, adapter, _, _, direct_targets = self.make_service()
        root = _summary()
        order: list[str] = []
        adapter.read_thread.side_effect = lambda *_args, **_kwargs: (
            order.append("authority-read") or ThreadSnapshot(summary=root)
        )
        adapter.interrupt_turn.side_effect = lambda **_kwargs: order.append("transport")

        def _record_audit(**kwargs: object) -> None:
            order.append("audit")
            record_turn_interrupt_dispatch_attempt(**kwargs)  # type: ignore[arg-type]

        with patch.object(
            thread_targets_module,
            "record_turn_interrupt_dispatch_attempt",
            side_effect=_record_audit,
        ):
            with self.assertLogs("bot.turn_interrupt_audit", level="INFO") as audit:
                service.interrupt_running_turn(
                    thread_id="  thread-1  ",
                    turn_id=" turn-raw ",
                )

        self.assertEqual(order, ["authority-read", "audit", "transport"])
        self.assertEqual(
            adapter.interrupt_turn.call_args_list,
            [call(thread_id="thread-1", turn_id=" turn-raw ")],
        )
        adapter.read_thread.assert_called_once_with("thread-1", include_turns=False)
        direct_targets.remember.assert_called_once_with(root)
        audit_text = "\n".join(audit.output)
        self.assertIn("source=feishu_binding", audit_text)
        self.assertNotIn("thread-1", audit_text)
        self.assertNotIn("turn-raw", audit_text)

        adapter.reset_mock()
        direct_targets.reset_mock()
        adapter.read_thread.side_effect = None
        adapter.interrupt_turn.side_effect = None
        adapter.read_thread.return_value = ThreadSnapshot(
            summary=_summary(
                "child-1",
                source="subAgent",
                subagent_kind="threadSpawn",
                parent_thread_id="thread-1",
            )
        )

        with self.assertRaisesRegex(CodexRpcPreSendError, "ThreadSpawn") as caught:
            service.interrupt_running_turn(
                thread_id="child-1",
                turn_id="child-turn",
            )

        self.assertIsInstance(caught.exception.cause, ValueError)
        adapter.interrupt_turn.assert_not_called()
        direct_targets.remember.assert_not_called()

    def test_interrupt_direct_root_read_failures_are_pre_send_without_audit(
        self,
    ) -> None:
        service, adapter, _, _, direct_targets = self.make_service()
        failures = (
            CodexRpcTransportError(
                "thread/read",
                {"message": "transport reset"},
            ),
            TimeoutError("Codex request timed out: thread/read"),
            ValueError("direct-root policy rejected"),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                adapter.reset_mock()
                direct_targets.reset_mock()
                adapter.read_thread.side_effect = failure
                with patch.object(
                    thread_targets_module,
                    "record_turn_interrupt_dispatch_attempt",
                ) as audit:
                    with self.assertRaises(CodexRpcPreSendError) as caught:
                        service.interrupt_running_turn(
                            thread_id="thread-1",
                            turn_id="turn-1",
                        )

                self.assertIs(caught.exception.cause, failure)
                audit.assert_not_called()
                adapter.interrupt_turn.assert_not_called()
                direct_targets.remember.assert_not_called()


if __name__ == "__main__":
    unittest.main()
