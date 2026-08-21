from __future__ import annotations

import ast
import json
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import bot.focus_runtime.feishu_platform as feishu_platform_module
import bot.focus_runtime.runtime as focus_runtime_module
import bot.focus_runtime.terminal_results as terminal_results_module
from bot.focus_runtime.feishu_platform import FeishuPlatform
from bot.focus_runtime.terminal_results import TerminalResults


_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_OWNER_PATH = pathlib.Path(terminal_results_module.__file__).resolve()
_PLATFORM_PATH = pathlib.Path(feishu_platform_module.__file__).resolve()

_ROOT_METHODS = {
    "_find_last_card_text",
    "_record_terminal_result_card",
    "_record_terminal_result_card_with_execution",
    "_resolve_terminal_result_text",
    "_has_recorded_terminal_result",
    "_publish_terminal_result",
}
_OWNER_METHODS = {
    "find_last_card_text",
    "record_terminal_result_card_with_execution",
    "resolve_terminal_result_text",
    "has_recorded_terminal_result",
    "publish_terminal_result",
}
_OWNER_DEPENDENCIES = {
    "_platform",
    "_store",
    "_resolve_session",
    "_publish_terminal_result",
}


def _class_node(path: pathlib.Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


def _method_node(owner: ast.ClassDef, method_name: str) -> ast.FunctionDef:
    return next(
        node
        for node in owner.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


def _self_stored_attrs(owner: ast.ClassDef) -> set[str]:
    return {
        node.attr
        for node in ast.walk(owner)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def _call_named(owner: ast.AST, function_name: str) -> ast.Call:
    calls = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == function_name
    ]
    if len(calls) != 1:
        raise AssertionError(
            f"expected one {function_name} call, found {len(calls)}"
        )
    return calls[0]


class _Bot:
    def __init__(self) -> None:
        self.app_id = "app-focus"
        self.contexts: dict[str, dict[str, str]] = {}
        self.history: list[SimpleNamespace] = []
        self.raw_results: dict[str, SimpleNamespace] = {}
        self.context_calls: list[str] = []
        self.list_calls: list[dict[str, object]] = []
        self.read_calls: list[tuple[str, dict]] = []
        self.context_error: Exception | None = None
        self.list_error: Exception | None = None

    def get_message_context(self, message_id: str) -> dict[str, str]:
        self.context_calls.append(message_id)
        if self.context_error is not None:
            raise self.context_error
        return dict(self.contexts.get(message_id, {}))

    def list_recent_messages(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        if self.list_error is not None:
            raise self.list_error
        return list(self.history)

    def read_interactive_message(
        self,
        *,
        message_id: str,
        content_dict: dict,
    ) -> SimpleNamespace:
        self.read_calls.append((message_id, content_dict))
        return self.raw_results.get(
            message_id,
            SimpleNamespace(
                card_kind="",
                text="",
                has_authoritative_text=False,
            ),
        )


def _message(
    message_id: str,
    *,
    msg_type: str = "interactive",
    app_id: str = "app-focus",
    content: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        sender=SimpleNamespace(sender_type="app", id=app_id),
        body=SimpleNamespace(
            content=json.dumps(
                {"message": message_id} if content is None else content,
                ensure_ascii=False,
            )
        ),
    )


class _Harness:
    def __init__(self, *, codex_thread_id: str = "codex-thread") -> None:
        self.bot = _Bot()
        self.platform = FeishuPlatform()
        self.platform.attach(self.bot)
        self.store = Mock()
        self.store.get.return_value = ""
        self.store.latest_for_thread.return_value = ""
        self.resolve_session = Mock(
            return_value=SimpleNamespace(current_thread_id=codex_thread_id)
        )
        self.publish = Mock(return_value=True)
        self.owner = TerminalResults(
            platform=self.platform,
            store=self.store,
            resolve_session=self.resolve_session,
            publish_terminal_result=self.publish,
        )


class TerminalResultsBoundaryTests(unittest.TestCase):
    def test_root_has_no_terminal_result_methods_or_self_calls(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        root_methods = {
            node.name
            for node in root.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        root_self_attrs = {
            node.attr
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }

        self.assertTrue(_ROOT_METHODS.isdisjoint(root_methods))
        self.assertTrue(_ROOT_METHODS.isdisjoint(root_self_attrs))

    def test_owner_has_exactly_five_methods_and_dead_wrapper_is_absent(self) -> None:
        owner = _class_node(_OWNER_PATH, "TerminalResults")
        methods = {
            node.name
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "__init__"
        }

        self.assertEqual(methods, _OWNER_METHODS)
        self.assertNotIn("record_terminal_result_card", methods)
        self.assertNotIn("_record_terminal_result_card", _OWNER_PATH.read_text())

    def test_owner_keeps_only_typed_dependencies_and_no_duplicate_authority(self) -> None:
        source = _OWNER_PATH.read_text(encoding="utf-8")
        owner = _class_node(_OWNER_PATH, "TerminalResults")
        initializer = _method_node(owner, "__init__")
        annotations = {
            argument.arg: ast.unparse(argument.annotation)
            for argument in initializer.args.kwonlyargs
        }

        self.assertEqual(
            annotations,
            {
                "platform": "FeishuPlatform",
                "store": "TerminalResultStore",
                "resolve_session": "_ResolveBindingSession",
                "publish_terminal_result": "_PublishTerminalResult",
            },
        )
        self.assertEqual(_self_stored_attrs(owner), _OWNER_DEPENDENCIES)
        self.assertNotIn("FocusRuntime", source)
        self.assertNotIn("bot.focus_runtime.runtime", source)
        self.assertNotIn("self._bot", source)
        self.assertNotIn("self.bot", source)
        self.assertFalse(
            any(isinstance(node, ast.Call) for node in ast.walk(initializer))
        )

        root = _class_node(_ROOT_PATH, "FocusRuntime")
        root_self_attrs = {
            node.attr
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        self.assertNotIn("bot", root_self_attrs)
        self.assertNotIn("_terminal_result_store", root_self_attrs)

        platform = _class_node(_PLATFORM_PATH, "FeishuPlatform")
        self.assertEqual(_self_stored_attrs(platform), {"_bot"})

    def test_dependency_protocols_keep_the_exact_callback_shapes(self) -> None:
        tree = ast.parse(_OWNER_PATH.read_text(encoding="utf-8"))
        protocols = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and any(ast.unparse(base) == "Protocol" for base in node.bases)
        }
        resolve = _method_node(protocols["_ResolveBindingSession"], "__call__")
        publish = _method_node(protocols["_PublishTerminalResult"], "__call__")

        self.assertEqual(
            ast.unparse(resolve.args),
            "self, sender_id: str, chat_id: str, message_id: str=''",
        )
        self.assertEqual(ast.unparse(resolve.returns), "BindingSessionSnapshot")
        self.assertEqual(
            ast.unparse(publish.args),
            "self, chat_id: str, *, final_reply_text: str, "
            "source_execution_message_id: str='', prompt_message_id: str='', "
            "prompt_reply_in_thread: bool=False, thread_id: str=''",
        )
        self.assertEqual(ast.unparse(publish.returns), "bool")

    def test_runtime_wires_resolver_after_attach_and_before_lifecycle_start(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        start = _method_node(root, "start")
        attach = _call_named(start, "self._feishu_platform.attach")
        install = _call_named(start, "set_terminal_result_text_resolver")
        lifecycle_start = _call_named(start, "self._service_runtime_lifecycle.start")

        self.assertLess(attach.lineno, install.lineno)
        self.assertLess(install.lineno, lifecycle_start.lineno)
        self.assertEqual(
            ast.unparse(install.args[0]),
            "self._terminal_results.resolve_terminal_result_text",
        )

        initializer = _method_node(root, "__init__")
        owner_call = _call_named(initializer, "TerminalResults")
        output_call = _call_named(initializer, "ExecutionOutputController")
        recovery_call = _call_named(initializer, "compose_execution_recovery")
        self.assertLess(owner_call.lineno, output_call.lineno)
        self.assertLess(output_call.lineno, recovery_call.lineno)

        owner_ports = {keyword.arg: keyword.value for keyword in owner_call.keywords}
        self.assertEqual(ast.unparse(owner_ports["platform"]), "feishu_platform")
        self.assertEqual(ast.unparse(owner_ports["store"]), "terminal_result_store")
        self.assertEqual(
            ast.unparse(owner_ports["resolve_session"]),
            "self._binding_runtime.resolve_session",
        )
        publish_port = owner_ports["publish_terminal_result"]
        self.assertIsInstance(publish_port, ast.Lambda)
        self.assertEqual(
            ast.unparse(publish_port.body.func),
            "execution_output.publish_terminal_result",
        )

        output_ports = {keyword.arg: keyword.value for keyword in output_call.keywords}
        self.assertEqual(
            ast.unparse(output_ports["record_terminal_result_card"]),
            "terminal_results.record_terminal_result_card_with_execution",
        )
        recovery_ports = {
            keyword.arg: keyword.value for keyword in recovery_call.keywords
        }
        self.assertEqual(
            ast.unparse(recovery_ports["publish_terminal_result"]),
            "terminal_results.publish_terminal_result",
        )
        self.assertEqual(
            ast.unparse(recovery_ports["has_recorded_terminal_result"]),
            "terminal_results.has_recorded_terminal_result",
        )


class TerminalResultsTests(unittest.TestCase):
    def test_find_last_prefers_store_authority_after_app_filter(self) -> None:
        harness = _Harness()
        harness.bot.contexts["trigger"] = {"thread_id": "feishu-topic"}
        harness.bot.history = [
            _message("foreign", msg_type="text", app_id="other-app"),
            _message("owned", msg_type="text"),
        ]
        harness.store.get.side_effect = lambda message_id: {
            "foreign": "must-not-leak",
            "owned": "authoritative raw terminal",
        }.get(message_id, "")

        result = harness.owner.find_last_card_text(
            "ou-user",
            "chat-1",
            message_id="trigger",
        )

        self.assertEqual(result, "authoritative raw terminal")
        self.assertEqual(
            harness.bot.list_calls,
            [
                {
                    "chat_id": "chat-1",
                    "thread_id": "feishu-topic",
                    "limit": 20,
                }
            ],
        )
        harness.resolve_session.assert_called_once_with(
            "ou-user",
            "chat-1",
            "trigger",
        )
        self.assertEqual(harness.store.get.call_args_list, [call("owned")])
        harness.store.latest_for_thread.assert_not_called()
        self.assertEqual(harness.bot.read_calls, [])

    def test_find_last_prefers_raw_terminal_over_execution_projection(self) -> None:
        harness = _Harness()
        harness.bot.history = [_message("execution"), _message("terminal")]
        harness.bot.raw_results = {
            "execution": SimpleNamespace(
                card_kind="execution",
                text="execution fallback",
                has_authoritative_text=False,
            ),
            "terminal": SimpleNamespace(
                card_kind="terminal",
                text="authoritative terminal",
                has_authoritative_text=True,
            ),
        }

        result = harness.owner.find_last_card_text("ou-user", "chat-1")

        self.assertEqual(result, "authoritative terminal")
        self.assertEqual(
            [message_id for message_id, _content in harness.bot.read_calls],
            ["execution", "terminal"],
        )
        harness.store.latest_for_thread.assert_not_called()

    def test_find_last_uses_execution_then_codex_thread_store_fallback(self) -> None:
        execution = _Harness(codex_thread_id="codex-thread-1")
        execution.bot.history = [_message("degraded"), _message("execution")]
        execution.bot.raw_results = {
            "degraded": SimpleNamespace(
                card_kind="terminal",
                text="degraded terminal",
                has_authoritative_text=False,
            ),
            "execution": SimpleNamespace(
                card_kind="execution",
                text="execution fallback",
                has_authoritative_text=False,
            ),
        }
        self.assertEqual(
            execution.owner.find_last_card_text("ou-user", "chat-1"),
            "execution fallback",
        )
        execution.store.latest_for_thread.assert_not_called()

        local = _Harness(codex_thread_id="codex-thread-1")
        local.bot.contexts["trigger"] = {"thread_id": "feishu-topic-1"}
        local.store.latest_for_thread.return_value = "local thread terminal"
        self.assertEqual(
            local.owner.find_last_card_text(
                "ou-user",
                "chat-1",
                message_id="trigger",
            ),
            "local thread terminal",
        )
        self.assertEqual(local.bot.list_calls[0]["thread_id"], "feishu-topic-1")
        local.store.latest_for_thread.assert_called_once_with("codex-thread-1")

    def test_find_last_list_failure_is_fail_closed_without_local_fallback(self) -> None:
        harness = _Harness()
        harness.bot.list_error = RuntimeError("history unavailable")
        harness.store.latest_for_thread.return_value = "must-not-return"

        with self.assertLogs("bot.focus_runtime", level="WARNING"):
            result = harness.owner.find_last_card_text("ou-user", "chat-1")

        self.assertEqual(result, "读取最近卡片失败，请稍后重试。")
        harness.store.latest_for_thread.assert_not_called()

    def test_record_normalizes_ids_and_preserves_raw_text(self) -> None:
        harness = _Harness()

        with patch("bot.focus_runtime.terminal_results.time.time", return_value=42.5):
            harness.owner.record_terminal_result_card_with_execution(
                message_id="  message-1  ",
                execution_message_id="  execution-1  ",
                final_reply_text="  raw text\n",
                terminal_result_id="  ABCDEF  ",
                thread_id="  thread-1  ",
                checksum="  FEDCBA  ",
            )

        record = harness.store.upsert.call_args.args[0]
        self.assertEqual(record.message_id, "message-1")
        self.assertEqual(record.execution_message_id, "execution-1")
        self.assertEqual(record.final_reply_text, "  raw text\n")
        self.assertEqual(record.recorded_at, 42.5)
        self.assertEqual(record.terminal_result_id, "abcdef")
        self.assertEqual(record.thread_id, "thread-1")
        self.assertEqual(record.checksum, "fedcba")

        harness.store.upsert.reset_mock()
        harness.owner.record_terminal_result_card_with_execution(
            message_id="  ",
            execution_message_id="execution-1",
            final_reply_text="text",
        )
        harness.owner.record_terminal_result_card_with_execution(
            message_id="message-1",
            execution_message_id="execution-1",
            final_reply_text="",
        )
        harness.store.upsert.assert_not_called()

    def test_resolve_and_has_delegate_with_exact_normalization(self) -> None:
        harness = _Harness()
        harness.store.get_by_terminal_result_id.return_value = "raw result"
        harness.store.has_execution_result.return_value = True
        projection = SimpleNamespace(
            terminal_result_id="  ABCDEF  ",
            terminal_result_checksum="  FEDCBA  ",
        )

        self.assertEqual(
            harness.owner.resolve_terminal_result_text(projection),
            "raw result",
        )
        harness.store.get_by_terminal_result_id.assert_called_once_with(
            "abcdef",
            checksum="fedcba",
        )
        self.assertTrue(
            harness.owner.has_recorded_terminal_result(
                execution_message_id=" execution-1 ",
                final_reply_text=" exact raw text ",
            )
        )
        harness.store.has_execution_result.assert_called_once_with(
            execution_message_id=" execution-1 ",
            final_reply_text=" exact raw text ",
        )

        harness.store.get_by_terminal_result_id.reset_mock()
        self.assertEqual(
            harness.owner.resolve_terminal_result_text(
                SimpleNamespace(terminal_result_id="")
            ),
            "",
        )
        harness.store.get_by_terminal_result_id.assert_not_called()

    def test_publish_prefers_explicit_thread_and_forwards_one_effect(self) -> None:
        harness = _Harness()
        harness.bot.context_error = RuntimeError("must not read context")

        result = harness.owner.publish_terminal_result(
            "chat-1",
            final_reply_text="raw result",
            source_execution_message_id="execution-1",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=True,
            thread_id="  explicit-thread  ",
        )

        self.assertTrue(result)
        self.assertEqual(harness.bot.context_calls, [])
        harness.publish.assert_called_once_with(
            "chat-1",
            final_reply_text="raw result",
            source_execution_message_id="execution-1",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=True,
            thread_id="explicit-thread",
        )

    def test_publish_uses_context_or_blank_and_never_repeats_effect(self) -> None:
        context = _Harness()
        context.bot.contexts["prompt-1"] = {"thread_id": " feishu-topic "}
        self.assertTrue(
            context.owner.publish_terminal_result(
                "chat-1",
                final_reply_text="raw result",
                prompt_message_id="prompt-1",
            )
        )
        context.publish.assert_called_once_with(
            "chat-1",
            final_reply_text="raw result",
            source_execution_message_id="",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=False,
            thread_id="feishu-topic",
        )

        failed_context = _Harness()
        failed_context.bot.context_error = RuntimeError("context unavailable")
        failed_context.publish.return_value = False
        self.assertFalse(
            failed_context.owner.publish_terminal_result(
                "chat-1",
                final_reply_text="raw result",
                prompt_message_id="prompt-1",
            )
        )
        failed_context.publish.assert_called_once_with(
            "chat-1",
            final_reply_text="raw result",
            source_execution_message_id="",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=False,
            thread_id="",
        )


if __name__ == "__main__":
    unittest.main()
