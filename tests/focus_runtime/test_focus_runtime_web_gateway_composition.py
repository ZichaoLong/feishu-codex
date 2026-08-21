from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import Mock

import bot.focus_runtime.runtime as focus_runtime_module
import bot.focus_runtime.web_gateway_composition as composition_module
from bot.focus_runtime.web_gateway_composition import compose_web_gateway
from bot.service_runtime_lifecycle import ServiceRuntimeIngressDispatcher
from bot.web_runtime.backend_reset_controller import WebBackendResetController
from bot.web_runtime.controller import WebRuntimeController
from bot.web_runtime.gateway import WebGatewayConfig
from bot.web_runtime.projection import FocusWebProjection


_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_COMPOSITION_PATH = pathlib.Path(composition_module.__file__).resolve()


class FocusRuntimeWebGatewayCompositionTests(unittest.TestCase):
    def _build(self):
        web_runtime = Mock(spec=WebRuntimeController)
        backend_reset = Mock(spec=WebBackendResetController)
        ingress = Mock(spec=ServiceRuntimeIngressDispatcher)
        runtime_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        runtime_result = object()

        def runtime_call(callback, *args, **kwargs):
            runtime_calls.append((callback, args, kwargs))
            return runtime_result

        operator_status = Mock(return_value={"status": "ok"})
        gateway = compose_web_gateway(
            config=WebGatewayConfig(enabled=False),
            data_dir=pathlib.Path("/focus-data"),
            projection=FocusWebProjection(),
            web_runtime=web_runtime,
            backend_reset=lambda: backend_reset,
            ingress=lambda: ingress,
            runtime_call=runtime_call,
            operator_status=operator_status,
        )
        return (
            gateway,
            web_runtime,
            backend_reset,
            ingress,
            runtime_calls,
            runtime_result,
            operator_status,
        )

    def test_runtime_ports_preserve_exact_callback_and_arguments(self) -> None:
        (
            gateway,
            web_runtime,
            backend_reset,
            _ingress,
            runtime_calls,
            runtime_result,
            _operator_status,
        ) = self._build()
        ports = gateway._ports  # noqa: SLF001 - direct composition evidence
        cases = (
            (lambda: ports.meta("tab"), web_runtime.meta, ("tab",), {}),
            (
                lambda: ports.backend_reset_preview(),
                backend_reset.preview,
                (),
                {},
            ),
            (
                lambda: ports.backend_reset_execute(force=True),
                backend_reset.execute,
                (),
                {"force": True},
            ),
            (
                lambda: ports.update_profile("tab", {"cwd": "/work"}, intent=3),
                web_runtime.update_profile,
                ("tab", {"cwd": "/work"}),
                {"intent": 3},
            ),
            (
                lambda: ports.next_turn_settings(),
                web_runtime.next_turn_settings,
                (),
                {},
            ),
            (
                lambda: ports.update_next_turn_settings("tab", {"model": "m"}),
                web_runtime.update_next_turn_settings,
                ("tab", {"model": "m"}),
                {},
            ),
            (
                lambda: ports.stage_attachment("tab", name="a.txt"),
                web_runtime.stage_attachment,
                ("tab",),
                {"name": "a.txt"},
            ),
            (
                lambda: ports.attachment_download("attachment-1"),
                web_runtime.attachment_download,
                ("attachment-1",),
                {},
            ),
            (
                lambda: ports.start_thread("tab", text="new"),
                web_runtime.start_thread,
                ("tab",),
                {"text": "new"},
            ),
            (
                lambda: ports.prompt_result(
                    "tab",
                    "thread-1",
                    mutation_id="11111111-1111-4111-8111-111111111111",
                ),
                web_runtime.prompt_result,
                ("tab", "thread-1"),
                {"mutation_id": "11111111-1111-4111-8111-111111111111"},
            ),
            (
                lambda: ports.interrupt("tab", "thread-1", turn_id="turn-1"),
                web_runtime.interrupt,
                ("tab", "thread-1"),
                {"turn_id": "turn-1"},
            ),
            (
                lambda: ports.resolve_unknown_mutation(
                    "tab", "thread-1", resolution="retry"
                ),
                web_runtime.resolve_unknown_mutation,
                ("tab", "thread-1"),
                {"resolution": "retry"},
            ),
            (
                lambda: ports.rename_thread("tab", "thread-1", name="renamed"),
                web_runtime.rename_thread,
                ("tab", "thread-1"),
                {"name": "renamed"},
            ),
            (
                lambda: ports.compact_thread("tab", "thread-1"),
                web_runtime.compact_thread,
                ("tab", "thread-1"),
                {},
            ),
            (
                lambda: ports.start_review("tab", "thread-1", target={}),
                web_runtime.start_review,
                ("tab", "thread-1"),
                {"target": {}},
            ),
            (
                lambda: ports.goal("tab", "thread-1"),
                web_runtime.goal,
                ("tab", "thread-1"),
                {},
            ),
            (
                lambda: ports.set_goal("tab", "thread-1", objective="ship"),
                web_runtime.set_goal,
                ("tab", "thread-1"),
                {"objective": "ship"},
            ),
            (
                lambda: ports.clear_goal("tab", "thread-1", intent=4),
                web_runtime.clear_goal,
                ("tab", "thread-1"),
                {"intent": 4},
            ),
            (
                lambda: ports.archive_thread("tab", "thread-1"),
                web_runtime.archive_thread,
                ("tab", "thread-1"),
                {},
            ),
            (
                lambda: ports.unarchive_thread("tab", "thread-1"),
                web_runtime.unarchive_thread,
                ("tab", "thread-1"),
                {},
            ),
            (
                lambda: ports.delete_thread("tab", "thread-1", confirmation="yes"),
                web_runtime.delete_thread,
                ("tab", "thread-1"),
                {"confirmation": "yes"},
            ),
            (
                lambda: ports.respond_request("tab", 7, decision="accept"),
                web_runtime.respond_request,
                ("tab", 7),
                {"decision": "accept"},
            ),
            (
                lambda: ports.document_intent_generation_floor("tab"),
                web_runtime.document_intent_generation_floor,
                ("tab",),
                {},
            ),
            (
                lambda: ports.client_connected("tab"),
                web_runtime.client_connected,
                ("tab",),
                {},
            ),
            (
                lambda: ports.client_transport_disconnected("tab"),
                web_runtime.client_transport_disconnected,
                ("tab",),
                {},
            ),
            (
                lambda: ports.client_document_reissued("tab"),
                web_runtime.client_document_reissued,
                ("tab",),
                {},
            ),
            (
                lambda: ports.client_disconnected("tab"),
                web_runtime.client_disconnected,
                ("tab",),
                {},
            ),
        )

        for invoke, expected_callback, expected_args, expected_kwargs in cases:
            with self.subTest(callback=expected_callback._mock_name):
                runtime_calls.clear()
                self.assertIs(invoke(), runtime_result)
                self.assertEqual(
                    runtime_calls,
                    [(expected_callback, expected_args, expected_kwargs)],
                )

    def test_staged_reads_keep_the_exact_ingress_dispatcher_mapping(self) -> None:
        (
            gateway,
            web_runtime,
            _backend_reset,
            ingress,
            runtime_calls,
            _runtime_result,
            _operator_status,
        ) = self._build()
        ports = gateway._ports  # noqa: SLF001 - direct composition evidence
        prepared = object()
        ingress.prepare_external_transaction.return_value = prepared

        self.assertIs(ports.prepare_list_threads(scope="global"), prepared)
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_list_threads,
            scope="global",
        )
        ingress.prepare_external_transaction.reset_mock()

        self.assertIs(
            ports.prepare_read_thread("tab", "thread-1", turn_limit=10),
            prepared,
        )
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_read_thread,
            "tab",
            "thread-1",
            turn_limit=10,
        )
        ingress.prepare_external_transaction.reset_mock()

        self.assertIs(
            ports.prepare_list_older_turns(
                "tab",
                "thread-1",
                cursor="opaque",
            ),
            prepared,
        )
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_list_older_turns,
            "tab",
            "thread-1",
            cursor="opaque",
        )
        ingress.prepare_external_transaction.reset_mock()

        self.assertIs(
            ports.prepare_tool_detail(
                "tab",
                "thread-1",
                "turn-1",
                "item-1",
                view="preview",
                change_index=2,
            ),
            prepared,
        )
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_tool_detail,
            "tab",
            "thread-1",
            "turn-1",
            "item-1",
            view="preview",
            change_index=2,
        )
        ingress.prepare_external_transaction.reset_mock()

        self.assertIs(
            ports.prepare_conversation_search(
                "tab",
                "thread-1",
                query="needle",
            ),
            prepared,
        )
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_conversation_search,
            "tab",
            "thread-1",
            query="needle",
        )

        effect = object()
        ingress.run_prepared_external_transaction.return_value = effect
        self.assertIs(ports.run_prepared_thread_read(prepared), effect)
        ingress.run_prepared_external_transaction.assert_called_once_with(
            prepared,
            web_runtime.run_prepared_thread_read,
        )
        ingress.abandon_prepared_external_transaction.return_value = True
        self.assertTrue(ports.abandon_prepared_thread_read(prepared))
        ingress.abandon_prepared_external_transaction.assert_called_once_with(prepared)
        self.assertEqual(runtime_calls, [])

    def test_staged_prompt_keeps_exact_ingress_and_cancellation_mapping(self) -> None:
        (
            gateway,
            web_runtime,
            _backend_reset,
            ingress,
            runtime_calls,
            _runtime_result,
            _operator_status,
        ) = self._build()
        ports = gateway._ports  # noqa: SLF001 - direct composition evidence
        prompt_preparation = object()
        prepared = Mock(preparation=prompt_preparation)
        ingress.prepare_external_transaction.return_value = prepared

        self.assertIs(
            ports.prepare_prompt(
                "tab",
                "thread-1",
                mutation_id="11111111-1111-4111-8111-111111111111",
            ),
            prepared,
        )
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_prompt,
            "tab",
            "thread-1",
            mutation_id="11111111-1111-4111-8111-111111111111",
        )

        effect = object()
        ingress.run_prepared_external_transaction.return_value = effect
        self.assertIs(ports.run_prepared_prompt(prepared), effect)
        ingress.run_prepared_external_transaction.assert_called_once_with(
            prepared,
            web_runtime.run_prepared_prompt,
        )

        ingress.abandon_prepared_external_transaction.return_value = False
        self.assertFalse(ports.abandon_prepared_prompt(prepared))
        self.assertEqual(runtime_calls, [])

        ingress.abandon_prepared_external_transaction.return_value = True
        self.assertTrue(ports.abandon_prepared_prompt(prepared))
        self.assertEqual(
            runtime_calls,
            [(web_runtime.abandon_prompt, (prompt_preparation,), {})],
        )

    def test_operator_status_remains_outside_runtime_loop(self) -> None:
        (
            gateway,
            _web_runtime,
            _backend_reset,
            _ingress,
            runtime_calls,
            _runtime_result,
            operator_status,
        ) = self._build()

        self.assertEqual(gateway._ports.operator_status(), {"status": "ok"})  # noqa: SLF001
        operator_status.assert_called_once_with()
        self.assertEqual(runtime_calls, [])

    def test_late_bound_owners_are_resolved_only_when_their_ports_run(self) -> None:
        web_runtime = Mock(spec=WebRuntimeController)
        backend_reset = Mock(spec=WebBackendResetController)
        ingress = Mock(spec=ServiceRuntimeIngressDispatcher)
        backend_reset_provider = Mock(return_value=backend_reset)
        ingress_provider = Mock(return_value=ingress)
        runtime_call = Mock()
        gateway = compose_web_gateway(
            config=WebGatewayConfig(enabled=False),
            data_dir=pathlib.Path("/focus-data"),
            projection=FocusWebProjection(),
            web_runtime=web_runtime,
            backend_reset=backend_reset_provider,
            ingress=ingress_provider,
            runtime_call=runtime_call,
            operator_status=Mock(),
        )

        backend_reset_provider.assert_not_called()
        ingress_provider.assert_not_called()

        gateway._ports.backend_reset_preview()  # noqa: SLF001
        backend_reset_provider.assert_called_once_with()
        runtime_call.assert_called_once_with(backend_reset.preview)

        prepared = object()
        ingress.prepare_external_transaction.return_value = prepared
        self.assertIs(gateway._ports.prepare_list_threads(limit=10), prepared)  # noqa: SLF001
        ingress_provider.assert_called_once_with()
        ingress.prepare_external_transaction.assert_called_once_with(
            web_runtime.prepare_list_threads,
            limit=10,
        )

    def test_root_installs_one_gateway_from_high_level_dependencies(self) -> None:
        tree = ast.parse(_ROOT_PATH.read_text(encoding="utf-8"))
        root = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "FocusRuntime"
        )
        initializer = next(
            node
            for node in root.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        calls = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "compose_web_gateway"
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords},
            {
                "config": "self._web_config",
                "data_dir": "self._data_dir",
                "projection": "self._web_projection",
                "web_runtime": "self._web_runtime",
                "backend_reset": "lambda: self._web_backend_reset",
                "ingress": "lambda: self._ingress",
                "runtime_call": "self._runtime_call",
                "operator_status": "self._operational_status_snapshot",
            },
        )
        root_names = {
            node.id for node in ast.walk(initializer) if isinstance(node, ast.Name)
        }
        self.assertNotIn("WebGatewayPorts", root_names)
        self.assertNotIn("WebGateway", root_names)
        assert initializer.end_lineno is not None
        self.assertLessEqual(
            initializer.end_lineno - initializer.lineno + 1,
            1_501,
        )

    def test_composition_module_has_no_root_back_reference_or_state_owner(self) -> None:
        tree = ast.parse(_COMPOSITION_PATH.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "compose_web_gateway"
        )

        self.assertNotIn("bot.focus_runtime.runtime", imports)
        self.assertFalse(any(isinstance(node, ast.ClassDef) for node in tree.body))
        self.assertEqual(
            {argument.arg for argument in function.args.kwonlyargs},
            {
                "config",
                "data_dir",
                "projection",
                "web_runtime",
                "backend_reset",
                "ingress",
                "runtime_call",
                "operator_status",
            },
        )


if __name__ == "__main__":
    unittest.main()
