import json
import os
import pathlib
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _register_handler as _reg,
)
from tests.focus_runtime.codex_handler_fakes import _runtime_state
from bot.cards import build_execution_card, build_terminal_result_card
from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)
from bot.feishu_bot import InteractiveMessageReadResult
from bot.focus_web_wire_catalog import FOCUS_WEB_RECORD_BY_NAME
from bot.thread_lifecycle_service import ThreadLifecyclePolicyError
from bot.thread_runtime_coordination import (
    MANAGED_LOADED_INVENTORY_RPC_TIMEOUT_SECONDS,
)
from bot.card_text_projection import terminal_result_checksum
from bot.runtime_loop import RuntimeLoopContextError
from bot.service_control_plane import (
    ServiceControlShutdownError,
    control_request,
)
from bot.service_runtime_lifecycle import ServiceRuntimeShutdownError
from bot.stores.service_instance_lease import ServiceInstanceLease, ServiceInstanceLeaseError
from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder,
    make_web_interaction_holder,
)
from bot.stores.terminal_result_store import TerminalResultRecord
from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
    _admit_adapter_connection,
)


class CodexHandlerRuntimeCompositionTests(CodexHandlerHarness):
    def test_web_and_feishu_create_share_the_canonical_thread_runtime_authority(
        self,
    ) -> None:
        handler, _bot = self._make_handler()

        web_create = (
            handler._web_runtime._thread_create._ports.create_and_commit_thread
        )
        self.assertIs(web_create.__self__, handler._thread_runtime_authority)
        self.assertIs(
            web_create.__func__,
            handler._thread_runtime_authority.create_and_commit_thread.__func__,
        )
        self.assertIs(
            handler._feishu_thread_sessions._thread_runtime,
            handler._thread_runtime_authority,
        )
        self.assertIs(handler._thread_runtime_authority._adapter, handler._adapter)

    def test_loaded_inventory_control_plane_returns_while_runtime_loop_is_busy(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        handler._adapter.loaded_thread_ids.update({"thread-2", "thread-1"})
        handler._service_instance_lease.acquire()
        endpoint = handler._service_control_plane.start()
        handler._service_instance_lease.publish_control_endpoint(endpoint)

        loop_entered = threading.Event()
        release_loop = threading.Event()

        def occupy_runtime_loop() -> None:
            loop_entered.set()
            if not release_loop.wait(timeout=2.0):
                raise AssertionError("test did not release occupied RuntimeLoop")

        handler._runtime_loop.submit(occupy_runtime_loop)
        self.assertTrue(loop_entered.wait(timeout=1.0))
        try:
            result = control_request(
                handler._data_dir,
                "thread/loaded/list",
                {},
                timeout_seconds=0.5,
            )
        finally:
            release_loop.set()

        self.assertEqual(
            result,
            {
                "instance_name": "default",
                "loaded_thread_ids": ["thread-1", "thread-2"],
            },
        )

    def test_loaded_inventory_control_read_never_reenters_runtime_loop(self) -> None:
        handler, _bot = self._make_handler()

        with (
            patch.object(
                handler,
                "_runtime_call",
                side_effect=AssertionError("loaded inventory re-entered RuntimeLoop"),
            ) as runtime_call,
            patch.object(
                handler._adapter,
                "list_loaded_thread_ids_for_control",
                return_value=["thread-2", "thread-1"],
            ) as list_loaded_thread_ids,
        ):
            result = handler._handle_service_control_request(
                "thread/loaded/list",
                {},
            )

        self.assertEqual(
            result,
            {
                "instance_name": "default",
                "loaded_thread_ids": ["thread-1", "thread-2"],
            },
        )
        runtime_call.assert_not_called()
        list_loaded_thread_ids.assert_called_once_with(
            timeout=MANAGED_LOADED_INVENTORY_RPC_TIMEOUT_SECONDS,
        )

    def test_web_resume_port_leaves_local_interest_commit_to_controller(self) -> None:
        handler, _bot = self._make_handler()
        self.assertTrue(_admit_adapter_connection(handler, 1))
        handler._adapter.thread_snapshots[("thread-web", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-web",
                cwd="/tmp/project",
                name="Web thread",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            effective_model="gpt-5.5",
        )

        handler._runtime_call(handler._web_runtime.client_connected, "tab-web")
        result = handler._runtime_call(
            handler._web_runtime.read_thread,
            "tab-web",
            "thread-web",
        )

        self.assertTrue(result["thread"]["observed_here"])
        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.resume_thread_calls],
            ["thread-web"],
        )
        interest = handler._web_runtime._runtime_interest.snapshot("thread-web")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.outcome, "confirmed")
        self.assertEqual(interest.desired_client_ids, ("tab-web",))

    def test_handler_rejects_string_false_for_boolean_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "web_enabled"):
            self._make_handler({"web_enabled": "false"})

    def test_handler_rejects_unknown_config_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "approval_polciy"):
            self._make_handler({"approval_polciy": "on-request"})

    def test_web_display_name_defaults_independently_of_instance_and_projects_custom_value(
        self,
    ) -> None:
        default_handler, _bot = self._make_handler(instance_name="explorer")
        custom_handler, _bot = self._make_handler(
            {"web_display_name": "  Workstation A  "},
            instance_name="research",
        )

        default_meta = default_handler._runtime_call(
            default_handler._web_runtime.meta,
            "default-title-tab",
        )
        custom_meta = custom_handler._runtime_call(
            custom_handler._web_runtime.meta,
            "custom-title-tab",
        )

        self.assertEqual(default_meta["web_display_name"], "Focus Web")
        self.assertEqual(custom_meta["web_display_name"], "Workstation A")

    def test_handler_projects_trusted_proxy_config_to_web_gateway(self) -> None:
        proof_sha256 = "0123456789abcdef" * 4
        handler, _bot = self._make_handler(
            {
                "web_enabled": True,
                "web_port": 8443,
                "web_trusted_proxy_origin": "https://focus.example.test",
                "web_trusted_proxy_proof_sha256": proof_sha256,
            },
            instance_name="explorer",
        )

        self.assertEqual(handler._web_config.instance_name, "explorer")
        self.assertEqual(
            handler._web_config.trusted_proxy_origin,
            "https://focus.example.test",
        )
        self.assertEqual(
            handler._web_config.trusted_proxy_proof_sha256,
            proof_sha256,
        )

    def test_web_next_turn_seed_and_feishu_binding_defaults_stay_independent(self) -> None:
        handler, _bot = self._make_handler(
            {
                "model": "gpt-5.5",
                "reasoning_effort": "high",
                "approval_policy": "never",
                "permissions_profile_id": ":workspace",
            }
        )

        web_seed = handler._runtime_call(handler._web_runtime.next_turn_settings)
        self.assertEqual(
            web_seed["next_turn_settings"],
            {
                "generation": 1,
                "model": "gpt-5.5",
                "reasoning_effort": "high",
                "approval_policy": "never",
                "permissions_profile_id": ":workspace",
            },
        )
        self.assertFalse(
            (handler._data_dir / "web_next_turn_settings.json").exists()
        )

        handler._runtime_call(handler._web_runtime.client_connected, "tab-web")
        handler._runtime_call(
            handler._web_runtime.update_next_turn_settings,
            "tab-web",
            {
                "model": "gpt-5.4",
                "reasoning_effort": "medium",
                "approval_policy": "on-request",
                "permissions_profile_id": ":danger-full-access",
            },
        )

        binding = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(binding["model"], "gpt-5.5")
        self.assertEqual(binding["reasoning_effort"], "high")
        self.assertEqual(binding["approval_policy"], "never")
        self.assertEqual(binding["permissions_profile_id"], ":workspace")

    def test_operator_status_bypasses_a_stalled_runtime_worker(self) -> None:
        handler, _bot = self._make_handler()
        entered = threading.Event()
        release = threading.Event()

        def _blocked_task() -> None:
            entered.set()
            release.wait(timeout=1.0)

        handler._runtime_submit(_blocked_task)
        self.assertTrue(entered.wait(timeout=1.0))
        try:
            with patch("bot.focus_runtime.runtime._RUNTIME_SLOW_TASK_SECONDS", 0.0):
                started_at = time.monotonic()
                status = handler._operational_status_snapshot()
                elapsed = time.monotonic() - started_at
        finally:
            release.set()

        self.assertLess(elapsed, 0.2)
        self.assertEqual(status["status"], "degraded")
        self.assertTrue(status["runtime_loop"]["active_task_over_threshold"])
        self.assertGreater(status["observed_at"], 0)
        self.assertEqual(status["poll_after_seconds"], 15.0)
        self.assertLessEqual(
            set(FOCUS_WEB_RECORD_BY_NAME["operator_status"].required_fields),
            set(status),
        )

    def test_operator_status_reports_sticky_adapter_ingress_cleanup_failure(self) -> None:
        handler, _bot = self._make_handler()
        gate = handler._adapter_ingress_gate
        self.assertTrue(_admit_adapter_connection(handler, 1))
        gate._invalidate_previous_epoch = lambda: (_ for _ in ()).throw(
            RuntimeError("cleanup failed")
        )

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            gate.observe_disconnect(1)

        status = handler._operational_status_snapshot()

        self.assertEqual(status["status"], "degraded")
        self.assertTrue(status["adapter_ingress"]["backend_reset_blocked"])
        self.assertTrue(status["adapter_ingress"]["cleanup_required"])
        self.assertEqual(
            status["adapter_ingress"]["recovery_action"],
            "retry_backend_reset_or_restart_service",
        )

    def test_production_notification_pipeline_maps_each_label_to_its_handler(self) -> None:
        calls: list[str] = []

        def record_stage(stage: str):
            def record(_owner, _method: str, _params: dict) -> None:
                calls.append(stage)

            return record

        handler, _ = self._make_handler()
        with (
                patch.object(
                    handler._adapter_events,
                    "reconcile_active_turn_lease_notification",
                    side_effect=lambda _method, _params: calls.append(
                        "active_turn_owner"
                    ),
                ),
                patch.object(
                    handler._thread_runtime_authority,
                    "observe_notification",
                    side_effect=lambda _method, _params: calls.append(
                        "effective_settings_facts"
                    ),
                ),
                patch.object(
                    handler._web_runtime,
                    "handle_notification",
                    side_effect=lambda _method, _params: calls.append("web_runtime"),
                ),
                patch.object(
                    handler._operation_owner,
                    "notification",
                    side_effect=lambda _method, _params: calls.append("operation_owner"),
                ),
                patch.object(
                    handler._adapter_notifications,
                    "handle_notification",
                    side_effect=lambda _method, _params: calls.append("feishu_projection"),
                ),
                patch.object(
                    handler._adapter_events,
                    "handle_server_request_notification",
                    side_effect=lambda _method, _params: calls.append(
                        "server_requests"
                    ),
                ),
                patch.object(
                    handler._adapter_events,
                    "handle_feishu_root_operation_notification",
                    side_effect=lambda _method, _params: calls.append(
                        "feishu_root_operation"
                    ),
                ),
        ):
            self._dispatch_adapter_notification(
                handler,
                "turn/started",
                {"threadId": "thread-1"},
            )

        self.assertEqual(
            calls,
            [
                "effective_settings_facts",
                "active_turn_owner",
                "server_requests",
                "web_runtime",
                "operation_owner",
                "feishu_root_operation",
                "feishu_projection",
            ],
        )

    def test_web_notification_workers_use_service_ingress_shutdown_barrier(
        self,
    ) -> None:
        handler, _ = self._make_handler()
        receipt = SimpleNamespace(thread_id="thread-123456789")

        with patch.object(
            handler._ingress,
            "start_background_external_transaction",
        ) as start_background:
            handler._schedule_web_notification_projection(receipt)

            start_background.assert_called_once_with(
                handler._web_runtime.run_notification_projection_transaction,
                receipt,
                thread_name="focus-web-notification-projection-thread-12345",
            )
            start_background.reset_mock()

            handler._schedule_web_attachment_cleanup(
                "thread:thread-123456789"
            )

            start_background.assert_called_once_with(
                handler._web_runtime.run_notification_attachment_cleanup,
                "thread:thread-123456789",
                thread_name="focus-web-attachment-cleanup-thread-12345",
            )

    def test_runtime_owned_aggregates_use_handler_context_guard(self) -> None:
        handler, _ = self._make_handler()
        self.assertEqual(
            handler._operation_owner._runtime_context_guard,
            handler._runtime_loop.assert_worker_context,
        )
        self.assertEqual(
            handler._feishu_root_operations._runtime_context_guard,
            handler._runtime_loop.assert_worker_context,
        )
        with self.assertRaises(RuntimeLoopContextError):
            handler._operation_owner.backend_disconnected()
        with self.assertRaises(RuntimeLoopContextError):
            handler._feishu_root_operations.snapshot("thread-1")

    def test_model_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/model gpt-5.5")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["model"], "gpt-5.5")
        self.assertIn("已切换当前会话的 model override：`gpt-5.5`", bot.replies[-1][1])
        self.assertIn("共享 Codex thread", bot.replies[-1][1])

    def test_model_command_auto_clears_override(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["model"] = "gpt-5.5"

        handler.handle_message("ou_user", "c1", "/model auto")

        self.assertEqual(state["model"], "")
        self.assertIn("已切换当前会话的 model override：`auto`", bot.replies[-1][1])

    def test_effort_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/effort high")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertIn("已切换当前会话的 effort override：`high`", bot.replies[-1][1])
        self.assertIn("共享 Codex thread", bot.replies[-1][1])

    def test_effort_command_auto_clears_override(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["reasoning_effort"] = "medium"

        handler.handle_message("ou_user", "c1", "/effort auto")

        self.assertEqual(state["reasoning_effort"], "")
        self.assertIn("已切换当前会话的 effort override：`auto`", bot.replies[-1][1])

    def test_on_register_eagerly_starts_adapter(self) -> None:
        handler, bot = self._make_handler()

        _reg(handler, bot)

        self.assertIs(handler._feishu_platform.bot, bot)
        self.assertEqual(handler._adapter.start_calls, 1)

    def test_on_register_leaves_web_disabled_for_existing_configs(self) -> None:
        handler, bot = self._make_handler()

        _reg(handler, bot)

        self.assertEqual(handler._web_gateway.endpoint, "")
        self.assertIsNone(WebGatewayRuntimeStore(handler._data_dir).load())

    def test_web_gateway_ports_forward_clear_goal_intent_generation_to_runtime(self) -> None:
        handler, _ = self._make_handler()
        calls: list[tuple[str, str, int]] = []

        def clear_goal(client_id: str, thread_id: str, *, intent_generation: int) -> dict:
            calls.append((client_id, thread_id, intent_generation))
            return {"cleared": True}

        handler._web_runtime.clear_goal = clear_goal

        result = handler._web_gateway._ports.clear_goal(
            "tab-1",
            "thread-1",
            intent_generation=7,
        )

        self.assertEqual(result, {"cleared": True})
        self.assertEqual(calls, [("tab-1", "thread-1", 7)])

    def test_web_gateway_ports_forward_exact_thread_inspection_locators(self) -> None:
        handler, _ = self._make_handler()
        calls: list[tuple[str, tuple, dict]] = []

        def prepare_tool_detail(*args, **kwargs):
            handler._runtime_loop.assert_worker_context()
            calls.append(("detail", args, kwargs))
            return {"kind": "commandExecution"}

        def prepare_conversation_search(*args, **kwargs):
            handler._runtime_loop.assert_worker_context()
            calls.append(("search", args, kwargs))
            return {"query": kwargs["query"]}

        handler._web_runtime.prepare_tool_detail = prepare_tool_detail
        handler._web_runtime.prepare_conversation_search = (
            prepare_conversation_search
        )
        handler._web_runtime.run_prepared_thread_read = lambda prepared: prepared

        prepared_detail = handler._web_gateway._ports.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "item-1",
            view="preview",
            change_index=2,
        )
        self.assertEqual(
            handler._web_gateway._ports.run_prepared_thread_read(
                prepared_detail
            ),
            {"kind": "commandExecution"},
        )
        prepared_search = handler._web_gateway._ports.prepare_conversation_search(
            "tab-1",
            "thread-1",
            query="needle",
            cursor="cursor-1",
        )
        self.assertEqual(
            handler._web_gateway._ports.run_prepared_thread_read(
                prepared_search
            ),
            {"query": "needle"},
        )
        self.assertEqual(
            calls,
            [
                (
                    "detail",
                    ("tab-1", "thread-1", "turn-1", "item-1"),
                    {"view": "preview", "change_index": 2},
                ),
                (
                    "search",
                    ("tab-1", "thread-1"),
                    {"query": "needle", "cursor": "cursor-1"},
                ),
            ],
        )

    def test_web_backend_reset_ports_enter_runtime_once_with_exact_parameters(self) -> None:
        handler, _ = self._make_handler()
        calls: list[tuple[str, object]] = []

        def preview() -> dict:
            handler._runtime_loop.assert_worker_context()
            calls.append(("preview", None))
            return {"status": "available"}

        def execute(*, force: bool, expected_connection_generation: int) -> dict:
            handler._runtime_loop.assert_worker_context()
            calls.append(("execute", (force, expected_connection_generation)))
            return {"force": force}

        handler._web_backend_reset.preview = preview
        handler._web_backend_reset.execute = execute

        self.assertEqual(
            handler._web_gateway._ports.backend_reset_preview(),
            {"status": "available"},
        )
        self.assertEqual(
            handler._web_gateway._ports.backend_reset_execute(
                force=True,
                expected_connection_generation=7,
            ),
            {"force": True},
        )
        self.assertEqual(
            calls,
            [("preview", None), ("execute", (True, 7))],
        )

    def test_web_goal_mutation_uses_its_already_admitted_writer_path(self) -> None:
        """An active goal mutation keeps the submitting Web document's lease."""

        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(summary=thread)
        handler._runtime_call(handler._web_runtime.client_connected, "web-document")

        result = handler._runtime_call(
            handler._web_runtime.set_goal,
            "web-document",
            "thread-1",
            objective="ship the contract",
            intent_generation=1,
        )

        self.assertEqual(result["goal"]["objective"], "ship the contract")
        self.assertEqual(
            handler._adapter.set_thread_goal_calls,
            [
                {
                    "thread_id": "thread-1",
                    "objective": "ship the contract",
                    "status": None,
                    "token_budget": None,
                }
            ],
        )
        # An objective-only goal defaults to active upstream. Its ACK can
        # precede the autonomous turn it starts, so the submission lease stays
        # process-local until an exact turn identity or a known rejection is
        # observed.
        lease = handler._interaction_lease_store.load("thread-1")
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertTrue(lease.holder.same_holder(make_web_interaction_holder("web-document", owner_pid=0)))
        self.assertEqual(lease.turn_id, "")

    def test_web_lifecycle_uses_its_exact_admitted_writer(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(summary=thread)
        handler._runtime_call(handler._web_runtime.client_connected, "web-document")

        result = handler._runtime_call(
            handler._web_runtime.archive_thread,
            "web-document",
            "thread-1",
        )

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(handler._adapter.archive_thread_calls, ["thread-1"])
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))

    def test_focusctl_lifecycle_cannot_override_active_web_or_fcodex_turn(self) -> None:
        for owner_kind, holder_factory in (
            (
                "web",
                lambda: make_web_interaction_holder(
                    "web-document", owner_pid=os.getpid()
                ),
            ),
            (
                "fcodex",
                lambda: make_fcodex_interaction_holder(
                    "fcodex:writer",
                    connection_id="connection-1",
                    owner_pid=os.getpid(),
                ),
            ),
        ):
            with self.subTest(owner_kind=owner_kind):
                tempdir = tempfile.TemporaryDirectory()
                self.addCleanup(tempdir.cleanup)
                data_dir = pathlib.Path(tempdir.name)
                handler, _ = self._make_handler(data_dir=data_dir)
                thread = ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="appServer",
                    status="idle",
                )
                handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(summary=thread)
                holder = holder_factory()
                self._activate_main_turn_lease(handler, "thread-1", holder)

                with self.assertRaisesRegex(ThreadLifecyclePolicyError, "另一终端"):
                    handler._handle_service_control_request(
                        "thread/archive",
                        {"thread_id": "thread-1"},
                    )

                self.assertEqual(handler._adapter.archive_thread_calls, [])

    def test_on_register_starts_and_shutdown_clears_enabled_web_gateway(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        static_dir = data_dir / "web-static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("Focus Web", encoding="utf-8")
        handler, bot = self._make_handler(
            {
                "web_enabled": True,
                "web_static_dir": str(static_dir),
                "web_disconnect_grace_seconds": 0,
            },
            data_dir=data_dir,
        )

        _reg(handler, bot)

        self.assertTrue(handler._web_gateway.endpoint.startswith("http://127.0.0.1:"))
        status = control_request(data_dir, "service/status")
        self.assertTrue(status["web_gateway_enabled"])
        self.assertEqual(status["web_gateway_url"], handler._web_gateway.endpoint)
        self.assertIsNotNone(WebGatewayRuntimeStore(data_dir).load())
        shutdown_order: list[str] = []
        prepare_shutdown = handler._web_runtime.prepare_shutdown
        gateway_stop = handler._web_gateway.stop
        adapter_stop = handler._adapter.stop
        finish_shutdown = handler._web_runtime.finish_shutdown

        def _prepare_shutdown() -> None:
            shutdown_order.append("prepare")
            prepare_shutdown()

        def _gateway_stop() -> None:
            shutdown_order.append("gateway")
            gateway_stop()

        def _adapter_stop() -> None:
            shutdown_order.append("adapter")
            adapter_stop()

        def _finish_shutdown() -> None:
            shutdown_order.append("finish")
            finish_shutdown()

        handler._web_runtime.prepare_shutdown = _prepare_shutdown
        handler._web_gateway.stop = _gateway_stop
        handler._adapter.stop = _adapter_stop
        handler._web_runtime.finish_shutdown = _finish_shutdown
        handler.shutdown()
        self.assertIsNone(WebGatewayRuntimeStore(data_dir).load())
        self.assertEqual(shutdown_order, ["prepare", "gateway", "finish", "adapter"])

    def test_on_register_rolls_back_when_web_gateway_start_fails(self) -> None:
        handler, bot = self._make_handler()

        def _start_web() -> str:
            raise RuntimeError("web start failed")

        handler._web_gateway.start = _start_web
        with self.assertRaisesRegex(RuntimeError, "web start failed"):
            _reg(handler, bot)

        self.assertTrue(handler._runtime_loop._closed)
        self.assertEqual(handler._service_control_plane.control_endpoint, "")
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_startup_rollback_preserves_authority_when_worker_cleanup_is_unproven(self) -> None:
        handler, bot = self._make_handler()

        def _start_web() -> str:
            raise RuntimeError("web start failed")

        handler._web_gateway.start = _start_web
        with patch.object(
            handler._execution_recovery,
            "shutdown",
            side_effect=RuntimeError("worker barrier failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "web start failed"):
                _reg(handler, bot)

        self.assertFalse(handler._runtime_loop._closed)
        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertIsNotNone(handler._service_instance_lease.load_metadata())

        handler.shutdown()

        self.assertTrue(handler._runtime_loop._closed)
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_startup_rollback_cleans_runtime_holders_only_after_loop_barrier(self) -> None:
        handler, bot = self._make_handler()
        events: list[str] = []
        runtime_stop = handler._runtime_loop.stop
        release_holders = (
            handler._thread_runtime_lease_store.release_holders_for_service_generation
        )

        def _stop_runtime_loop(*, timeout=None) -> None:
            events.append("barrier-enter")
            runtime_stop(timeout=timeout)
            events.append("barrier-return")

        def _release_holders(**kwargs):
            events.append("holder-cleanup")
            return release_holders(**kwargs)

        def _start_web() -> str:
            handler._thread_runtime_lease_store.acquire(
                "thread-1",
                handler._service_runtime_authority.service_thread_runtime_holder(),
            )
            raise RuntimeError("web start failed")

        handler._runtime_loop.stop = _stop_runtime_loop
        handler._thread_runtime_lease_store.release_holders_for_service_generation = (
            _release_holders
        )
        handler._web_gateway.start = _start_web

        with self.assertRaisesRegex(RuntimeError, "web start failed"):
            _reg(handler, bot)

        self.assertEqual(
            events,
            ["barrier-enter", "barrier-return", "holder-cleanup"],
        )
        self.assertIsNone(handler._thread_runtime_lease_store.load("thread-1"))
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_startup_rollback_barrier_failure_preserves_machine_authority(self) -> None:
        handler, bot = self._make_handler()

        def _start_web() -> str:
            handler._thread_runtime_lease_store.acquire(
                "thread-1",
                handler._service_runtime_authority.service_thread_runtime_holder(),
            )
            raise RuntimeError("web start failed")

        handler._web_gateway.start = _start_web
        with (
            patch.object(
                handler._runtime_loop,
                "stop",
                side_effect=RuntimeError("barrier failed"),
            ),
            patch.object(
                handler._service_runtime_authority,
                "unregister_instance_runtime",
            ) as unregister,
            patch.object(
                handler._thread_runtime_lease_store,
                "release_holders_for_service_generation",
            ) as release_holders,
            patch.object(handler._service_instance_lease, "release") as release_service,
        ):
            with self.assertRaisesRegex(RuntimeError, "web start failed"):
                _reg(handler, bot)

        unregister.assert_not_called()
        release_holders.assert_not_called()
        release_service.assert_not_called()
        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        lease = handler._thread_runtime_lease_store.load("thread-1")
        assert lease is not None
        self.assertEqual(
            {item.holder_id for item in lease.holders},
            {f"service:{handler._service_instance_lease.owner_token}"},
        )

    def test_shutdown_releases_machine_authority_after_runtime_barrier(self) -> None:
        handler, bot = self._make_handler()
        _reg(handler, bot)
        handler._service_runtime_authority.ensure_service_thread_runtime_lease(
            "thread-1"
        )
        events: list[str] = []
        runtime_stop = handler._runtime_loop.stop
        unregister = handler._service_runtime_authority.unregister_instance_runtime
        release_holders = (
            handler._thread_runtime_lease_store.release_holders_for_service_generation
        )
        release_service = handler._service_instance_lease.release

        def _stop_runtime_loop(*, timeout=None) -> None:
            events.append("barrier-enter")
            runtime_stop(timeout=timeout)
            events.append("barrier-return")

        def _unregister() -> None:
            events.append("unregister")
            unregister()

        def _release_holders(**kwargs):
            events.append("holder-cleanup")
            return release_holders(**kwargs)

        def _release_service() -> None:
            events.append("service-release")
            release_service()

        handler._runtime_loop.stop = _stop_runtime_loop
        handler._service_runtime_authority.unregister_instance_runtime = _unregister
        handler._thread_runtime_lease_store.release_holders_for_service_generation = (
            _release_holders
        )
        handler._service_instance_lease.release = _release_service

        handler.shutdown()

        self.assertEqual(
            events,
            [
                "barrier-enter",
                "barrier-return",
                "unregister",
                "holder-cleanup",
                "service-release",
            ],
        )
        self.assertIsNone(handler._thread_runtime_lease_store.load("thread-1"))
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_shutdown_drains_admitted_backend_reset_before_final_adapter_stop(self) -> None:
        handler, bot = self._make_handler()
        _reg(handler, bot)
        reset_stopped = threading.Event()
        allow_reset_restart = threading.Event()
        reset_done = threading.Event()
        events: list[str] = []
        reset_errors: list[BaseException] = []
        shutdown_errors: list[BaseException] = []
        original_stop = handler._adapter.stop
        original_start = handler._adapter.start

        def stop_adapter() -> None:
            if threading.current_thread() is handler._runtime_loop._worker:
                events.append("reset-stop")
                original_stop()
                reset_stopped.set()
                if not allow_reset_restart.wait(timeout=2.0):
                    raise AssertionError("test did not release paused backend reset")
                return
            events.append("shutdown-stop")
            original_stop()

        def start_adapter() -> None:
            events.append("reset-start")
            handler._adapter.connection_generation_value += 1
            original_start()

        def reset_backend() -> None:
            try:
                self._reset_backend(handler, force=False)
            except BaseException as exc:  # pragma: no cover - asserted below
                reset_errors.append(exc)
            finally:
                reset_done.set()

        def shutdown_handler() -> None:
            try:
                handler.shutdown()
            except BaseException as exc:  # pragma: no cover - asserted below
                shutdown_errors.append(exc)

        handler._adapter.stop = stop_adapter
        handler._adapter.start = start_adapter
        handler._runtime_submit(reset_backend)
        self.assertTrue(reset_stopped.wait(timeout=1.0))

        shutdown_thread = threading.Thread(target=shutdown_handler)
        shutdown_thread.start()
        time.sleep(0.05)

        self.assertTrue(shutdown_thread.is_alive())
        self.assertEqual(events, ["reset-stop"])
        self.assertTrue(handler._service_instance_lease.owns_current_lease())

        allow_reset_restart.set()
        self.assertTrue(reset_done.wait(timeout=1.0))
        shutdown_thread.join(timeout=2.0)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertEqual(reset_errors, [])
        self.assertEqual(shutdown_errors, [])
        self.assertEqual(events, ["reset-stop", "reset-start", "shutdown-stop"])
        self.assertTrue(handler._runtime_loop._closed)
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_shutdown_barrier_failure_preserves_all_machine_authority(self) -> None:
        handler, bot = self._make_handler()
        _reg(handler, bot)
        handler._service_runtime_authority.ensure_service_thread_runtime_lease(
            "thread-1"
        )

        with (
            patch.object(
                handler._runtime_loop,
                "stop",
                side_effect=RuntimeError("barrier failed"),
            ),
            patch.object(
                handler._service_runtime_authority,
                "unregister_instance_runtime",
            ) as unregister,
            patch.object(
                handler._thread_runtime_lease_store,
                "release_holders_for_service_generation",
            ) as release_holders,
            patch.object(handler._service_instance_lease, "release") as release_service,
        ):
            with self.assertRaisesRegex(ServiceRuntimeShutdownError, "barrier failed"):
                handler.shutdown()

        unregister.assert_not_called()
        release_holders.assert_not_called()
        release_service.assert_not_called()
        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertIsNotNone(handler._thread_runtime_lease_store.load("thread-1"))

    def test_shutdown_retries_control_plane_barrier_before_releasing_authority(self) -> None:
        handler, bot = self._make_handler()
        _reg(handler, bot)
        handler._service_runtime_authority.ensure_service_thread_runtime_lease(
            "thread-1"
        )
        control_plane_stop = handler._service_control_plane.stop
        stop_attempts = 0

        def _stop_control_plane(*, timeout: float | None = 5.0) -> None:
            nonlocal stop_attempts
            stop_attempts += 1
            if stop_attempts == 1:
                raise ServiceControlShutdownError("request thread still active")
            control_plane_stop(timeout=timeout)

        handler._service_control_plane.stop = _stop_control_plane

        with self.assertLogs(
            "bot.service_runtime_lifecycle", level="ERROR"
        ) as shutdown_logs:
            with self.assertRaisesRegex(
                ServiceRuntimeShutdownError,
                "request thread still active",
            ):
                handler.shutdown()

        self.assertTrue(
            any("retaining machine authority" in message for message in shutdown_logs.output)
        )
        self.assertEqual(stop_attempts, 1)
        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertIsNotNone(handler._service_instance_lease.load_metadata())
        self.assertIsNotNone(handler._thread_runtime_lease_store.load("thread-1"))
        self.assertTrue(handler._service_control_plane.control_endpoint)

        handler.shutdown()

        self.assertEqual(stop_attempts, 2)
        self.assertEqual(handler._service_control_plane.control_endpoint, "")
        self.assertIsNone(handler._thread_runtime_lease_store.load("thread-1"))
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_shutdown_retries_instance_registry_release_before_service_lease(self) -> None:
        handler, bot = self._make_handler()
        _reg(handler, bot)
        unregister = handler._service_runtime_authority.unregister_instance_runtime
        attempts = 0

        def _flaky_unregister() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("instance registry unavailable")
            unregister()

        handler._service_runtime_authority.unregister_instance_runtime = (
            _flaky_unregister
        )

        with self.assertLogs("bot.focus_runtime", level="ERROR"):
            with self.assertRaisesRegex(
                ServiceRuntimeShutdownError,
                "instance registry unavailable",
            ):
                handler.shutdown()

        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertIsNotNone(handler._service_instance_lease.load_metadata())

        handler.shutdown()

        self.assertEqual(attempts, 2)
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_shutdown_retries_runtime_holder_release_before_service_lease(self) -> None:
        handler, bot = self._make_handler()
        _reg(handler, bot)
        handler._service_runtime_authority.ensure_service_thread_runtime_lease(
            "thread-1"
        )
        release_holders = (
            handler._thread_runtime_lease_store.release_holders_for_service_generation
        )
        attempts = 0

        def _flaky_release_holders(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("runtime holder store unavailable")
            return release_holders(**kwargs)

        handler._thread_runtime_lease_store.release_holders_for_service_generation = (
            _flaky_release_holders
        )

        with self.assertLogs("bot.focus_runtime", level="ERROR"):
            with self.assertRaisesRegex(
                ServiceRuntimeShutdownError,
                "runtime holder store unavailable",
            ):
                handler.shutdown()

        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertIsNotNone(handler._thread_runtime_lease_store.load("thread-1"))

        handler.shutdown()

        self.assertEqual(attempts, 2)
        self.assertIsNone(handler._thread_runtime_lease_store.load("thread-1"))
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_feishu_runtime_release_is_deferred_while_web_has_runtime_interest(self) -> None:
        handler, _bot = self._make_handler()
        handler._binding_runtime_coordinator._runtime_interest_retained = (
            lambda thread_id: thread_id == "thread-1"
        )

        with patch.object(
            handler._service_runtime_authority,
            "release_service_thread_runtime_lease",
        ) as mock_release:
            handler._binding_runtime_coordinator.unsubscribe_thread_unless_web_runtime_requires_interest("thread-1")
            handler._binding_runtime_coordinator.release_service_thread_runtime_lease_unless_web_runtime_requires_interest("thread-1")

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        mock_release.assert_not_called()

    def test_on_register_fails_fast_when_service_instance_is_already_owned(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        lease = ServiceInstanceLease(data_dir)
        lease.acquire(control_endpoint="tcp://127.0.0.1:32001")
        self.addCleanup(lease.release)

        with self.assertRaises(ServiceInstanceLeaseError):
            _reg(handler, bot)

        self.assertEqual(handler._adapter.start_calls, 0)

    def test_last_text_skips_legacy_terminal_card_and_falls_back_to_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("最新终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
            SimpleNamespace(
                message_id="msg-execution",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_execution_card("旧执行输出", [], running=False),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertIn("旧执行输出", bot.replies[-1][1])
        self.assertNotIn("最新终态", bot.replies[-1][1])

    def test_last_text_prefers_local_authoritative_terminal_text_when_protocol_is_lost(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_results._store.upsert(
            TerminalResultRecord(
                message_id="msg-terminal",
                execution_message_id="",
                final_reply_text="本地权威终态\n> 引用正文",
                recorded_at=1.0,
            )
        )
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        {
                            "title": "Codex",
                            "elements": [[{"tag": "text", "text": "飞书投影已丢协议 marker"}]],
                        },
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
            SimpleNamespace(
                message_id="msg-older",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("较早终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertEqual(bot.replies[-1][1], "本地权威终态\n> 引用正文")

    def test_last_text_falls_back_to_latest_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-execution",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_execution_card("最近执行输出", [], running=False),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
            SimpleNamespace(
                message_id="msg-other-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id="other_app"),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("别的机器人终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertIn("最近执行输出", bot.replies[-1][1])

    def test_last_text_skips_degraded_terminal_result_card_when_store_misses(self) -> None:
        handler, bot = self._make_handler()
        checksum = terminal_result_checksum("权威原文")
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card(
                            "降级投影正文",
                            terminal_result_id="0123456789abcdef0123456789abcdef",
                            checksum=checksum,
                        ),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
            SimpleNamespace(
                message_id="msg-execution",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_execution_card("最近执行输出", [], running=False),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertIn("最近执行输出", bot.replies[-1][1])
        self.assertNotIn("降级投影正文", bot.replies[-1][1])

    def test_last_text_prefers_latest_authoritative_text_message(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_results._store.upsert(
            TerminalResultRecord(
                message_id="msg-latest-text",
                execution_message_id="exec-1",
                final_reply_text="最新纯文本终态",
                recorded_at=2.0,
            )
        )
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-latest-text",
                msg_type="text",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(content=json.dumps({"text": "最新纯文本终态"}, ensure_ascii=False)),
                thread_id="",
            ),
            SimpleNamespace(
                message_id="msg-execution",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_execution_card("旧执行输出", [], running=False),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertEqual(bot.replies[-1][1], "最新纯文本终态")

    def test_last_text_does_not_export_legacy_terminal_projection_when_raw_card_fetch_fails(self) -> None:
        handler, bot = self._make_handler()
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("最近终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertEqual(bot.replies[-1][1], "最近没有找到可导出的终态卡；也没有可回退的执行卡。")

    def test_last_text_prefers_raw_terminal_when_history_projection_loses_marker(self) -> None:
        handler, bot = self._make_handler()
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-latest",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        {
                            "title": "Codex",
                            "elements": [[{"tag": "text", "text": "投影里 marker 丢了"}]],
                        },
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
            SimpleNamespace(
                message_id="msg-older",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("较早终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]
        bot.raw_card_results["msg-latest"] = InteractiveMessageReadResult(
            text="最新终态",
            card_kind="terminal",
            has_authoritative_text=True,
        )

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertEqual(bot.replies[-1][1], "最新终态")

    def test_last_text_uses_current_thread_scope(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["msg-thread"] = {"chat_type": "group", "thread_id": "th-1"}
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-thread-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("线程内终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="th-1",
            ),
            SimpleNamespace(
                message_id="msg-main-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_terminal_result_card("主会话终态"),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]
        bot.raw_card_results["msg-thread-terminal"] = InteractiveMessageReadResult(
            text="线程内终态",
            card_kind="terminal",
            has_authoritative_text=True,
        )
        bot.raw_card_results["msg-main-terminal"] = InteractiveMessageReadResult(
            text="主会话终态",
            card_kind="terminal",
            has_authoritative_text=True,
        )

        handler.handle_message("ou_admin", "c1", "/last text", message_id="msg-thread")

        self.assertEqual(bot.replies[-1][1], "线程内终态")

    def test_last_text_does_not_use_codex_thread_id_as_feishu_thread_container(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["msg-main"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
        state = _runtime_state(handler, "ou_admin", "c1", "msg-main")
        state["current_thread_id"] = "codex-thread-1"
        handler._terminal_results._store.upsert(
            TerminalResultRecord(
                message_id="msg-terminal",
                execution_message_id="",
                final_reply_text="本地终态",
                recorded_at=1.0,
                thread_id="codex-thread-1",
            )
        )

        handler.handle_message("ou_admin", "c1", "/last text", message_id="msg-main")

        self.assertEqual(bot.list_recent_messages_calls[-1]["thread_id"], "")
        self.assertEqual(bot.replies[-1][1], "本地终态")

    def test_last_text_uses_feishu_thread_for_history_and_codex_thread_for_local_fallback(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["msg-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
            "thread_id": "feishu-thread-1",
        }
        state = _runtime_state(handler, "ou_admin", "c1", "msg-thread")
        state["current_thread_id"] = "codex-thread-1"
        handler._terminal_results._store.upsert(
            TerminalResultRecord(
                message_id="msg-terminal",
                execution_message_id="",
                final_reply_text="本地线程终态",
                recorded_at=1.0,
                thread_id="codex-thread-1",
            )
        )

        handler.handle_message("ou_admin", "c1", "/last text", message_id="msg-thread")

        self.assertEqual(bot.list_recent_messages_calls[-1]["thread_id"], "feishu-thread-1")
        self.assertEqual(bot.replies[-1][1], "本地线程终态")

    def test_last_text_does_not_export_history_rendered_legacy_terminal_card_shape(self) -> None:
        handler, bot = self._make_handler()
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-history-terminal",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        {
                            "title": "Codex",
                            "elements": [
                                [
                                    {"tag": "text", "text": "## 结论"},
                                    {
                                        "tag": "text",
                                        "text": "第一条\n第二条\u2063\u2060\u2064\u2060\u2063",
                                    },
                                ]
                            ],
                        },
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertEqual(bot.replies[-1][1], "最近没有找到可导出的终态卡；也没有可回退的执行卡。")

    def test_last_text_requires_text_subcommand(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/last")

        self.assertEqual(bot.replies[-1][1], "用法：`/last text`")

    def test_last_text_reports_when_no_matching_card_exists(self) -> None:
        handler, bot = self._make_handler()
        bot.history_messages = [
            SimpleNamespace(
                msg_type="text",
                sender=SimpleNamespace(sender_type="user", id="ou_user"),
                body=SimpleNamespace(content=json.dumps({"text": "普通消息"}, ensure_ascii=False)),
                thread_id="",
            )
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertEqual(bot.replies[-1][1], "最近没有找到可导出的终态卡；也没有可回退的执行卡。")

    def test_last_text_ignores_corrupted_terminal_result_store(self) -> None:
        handler, bot = self._make_handler()
        (handler._data_dir / "terminal_results.json").write_text(
            '{"schema_version":"oops","results":[]}',
            encoding="utf-8",
        )
        bot.history_messages = [
            SimpleNamespace(
                message_id="msg-execution",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id=bot.app_id),
                body=SimpleNamespace(
                    content=json.dumps(
                        build_execution_card("最近执行输出", [], running=False),
                        ensure_ascii=False,
                    )
                ),
                thread_id="",
            ),
        ]

        handler.handle_message("ou_user", "c1", "/last text")

        self.assertIn("最近执行输出", bot.replies[-1][1])

    def test_on_register_recovers_from_stale_owner_metadata_and_socket(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        metadata_path = data_dir / "service-instance.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "owner_pid": 999999,
                    "owner_token": "stale-owner-token",
                    "control_endpoint": "tcp://127.0.0.1:32001",
                    "started_at": 1.0,
                }
            ),
            encoding="utf-8",
        )
        handler, bot = self._make_handler(data_dir=data_dir)

        _reg(handler, bot)

        metadata = handler._service_instance_lease.load_metadata()
        status = control_request(data_dir, "service/status")

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.owner_pid, os.getpid())
        self.assertNotEqual(metadata.owner_token, "stale-owner-token")
        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertTrue(metadata.control_endpoint.startswith("tcp://127.0.0.1:"))
        self.assertEqual(status["pid"], os.getpid())

    def test_startup_publishes_endpoint_only_after_adapter_is_ready(self) -> None:
        handler, bot = self._make_handler()
        live_url = handler._adapter.config.app_server_url
        adapter_ready = False
        original_start = handler._adapter.start

        def start_adapter() -> None:
            nonlocal adapter_ready
            original_start()
            adapter_ready = True

        handler._adapter.start = start_adapter
        handler._adapter.current_app_server_url = (
            lambda: live_url if adapter_ready else ""
        )

        self.assertEqual(handler._adapter.current_app_server_url(), "")
        _reg(handler, bot)

        registry_entry = handler._instance_registry.load(handler._instance_name)
        status = control_request(handler._data_dir, "service/status")
        self.assertIsNotNone(registry_entry)
        assert registry_entry is not None
        self.assertEqual(registry_entry.app_server_url, live_url)
        self.assertEqual(status["app_server_url"], live_url)

    def test_on_register_rolls_back_runtime_loop_when_adapter_start_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        stop_calls: list[str] = []

        def _stop_adapter() -> None:
            stop_calls.append("adapter")

        def _start_adapter() -> None:
            raise RuntimeError("adapter start failed")

        handler._adapter.stop = _stop_adapter
        handler._adapter.start = _start_adapter

        with self.assertRaisesRegex(RuntimeError, "adapter start failed"):
            _reg(handler, bot)

        self.assertTrue(handler._runtime_loop._closed)
        self.assertEqual(stop_calls, ["adapter"])
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_on_register_rolls_back_adapter_when_control_plane_start_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        stop_calls: list[str] = []

        def _stop_adapter() -> None:
            stop_calls.append("adapter")

        def _start_control_plane() -> None:
            raise RuntimeError("control plane start failed")

        handler._adapter.stop = _stop_adapter
        handler._service_control_plane.start = _start_control_plane

        with self.assertRaisesRegex(RuntimeError, "control plane start failed"):
            _reg(handler, bot)

        self.assertTrue(handler._runtime_loop._closed)
        self.assertEqual(stop_calls, ["adapter"])
        self.assertEqual(handler._service_control_plane.control_endpoint, "")
        self.assertIsNone(handler._service_instance_lease.load_metadata())
