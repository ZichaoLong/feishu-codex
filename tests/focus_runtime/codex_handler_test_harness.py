import os
import pathlib
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _FakeAdapter,
    _FakeBot,
    _store_canonical_pending_interaction,
)
from tests.focus_runtime.codex_handler_fakes import _store_pending_interaction as _store_pending
from tests.focus_runtime.feishu_owner_test_support import (
    prepare_terminal_feishu_fifo,
)
from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)
from bot.focus_runtime.runtime import FocusRuntime
from bot.codex_protocol.client import (
    CodexRpcError,
)
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.feishu_execution_queue import (
    FeishuBindingExecutionSnapshot,
    FeishuQueuedMessageOrigin,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.service_runtime_lifecycle import ServiceRuntimePhase
from bot.thread_runtime_authority import (
    ThreadResumeOutcomeUnknown,
)

_DISPLAY_INIT_COMMAND = feishu_visible_command_syntax("/init <token>")
_DISPLAY_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")
_DISPLAY_RESUME_COMMAND = feishu_visible_command_syntax("/resume <thread_id|thread_name>")
_DISPLAY_LOCAL_RESUME_COMMAND = feishu_visible_command_syntax("fcodex resume <thread_id|thread_name>")
_DISPLAY_CD_COMMAND = feishu_visible_command_syntax("/cd <path>")
_DISPLAY_RENAME_COMMAND = feishu_visible_command_syntax("/rename <title>")
_DISPLAY_LOCAL_THREAD_UNSUBSCRIBE = feishu_visible_command_syntax(
    "focusctl thread detach --thread-id <thread_id>"
)
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)


def _admit_adapter_connection(handler: FocusRuntime, generation: int) -> bool:
    return bool(handler._runtime_call(handler._adapter_events.accept_connection_ingress, generation))


class CodexHandlerHarness(unittest.TestCase):
    _prepare_terminal_feishu_fifo = prepare_terminal_feishu_fifo

    @staticmethod
    def _unpack_card_response(response) -> dict:
        """Unpack P2CardActionTriggerResponse into a plain dict for assertions."""
        if isinstance(response, dict):
            return response
        result: dict = {}
        if getattr(response, "card", None):
            result["card"] = response.card.data
        if getattr(response, "toast", None):
            result["toast"] = response.toast.content
            result["toast_type"] = response.toast.type
        return result

    @staticmethod
    def _first_action(card: dict) -> dict:
        return next(
            element for element in card["elements"] if isinstance(element, dict) and element.get("tag") == "action"
        )

    @staticmethod
    def _action_elements(card: dict) -> list[dict]:
        return [
            element for element in card["elements"] if isinstance(element, dict) and element.get("tag") == "action"
        ]

    @staticmethod
    def _binding_keys(handler: FocusRuntime) -> tuple[tuple[str, str], ...]:
        with handler._lock:
            return handler._binding_runtime.binding_keys_locked()

    @staticmethod
    def _queue_snapshot(handler: FocusRuntime, binding: tuple[str, str]):
        return handler._runtime_call(
            handler._feishu_execution_queue.snapshot,
            binding,
        )

    @staticmethod
    def _enqueue_feishu_queue_item(
        handler: FocusRuntime,
        *,
        kind: str,
        binding: tuple[str, str],
        root_thread_id: str,
        sender_id: str | None = None,
        chat_id: str | None = None,
        message_id: str = "",
        text: str = "",
        input_items: tuple[dict, ...] = (),
        origin: FeishuQueuedMessageOrigin | None = None,
    ):
        def enqueue():
            with handler._lock:
                snapshot = handler._binding_runtime_coordinator.feishu_binding_execution_snapshot_locked(binding)
                if snapshot is None:
                    snapshot = FeishuBindingExecutionSnapshot(
                        binding=binding,
                        root_thread_id=root_thread_id,
                        active=True,
                        attached=True,
                        has_inflight_execution=True,
                        current_turn_id="turn-1",
                    )
                else:
                    snapshot = replace(
                        snapshot,
                        root_thread_id=root_thread_id,
                        active=True,
                        attached=True,
                        has_inflight_execution=True,
                    )
                if kind == "prompt":
                    return handler._feishu_execution_queue.enqueue_prompt(
                        snapshot,
                        sender_id=sender_id or binding[0],
                        chat_id=chat_id or binding[1],
                        message_id=message_id,
                        text=text,
                        origin=origin,
                        input_items=input_items,
                    )
                return handler._feishu_execution_queue.enqueue_compact(
                    snapshot,
                    sender_id=sender_id or binding[0],
                    chat_id=chat_id or binding[1],
                    message_id=message_id,
                    origin=origin,
                )

        return handler._runtime_call(enqueue)

    @staticmethod
    def _wait_until(predicate, *, timeout: float = 1.0, interval: float = 0.01) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        if predicate():
            return
        raise AssertionError("condition not met within timeout")

    @staticmethod
    def _store_pending_request(handler: FocusRuntime, request_id: int | str, pending: dict) -> None:
        _store_pending(handler, jsonrpc_id_key(request_id), pending)

    @staticmethod
    def _store_canonical_pending_request(
        handler: FocusRuntime,
        pending: dict,
    ) -> str:
        return _store_canonical_pending_interaction(handler, pending)

    @staticmethod
    def _has_pending_request(handler: FocusRuntime, request_id: int | str) -> bool:
        return handler._interaction_requests.has_pending_request(jsonrpc_id_key(request_id))

    @staticmethod
    def _dispatch_adapter_notification(
        handler: FocusRuntime,
        method: str,
        params: dict,
    ) -> None:
        handler._runtime_call(handler._adapter_events.dispatch_notification, method, params)

    @staticmethod
    def _dispatch_adapter_notification_for_connection(
        handler: FocusRuntime,
        connection_generation: int,
        method: str,
        params: dict,
    ) -> None:
        handler._runtime_call(
            handler._adapter_events.handle_notification_for_connection,
            connection_generation,
            method,
            params,
        )

    @staticmethod
    def _feishu_root_snapshot(handler: FocusRuntime, root_thread_id: str):
        return handler._runtime_call(
            handler._feishu_root_operations.snapshot,
            root_thread_id,
        )

    @staticmethod
    def _admit_feishu_root_operation_token(
        handler: FocusRuntime,
        binding: tuple[str, str],
        root_thread_id: str,
        *,
        reason: str,
        message_id: str = "",
    ):
        return handler._runtime_call(
            handler._feishu_root_operations.admit,
            binding,
            root_thread_id,
            chat_id=binding[1],
            message_id=message_id,
            reason=reason,
        )

    def _arm_accepted_feishu_continuation(
        self,
        handler: FocusRuntime,
        binding: tuple[str, str],
        root_thread_id: str,
        *,
        reason: str,
    ) -> int:
        before = self._feishu_root_snapshot(
            handler,
            root_thread_id,
        ).continuation_generations
        admission = self._admit_feishu_root_operation_token(
            handler,
            binding,
            root_thread_id,
            reason=reason,
        )
        handler._runtime_call(
            handler._feishu_root_operations.arm_continuation,
            admission,
            reason=reason,
        )
        handler._runtime_call(
            handler._feishu_root_operations.acknowledge_continuing,
            admission,
        )
        after = self._feishu_root_snapshot(
            handler,
            root_thread_id,
        ).continuation_generations
        created = set(after) - set(before)
        self.assertEqual(len(created), 1)
        return created.pop()

    @staticmethod
    def _pending_rename_form_snapshot(handler: FocusRuntime, message_id: str) -> dict[str, str] | None:
        return handler._threads_ui_domain.pending_rename_form_snapshot(message_id)

    @staticmethod
    def _register_pending_rename_form(handler: FocusRuntime, message_id: str, *, thread_id: str) -> None:
        handler._threads_ui_domain.register_pending_rename_form(message_id, thread_id=thread_id)

    @staticmethod
    def _service_runtime_holder_ids(handler: FocusRuntime, thread_id: str) -> tuple[str, ...]:
        lease = handler._thread_runtime_lease_store.load(thread_id)
        if lease is None:
            return ()
        return tuple(holder.holder_id for holder in lease.holders)

    @staticmethod
    def _seed_authoritative_thread(
        handler: FocusRuntime,
        thread_id: str = "thread-1",
        *,
        status: str = "notLoaded",
    ) -> ThreadSummary:
        thread = ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status=status,
        )
        snapshot = ThreadSnapshot(summary=thread)
        handler._adapter.thread_snapshots[(thread_id, False)] = snapshot
        handler._adapter.thread_snapshots[(thread_id, None)] = snapshot
        return thread

    def _seed_resume_outcome_unknown(
        self,
        handler: FocusRuntime,
        thread_id: str,
    ) -> ThreadResumeOutcomeUnknown:
        original_resume_thread = handler._adapter.resume_thread

        def _unknown_resume(_thread_id: str, **_kwargs):
            raise CodexRpcError(
                "thread/resume",
                {"code": -32603, "message": "response assembly failed"},
            )

        handler._adapter.resume_thread = _unknown_resume
        try:
            with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
                handler._thread_runtime_authority.begin_resume_thread(thread_id)
        finally:
            handler._adapter.resume_thread = original_resume_thread
        return raised.exception

    def _make_handler(
        self,
        cfg: dict | None = None,
        *,
        data_dir: pathlib.Path | None = None,
        instance_name: str = "default",
    ) -> tuple[FocusRuntime, _FakeBot]:
        if data_dir is None:
            tempdir = tempfile.TemporaryDirectory()
            self.addCleanup(tempdir.cleanup)
            data_dir = pathlib.Path(tempdir.name)
        effective_cfg = {"mirror_watchdog_seconds": 999999}
        effective_cfg.update(dict(cfg or {}))
        config_patch = patch(
            "bot.focus_runtime.runtime.load_config_file",
            return_value=effective_cfg,
        )
        adapter_patch = patch(
            "bot.focus_runtime.runtime.CodexAppServerAdapter",
            _FakeAdapter,
        )
        env_patch = patch.dict(
            os.environ,
            {
                "FOCUS_GLOBAL_DATA_DIR": str(data_dir / "_global"),
                "FOCUS_INSTANCE": instance_name,
            },
            clear=False,
        )
        config_patch.start()
        adapter_patch.start()
        env_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(adapter_patch.stop)
        self.addCleanup(env_patch.stop)
        handler = FocusRuntime(data_dir=data_dir)
        handler._service_runtime_lifecycle._set_phase(ServiceRuntimePhase.ACTIVE)
        # Synthetic unstarted calls need one provisional service generation;
        # start() replaces it through ServiceInstanceLease.
        handler._service_instance_lease._owner_token = "test-unstarted-service-generation"
        # Restarted fakes retain the persisted app-server thread inventory.
        for stored_binding in handler._chat_binding_store.load_all().values():
            thread_id = str(stored_binding.get("current_thread_id", "") or "").strip()
            if not thread_id:
                continue
            handler._adapter.thread_snapshots.setdefault(
                (thread_id, None),
                ThreadSnapshot(
                    summary=ThreadSummary(
                        thread_id=thread_id,
                        cwd=str(stored_binding.get("working_dir", "") or "") or "/tmp/project",
                        name=str(stored_binding.get("current_thread_title", "") or ""),
                        preview="",
                        created_at=0,
                        updated_at=0,
                        source="appServer",
                        status="idle",
                    )
                ),
            )
        self.addCleanup(handler.shutdown)
        bot = _FakeBot(data_dir)
        handler._feishu_platform.attach(bot)
        return handler, bot

    @staticmethod
    def _on_turn_completed(handler: FocusRuntime, params: dict) -> None:
        handler._runtime_call(
            handler._adapter_events.dispatch_notification,
            "turn/completed",
            params,
        )

    @staticmethod
    def _adapter_request(handler: FocusRuntime, *args) -> None:
        handler._runtime_call(
            handler._adapter_events.handle_request_for_connection,
            handler._adapter.connection_generation_value,
            *args,
        )

    @staticmethod
    def _reset_backend(handler: FocusRuntime, *, force: bool) -> dict:
        return handler._runtime_call(handler._reset_current_instance_backend, force)

    @staticmethod
    def _fcodex_operation_service(handler: FocusRuntime):
        """Expose the operation aggregate only to white-box test fixtures."""

        return handler._operation_owner._operation_service

    @staticmethod
    def _activate_main_turn_lease(handler, thread_id, holder, turn_id="turn-1"):
        acquired = handler._interaction_lease_store.acquire(thread_id, holder)
        assert acquired.granted and acquired.lease is not None
        active = handler._interaction_lease_store.activate_turn(acquired.lease, turn_id)
        assert active is not None
        return active
