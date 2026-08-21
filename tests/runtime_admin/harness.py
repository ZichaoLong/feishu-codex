"""Shared fixture for Runtime Admin controller integration tests."""

import pathlib
import tempfile
import threading
import types

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.reason_codes import (
    ReasonedCheck,
)
from bot.runtime_admin.controller import (
    RuntimeAdminController,
    RuntimeAdminCoordinationPort,
    RuntimeAdminPolicyPort,
    RuntimeAdminPorts,
    RuntimeAdminPresentationPort,
    RuntimeAdminThreadPort,
)
from bot.runtime_admin.binding_clear import (
    RuntimeBindingBatchDeactivationReceipt,
    RuntimeBindingDeactivationReceipt,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.thread_image_delivery import ThreadImageDeliveryController
from bot.thread_lifecycle_service import (
    ThreadLifecycleAdmissionPort,
    ThreadLifecycleBackendPort,
    ThreadLifecycleCleanupPort,
    ThreadLifecyclePorts,
    ThreadLifecycleService,
)
from tests.runtime_admin_test_support import make_binding_runtime


def _backend_reset_result(force: bool) -> dict[str, object]:
    return {
        "force": force,
        "detached_binding_ids": [],
        "interrupted_binding_ids": [],
        "retired_request_count": 0,
        "purged_thread_ids": [],
        "projection_warnings": [],
        "app_server_url": "ws://127.0.0.1:8765",
    }


class RuntimeAdminControllerHarnessMixin:
    def _make_controller(self, *, owner_loss_observer=None):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        chat_binding_store = ChatBindingStore(data_dir)
        interaction_lease_store, binding_runtime = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=chat_binding_store,
            owner_loss_observer=owner_loss_observer,
        )
        unsubscribed: list[str] = []
        archived: list[str] = []
        unarchived: list[str] = []
        deleted: list[str] = []
        released_runtime_leases: list[str] = []
        pending_by_thread: set[str] = set()
        pending_by_binding: set[tuple[str, str]] = set()
        summaries: dict[str, ThreadSummary] = {}
        loaded_thread_ids: list[str] = []
        pending_requests: list[dict[str, object]] = []
        reset_calls: list[bool] = []
        sent_images: list[tuple[str, str]] = []
        runtime_submissions: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        reply_texts: list[tuple[str, str, str]] = []
        reply_cards: list[tuple[str, dict[str, object], str]] = []
        submitted_prompts: list[dict[str, object]] = []
        thread_goals: dict[str, ThreadGoalSummary] = {}
        chat_display_names: dict[tuple[str, str, str], str] = {}
        chat_display_name_calls: list[dict[str, object]] = []
        invalidated_queues: list[tuple[str, str]] = []

        def _reset_current_instance_backend(force: bool) -> dict[str, object]:
            reset_calls.append(force)
            return _backend_reset_result(force)

        def _read_thread(thread_id: str):
            return ThreadSnapshot(summary=summaries[thread_id])

        def _set_thread_goal(
            thread_id: str,
            objective: str | None = None,
            status: str | None = None,
        ) -> ThreadGoalSummary:
            existing = thread_goals.get(thread_id)
            goal = ThreadGoalSummary(
                thread_id=thread_id,
                objective=(
                    str(objective or "").strip()
                    if objective is not None
                    else (existing.objective if existing is not None else "")
                ),
                status=(
                    str(status or "").strip()
                    if status is not None
                    else (existing.status if existing is not None else "active")
                )
                or "active",
                token_budget=existing.token_budget if existing is not None else None,
                tokens_used=existing.tokens_used if existing is not None else 0,
                time_used_seconds=existing.time_used_seconds if existing is not None else 0,
                created_at=existing.created_at if existing is not None else 0,
                updated_at=existing.updated_at if existing is not None else 0,
            )
            thread_goals[thread_id] = goal
            return goal

        def _resolve_binding_chat_display_name(
            *,
            binding_kind: str,
            sender_id: str,
            chat_id: str,
            refresh_names: bool = False,
        ) -> str:
            chat_display_name_calls.append(
                {
                    "binding_kind": binding_kind,
                    "sender_id": sender_id,
                    "chat_id": chat_id,
                    "refresh_names": refresh_names,
                }
            )
            return chat_display_names.get((binding_kind, sender_id, chat_id), "")

        def _deactivate_binding_and_invalidate_queue_locked(binding):
            return _deactivate_bindings_and_invalidate_queues_locked(
                (binding,), []
            )

        def _deactivate_bindings_and_invalidate_queues_locked(
            bindings,
            cleanup_errors,
        ):
            committed_removals = binding_runtime.deactivate_bindings_with_receipts_locked(
                bindings,
                cleanup_errors=cleanup_errors,
            )
            receipt = RuntimeBindingBatchDeactivationReceipt(
                confirmed_removals=tuple(
                    RuntimeBindingDeactivationReceipt(
                        binding=item.binding,
                        thread_id=item.thread_id,
                        unsubscribe_thread_id=item.unsubscribe_thread_id,
                        timer_cancellations=item.timer_cancellations,
                    )
                    for item in committed_removals
                )
            )
            invalidated_queues.extend(
                removal.binding for removal in receipt.confirmed_removals
            )
            return receipt

        interaction_requests = types.SimpleNamespace(
            thread_has_pending_request_locked=lambda thread_id: thread_id in pending_by_thread,
            binding_has_pending_request_locked=lambda binding: binding in pending_by_binding,
        )
        thread_lifecycle = ThreadLifecycleService(
            lock=lock,
            binding_runtime=binding_runtime,
            ports=ThreadLifecyclePorts(
                backend=ThreadLifecycleBackendPort(
                    read_thread=_read_thread,
                    list_loaded_thread_ids=lambda: list(loaded_thread_ids),
                    archive_thread=lambda thread_id: archived.append(thread_id),
                    unarchive_thread=(
                        lambda thread_id: unarchived.append(thread_id) or summaries[thread_id]
                    ),
                    delete_thread=lambda thread_id: deleted.append(thread_id),
                    is_thread_not_found_error=lambda exc: False,
                    is_thread_not_loaded_error=lambda exc: False,
                ),
                admission=ThreadLifecycleAdmissionPort(
                    instance_name=lambda: "corp-a",
                    load_runtime_lease=lambda thread_id: None,
                    external_write_denial_check=(
                        lambda thread_id, writer_holder=None: ReasonedCheck.allow()
                    ),
                    loaded_gate_check=lambda thread_id, operation: ReasonedCheck.allow(),
                ),
                cleanup=ThreadLifecycleCleanupPort(
                    binding_has_pending_request_locked=(
                        interaction_requests.binding_has_pending_request_locked
                    ),
                    invalidate_feishu_execution_queue_locked=(
                        lambda binding: invalidated_queues.append(binding)
                    ),
                    unsubscribe_thread=lambda thread_id: unsubscribed.append(thread_id),
                    release_service_runtime_lease=(
                        lambda thread_id: released_runtime_leases.append(thread_id)
                    ),
                ),
            ),
        )
        controller = RuntimeAdminController(
            lock=lock,
            binding_runtime=binding_runtime,
            interaction_requests=interaction_requests,
            thread_lifecycle=thread_lifecycle,
            ports=RuntimeAdminPorts(
                thread=RuntimeAdminThreadPort(
                    read_thread=_read_thread,
                    read_thread_for_stale_cleanup=_read_thread,
                    list_loaded_thread_ids=lambda: list(loaded_thread_ids),
                    current_app_server_url=lambda: "http://127.0.0.1:1234",
                    unsubscribe_thread=lambda thread_id: unsubscribed.append(thread_id),
                    attach_binding=(
                        lambda binding, thread_id, *, active_observer=False: (
                            summaries[thread_id]
                        )
                    ),
                    get_thread_goal=lambda thread_id: thread_goals.get(thread_id),
                    set_thread_goal=_set_thread_goal,
                    clear_thread_goal=lambda thread_id: thread_goals.pop(thread_id, None) is not None,
                    resolve_thread_target_for_control_params=lambda params: ThreadSummary(
                        thread_id=str(params.get("thread_id", "") or "").strip(),
                        cwd="/tmp/project",
                        name="demo",
                        preview="",
                        created_at=0,
                        updated_at=0,
                        source="cli",
                        status="idle",
                    ),
                ),
                coordination=RuntimeAdminCoordinationPort(
                    clear_all_stored_bindings=chat_binding_store.clear_all,
                    deactivate_binding_and_invalidate_queue_locked=(
                        _deactivate_binding_and_invalidate_queue_locked
                    ),
                    deactivate_bindings_and_invalidate_queues_locked=(
                        _deactivate_bindings_and_invalidate_queues_locked
                    ),
                    release_service_thread_runtime_lease=lambda thread_id: released_runtime_leases.append(thread_id),
                    service_control_endpoint=lambda: "tcp://127.0.0.1:32001",
                    web_gateway_enabled=lambda: True,
                    current_web_gateway_url=lambda: "http://127.0.0.1:8766",
                    instance_name=lambda: "corp-a",
                    load_thread_runtime_lease=lambda thread_id: None,
                    pending_interaction_request_count=lambda: len(pending_requests),
                    reset_current_instance_backend=_reset_current_instance_backend,
                    submit_to_runtime=lambda fn, *args, **kwargs: runtime_submissions.append((fn, args, kwargs)),
                    invalidate_feishu_execution_queue_locked=lambda binding: invalidated_queues.append(binding),
                    invalidate_all_feishu_execution_queues_locked=lambda: 0,
                    operational_status=lambda: {
                        "status": "ok",
                        "warnings": [],
                        "runtime_loop": {},
                    },
                ),
                policy=RuntimeAdminPolicyPort(
                    prompt_write_denial_check=lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.allow(),
                    external_control_write_denial_check=lambda thread_id, writer_holder=None: ReasonedCheck.allow(),
                    all_mode_thread_exclusivity_check=(
                        lambda chat_id, thread_id: ReasonedCheck.allow()
                    ),
                    detached_runtime_attach_check=lambda thread_id: ReasonedCheck.allow(),
                    is_thread_not_found_error=lambda exc: False,
                    is_thread_not_loaded_error=lambda exc: False,
                ),
                presentation=RuntimeAdminPresentationPort(
                    permissions_summary=lambda approval_policy, sandbox: f"{sandbox}/{approval_policy}",
                    thread_image_delivery=ThreadImageDeliveryController(
                        upload_image=lambda local_path: "img-key-1",
                        send_image_by_key=lambda chat_id, image_key: sent_images.append((chat_id, image_key)) or f"msg:{chat_id}",
                        path_exists=lambda path: True,
                        path_is_file=lambda path: True,
                    ),
                    reply_text=lambda chat_id, text, message_id="": reply_texts.append((chat_id, text, message_id)),
                    reply_card=lambda chat_id, card, message_id="": reply_cards.append((chat_id, card, message_id)),
                    submit_prompt_for_control=lambda binding, **kwargs: submitted_prompts.append(
                        {"binding": binding, **kwargs}
                    ) or {
                        "binding_id": f"p2p:{binding[0]}:{binding[1]}",
                        "thread_id": "thread-1",
                        "started": True,
                        "turn_id": "turn-1",
                        "reason_code": "",
                        "reason": "",
                        "synthetic_source": str(kwargs.get("synthetic_source", "") or ""),
                        "display_mode": str(kwargs.get("display_mode", "silent") or "silent"),
                    },
                    resolve_binding_chat_display_name=_resolve_binding_chat_display_name,
                ),
            ),
        )
        controller._submitted_prompts = submitted_prompts  # type: ignore[attr-defined]
        controller._thread_goals = thread_goals  # type: ignore[attr-defined]
        controller._unarchived = unarchived  # type: ignore[attr-defined]
        controller._deleted = deleted  # type: ignore[attr-defined]
        controller._chat_display_names = chat_display_names  # type: ignore[attr-defined]
        controller._chat_display_name_calls = chat_display_name_calls  # type: ignore[attr-defined]
        controller._invalidated_queues = invalidated_queues  # type: ignore[attr-defined]
        return (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            unsubscribed,
            archived,
            released_runtime_leases,
            pending_by_thread,
            pending_by_binding,
            pending_requests,
            reset_calls,
            sent_images,
        )

    def _bind_thread(self, lock, owner, binding, *, thread_id: str):
        with lock:
            state = owner._get_or_create_runtime_state_locked(binding)
            owner.bind_thread_locked(
                owner.resident_session_snapshot_locked(binding).handle,
                thread_id=thread_id,
                thread_title="demo",
                working_dir="/tmp/project",
            )
        return state

    @staticmethod
    def _direct_root_summary(thread_id: str) -> ThreadSummary:
        return ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
