"""Shared white-box fixtures for Feishu liveness and queue tests."""

import time

from bot.adapters.base import ThreadSummary
from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
)
from bot.stores.interaction_lease_store import (
    InteractionLeaseHolder,
)
from tests.focus_runtime.codex_handler_fakes import _bind_authoritative_thread


def apply_destination_loss_event(handler, chat_id: str, *, reason: str) -> bool:
    """Synchronously reconcile a durably accepted event in white-box tests."""

    event_type = (
        FeishuDestinationLossProofType.BOT_REMOVED_EVENT
        if reason == "bot_removed"
        else FeishuDestinationLossProofType.CHAT_DISBANDED_EVENT
    )
    event = FeishuDestinationLossProof(
        source_id=f"test-destination-loss-{time.monotonic_ns()}",
        chat_id=chat_id,
        proof_type=event_type,
    )
    handler._destination_liveness.accept(event)
    return bool(
        handler._runtime_call(
            handler._destination_liveness.reconcile_proof_on_runtime,
            event,
        )
    )


def prepare_terminal_feishu_fifo(
    test_case,
    handler,
    *,
    kind: str,
    message_id: str,
    text: str = "queued follow-up",
) -> tuple[str, tuple[str, str], InteractionLeaseHolder]:
    """Build a terminal root with a successor held by the Feishu FIFO."""

    root_thread_id = "thread-1"
    binding = ("ou_user", "chat-a")
    root = ThreadSummary(
        thread_id=root_thread_id,
        cwd="/tmp/project",
        name="demo",
        preview="",
        created_at=1,
        updated_at=2,
        source="cli",
        status="idle",
    )
    _bind_authoritative_thread(handler, binding[0], binding[1], root)
    handler._runtime_call(handler._binding_runtime_coordinator.activate_binding_if_needed, *binding)
    holder = handler._binding_runtime.feishu_interaction_holder(binding)
    test_case._enqueue_feishu_queue_item(
        handler,
        kind=kind,
        binding=binding,
        root_thread_id=root_thread_id,
        message_id=message_id,
        text=text,
        input_items=(({"type": "text", "text": text},) if kind == "prompt" else ()),
    )

    # Main-turn retirement may drain the successor immediately.  The queue
    # itself is not writer authority; any lease observed afterward must have
    # been acquired by the successor's own exact submission.
    handler._runtime_call(
        handler._terminal_execution.retire_ingress,
        binding[0],
        binding[1],
    )
    handler._runtime_call(
        handler._feishu_root_operations.reconcile_terminal,
        root_thread_id,
    )
    lease = handler._interaction_lease_store.load(root_thread_id)
    if lease is not None and not lease.holder.same_holder(holder):
        raise AssertionError("terminal FIFO fixture acquired the wrong holder")
    return root_thread_id, binding, holder
