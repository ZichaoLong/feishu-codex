from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
)
from bot.focus_runtime.runtime import FocusRuntime as CodexHandler
from bot.focus_runtime.feishu_surface import FeishuSurface
from bot.runtime_loop import RuntimeLoop
from bot.service_runtime_lifecycle import (
    ServiceRuntimeActivationPorts,
    ServiceRuntimeIngressDispatcher,
    ServiceRuntimeIngressRejected,
    ServiceRuntimeLifecycle,
    ServiceRuntimeLifecycleReentryError,
    ServiceRuntimePhase,
    ServiceRuntimeShutdownPorts,
)


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _lifecycle(runtime_loop: RuntimeLoop) -> ServiceRuntimeLifecycle:
    return ServiceRuntimeLifecycle(
        activation=ServiceRuntimeActivationPorts(
            acquire_service_lease=_noop,
            prepare_owned_state=_noop,
            start_runtime_loop=runtime_loop.start,
            restore_runtime_state=_noop,
            start_adapter=_noop,
            start_destination_liveness_worker=_noop,
            start_control_plane=lambda: "test://control",
            publish_control_endpoint=_noop,
            register_instance_runtime=_noop,
            restore_runtime_leases=_noop,
            start_web_gateway=_noop,
        ),
        shutdown=ServiceRuntimeShutdownPorts(
            cancel_frontend_timers=_noop,
            web_is_running=lambda: False,
            prepare_web_shutdown=_noop,
            stop_web_gateway=_noop,
            stop_control_plane=_noop,
            stop_server_request_runtime=_noop,
            stop_execution_recovery_worker=_noop,
            stop_destination_liveness_worker=_noop,
            stop_card_dispatcher=_noop,
            finish_web_shutdown=_noop,
            stop_runtime_loop=runtime_loop.stop,
            stop_adapter=_noop,
            release_machine_authority=_noop,
        ),
    )


class CodexHandlerExternalIngressTests(unittest.TestCase):
    def _handler(
        self,
        *,
        active: bool,
    ) -> tuple[CodexHandler, ServiceRuntimeLifecycle, RuntimeLoop, list[str]]:
        handler = object.__new__(CodexHandler)
        runtime_loop = RuntimeLoop(name="external-ingress-test-runtime")
        lifecycle = _lifecycle(runtime_loop)
        handler._runtime_loop = runtime_loop
        handler._service_runtime_lifecycle = lifecycle
        handler._ingress = ServiceRuntimeIngressDispatcher(
            lifecycle,
            runtime_loop.call,
            runtime_loop.submit,
        )
        calls: list[str] = []

        def record(name: str, result: Any = None) -> Callable[..., Any]:
            def invoke(*_args: Any, **_kwargs: Any) -> Any:
                calls.append(name)
                return result

            return invoke

        handler._feishu_surface = SimpleNamespace(
            handle_message_impl=record("message"),
            handle_message_recalled_impl=record("message_recalled"),
            should_bypass_runtime_for_card_action=(
                FeishuSurface.should_bypass_runtime_for_card_action
            ),
            handle_card_action_impl=record("card_action", "card-result"),
            handle_attachment_message_impl=record("attachment"),
            preflight_group_prompt_impl=record(
                "preflight_group_prompt",
                True,
            ),
            should_route_group_followup_prompt_impl=record(
                "should_route_group_followup_prompt",
                False,
            ),
        )
        handler._binding_runtime_coordinator = SimpleNamespace(
            is_sender_active_on_runtime=record("is_sender_active", True),
            deactivate_sender_impl=record("deactivate_sender"),
        )
        handler._destination_liveness = SimpleNamespace(
            accept=record("destination_loss")
        )
        if active:
            lifecycle.start()
        return handler, lifecycle, runtime_loop, calls

    @staticmethod
    def _public_ingress_calls(handler: CodexHandler) -> tuple[tuple[str, Callable[[], Any]], ...]:
        return (
            (
                "message",
                lambda: handler.handle_message("ou-user", "chat", "hello", "message"),
            ),
            (
                "message_recalled",
                lambda: handler.handle_message_recalled("chat", "message"),
            ),
            (
                "card_action",
                lambda: handler.handle_card_action(
                    "ou-user",
                    "chat",
                    "message",
                    {"action": "approve"},
                ),
            ),
            (
                "card_action_direct",
                lambda: handler.handle_card_action(
                    "ou-user",
                    "chat",
                    "message",
                    {"action": "attach_runtime"},
                ),
            ),
            (
                "attachment",
                lambda: handler.handle_attachment_message(
                    "ou-user",
                    "chat",
                    "message",
                    "file",
                    "file-key",
                    "spec.txt",
                ),
            ),
            (
                "is_sender_active",
                lambda: handler.is_sender_active("ou-user", "chat", "message"),
            ),
            (
                "deactivate_sender",
                lambda: handler.deactivate_sender("ou-user", "chat", "message"),
            ),
            (
                "preflight_group_prompt",
                lambda: handler.preflight_group_prompt(
                    "ou-user",
                    "chat",
                    message_id="message",
                ),
            ),
            (
                "should_route_group_followup_prompt",
                lambda: handler.should_route_group_followup_prompt(
                    "ou-user",
                    "chat",
                    message_id="message",
                ),
            ),
            (
                "destination_loss",
                lambda: handler.accept_destination_loss_proof(
                    FeishuDestinationLossProof(
                        source_id="event-1",
                        chat_id="chat",
                        proof_type=(
                            FeishuDestinationLossProofType.CHAT_DISBANDED_EVENT
                        ),
                    )
                ),
            ),
        )

    def test_complete_public_surface_uses_the_active_lifecycle_gate(self) -> None:
        handler, lifecycle, _runtime_loop, calls = self._handler(active=True)

        results = {
            name: invoke()
            for name, invoke in self._public_ingress_calls(handler)
        }
        lifecycle.stop()

        self.assertEqual(
            calls,
            [
                "message",
                "message_recalled",
                "card_action",
                "card_action",
                "attachment",
                "is_sender_active",
                "deactivate_sender",
                "preflight_group_prompt",
                "should_route_group_followup_prompt",
                "destination_loss",
            ],
        )
        self.assertEqual(results["card_action"], "card-result")
        self.assertEqual(results["card_action_direct"], "card-result")
        self.assertIs(results["is_sender_active"], True)
        self.assertIs(results["preflight_group_prompt"], True)
        self.assertIs(results["should_route_group_followup_prompt"], False)

    def test_complete_public_surface_rejects_assembled_ingress(self) -> None:
        handler, lifecycle, runtime_loop, calls = self._handler(active=False)

        for name, invoke in self._public_ingress_calls(handler):
            with self.subTest(name=name):
                with self.assertRaises(ServiceRuntimeIngressRejected) as rejected:
                    invoke()
                self.assertEqual(
                    rejected.exception.phase,
                    ServiceRuntimePhase.ASSEMBLED,
                )

        self.assertEqual(calls, [])
        self.assertIsNone(runtime_loop._worker)
        lifecycle.stop()

    def test_shutdown_waits_for_runtime_callback_and_rejects_new_ingress(self) -> None:
        handler, lifecycle, _runtime_loop, calls = self._handler(active=True)
        callback_entered = threading.Event()
        release_callback = threading.Event()
        message_errors: list[Exception] = []
        stop_errors: list[Exception] = []

        def block_message(*_args: Any, **_kwargs: Any) -> None:
            calls.append("blocking_message")
            callback_entered.set()
            release_callback.wait(timeout=2.0)

        handler._feishu_surface.handle_message_impl = block_message
        message_thread = threading.Thread(
            target=lambda: self._capture(
                lambda: handler.handle_message("ou-user", "chat", "hello", "message"),
                message_errors,
            )
        )
        message_thread.start()
        self.assertTrue(callback_entered.wait(timeout=1.0))

        stop_thread = threading.Thread(
            target=lambda: self._capture(lifecycle.stop, stop_errors)
        )
        stop_thread.start()
        deadline = time.monotonic() + 1.0
        while (
            lifecycle.phase is not ServiceRuntimePhase.STOPPING
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)

        self.assertEqual(lifecycle.phase, ServiceRuntimePhase.STOPPING)
        self.assertTrue(stop_thread.is_alive())
        with self.assertRaises(ServiceRuntimeIngressRejected) as rejected:
            handler.is_sender_active("ou-other", "chat")
        self.assertEqual(rejected.exception.phase, ServiceRuntimePhase.STOPPING)

        release_callback.set()
        message_thread.join(timeout=1.0)
        stop_thread.join(timeout=1.0)
        self.assertFalse(message_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(message_errors, [])
        self.assertEqual(stop_errors, [])
        self.assertEqual(calls, ["blocking_message"])
        self.assertEqual(lifecycle.phase, ServiceRuntimePhase.CLOSED)

    def test_ingress_callback_cannot_reenter_shutdown(self) -> None:
        handler, lifecycle, _runtime_loop, _calls = self._handler(active=True)
        handler._feishu_surface.handle_message_impl = lambda *_args, **_kwargs: handler.shutdown()

        with self.assertRaises(ServiceRuntimeLifecycleReentryError):
            handler.handle_message("ou-user", "chat", "hello", "message")

        handler._feishu_surface.handle_card_action_impl = (
            lambda *_args, **_kwargs: handler.shutdown()
        )
        with self.assertRaises(ServiceRuntimeLifecycleReentryError):
            handler.handle_card_action(
                "ou-user",
                "chat",
                "message",
                {"action": "attach_runtime"},
            )

        self.assertEqual(lifecycle.phase, ServiceRuntimePhase.ACTIVE)
        lifecycle.stop()

    def test_async_ingress_origin_can_stop_after_dispatch(self) -> None:
        handler, lifecycle, _runtime_loop, calls = self._handler(active=True)
        callback_entered = threading.Event()
        release_callback = threading.Event()
        stopping_observed: list[ServiceRuntimePhase] = []

        def block_recall(*_args: Any, **_kwargs: Any) -> None:
            calls.append("blocking_recall")
            callback_entered.set()
            release_callback.wait(timeout=2.0)

        def release_after_stopping() -> None:
            deadline = time.monotonic() + 1.0
            while (
                lifecycle.phase is not ServiceRuntimePhase.STOPPING
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            stopping_observed.append(lifecycle.phase)
            release_callback.set()

        handler._feishu_surface.handle_message_recalled_impl = block_recall
        handler.handle_message_recalled("chat", "message")
        self.assertTrue(callback_entered.wait(timeout=1.0))
        releaser = threading.Thread(target=release_after_stopping)
        releaser.start()

        lifecycle.stop()

        releaser.join(timeout=1.0)
        self.assertFalse(releaser.is_alive())
        self.assertEqual(stopping_observed, [ServiceRuntimePhase.STOPPING])
        self.assertEqual(calls, ["blocking_recall"])
        self.assertEqual(lifecycle.phase, ServiceRuntimePhase.CLOSED)

    @staticmethod
    def _capture(action: Callable[[], Any], errors: list[Exception]) -> None:
        try:
            action()
        except Exception as exc:  # pragma: no cover - asserted by the caller
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
