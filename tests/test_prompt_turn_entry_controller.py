import pathlib
import tempfile
import threading
import unittest
from dataclasses import replace

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_execution_runtime import BindingExecutionRuntimeTransitions
from bot.binding_runtime_contract import BindingExecutionTarget
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import CodexRpcPreSendError
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.feishu_root_operation_contract import (
    FeishuPromptInterruptCandidateClaim,
    FeishuRootContinuationToken,
    FeishuRootOperationToken,
)
from bot.prompt_turn_entry_controller import (
    FeishuRootOperationPort,
    InteractionPort,
    PresentationPort,
    PromptTurnEntryController,
    PromptTurnEntryPorts,
    ThreadSessionPort,
)
from bot.reason_codes import PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER, ReasonedCheck
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    ThreadStateChanged,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.thread_runtime_authority import (
    ThreadResumeLeaseReceipt,
    ThreadResumeLocalCommitFailed,
    ThreadResumeLocalFailurePolicy,
    ThreadResumeSettlement,
    ThreadResumeSettlementError,
    ThreadResumeSettlementOutcome,
)
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state


def _confirmed_patch(
    chat_id: str,
    message_id: str,
    _content: str,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=FeishuOutboundOperation.PATCH_MESSAGE,
        effect=FeishuOutboundEffect.CONFIRMED,
        destination_liveness=FeishuDestinationLiveness.REACHABLE,
        chat_id=chat_id,
        attempt_id="test-patch",
        message_id=message_id,
    )


class PromptTurnEntryControllerTests(unittest.TestCase):
    def _make_controller(self, *, continuation_may_autostart: bool = True):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        chat_binding_store = ChatBindingStore(data_dir)
        binding_runtime = BindingRuntimeManager(
            lock=lock,
            default_working_dir="/tmp/project",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=chat_binding_store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        turn_execution = TurnExecutionCoordinator()
        execution_runtime = BindingExecutionRuntimeTransitions(
            lock=lock,
            binding_runtime=binding_runtime,
            turn_execution=turn_execution,
        )
        binding = ("ou_user", "c1")
        binding_runtime.resolve_session(*binding)
        with lock:
            state = binding_runtime.resident_runtime_state_locked(binding)
            assert state is not None

        replies: list[tuple[str, str, str, bool]] = []
        create_thread_calls: list[dict] = []
        resume_calls: list[dict] = []
        start_turn_calls: list[dict] = []
        interrupt_calls: list[dict] = []
        sent_execution_cards: list[tuple[str, str, bool]] = []
        flushed: list[tuple[str, str, bool]] = []
        reconciled: list[tuple[str, str, str, str]] = []
        refreshed: list[tuple[str, str]] = []
        finalized: list[tuple[str, str]] = []
        degraded: list[tuple[str, str, str]] = []
        scheduled_watchdogs: list[tuple[str, str]] = []
        reserved_cards: dict[str, str] = {}
        root_operation_admissions: list[dict] = []
        root_operation_outcomes: list[tuple[FeishuRootOperationToken, str, str]] = []
        root_continuation_arms: list[tuple[FeishuRootOperationToken, str, str]] = []
        root_start_identity_waits: list[FeishuRootOperationToken] = []
        root_operation_unknowns: list[tuple[FeishuRootOperationToken, str]] = []
        interaction_acquire_calls: list[tuple[tuple[str, str], str]] = []
        binding_sync_calls: list[tuple[tuple[str, str], str]] = []
        create_thread_result = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="created",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            )
        )
        resume_summaries: dict[str, ThreadSummary] = {
            "thread-1": ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            )
        }
        start_turn_behavior = {
            "value": {"turn": {"id": "submission-1"}},
        }
        interrupt_behavior = {"exc": None}
        access_policy = ThreadAccessPolicy(
            lock=lock,
            is_group_chat=lambda chat_id, message_id: False,
            group_mode_for_chat=lambda chat_id: "assistant",
            thread_subscribers_locked=binding_runtime.thread_subscribers,
            current_interaction_lease_locked=binding_runtime.current_interaction_lease_locked,
            feishu_interaction_holder=binding_runtime.feishu_interaction_holder,
        )

        def _resolve_session(sender_id: str, chat_id: str, message_id: str = ""):
            with lock:
                session = binding_runtime.resident_session_snapshot_locked(binding)
                assert session is not None
                return session

        def _bind_thread(
            sender_id: str, chat_id: str, thread: ThreadSummary, *, message_id: str = ""
        ) -> None:
            del sender_id, chat_id, message_id
            with lock:
                session = binding_runtime.resident_session_snapshot_locked(binding)
                assert session is not None
                binding_runtime.bind_thread_locked(
                    session.handle,
                    thread_id=thread.thread_id,
                    thread_title=thread.title,
                    working_dir=thread.cwd or session.working_dir,
                )

        def _clear_thread_binding(
            sender_id: str,
            chat_id: str,
            *,
            message_id: str = "",
            session=None,
        ) -> None:
            del sender_id, chat_id, message_id
            with lock:
                session = session or binding_runtime.resident_session_snapshot_locked(
                    binding
                )
                assert session is not None
                binding_runtime.clear_thread_binding_locked(
                    session.handle,
                )

        def _reattach_bound_thread(
            sender_id: str,
            chat_id: str,
            thread_id: str,
            *,
            original_arg: str,
            summary: ThreadSummary | None = None,
            retain_on_local_failure: bool,
            message_id: str = "",
            exact_mutation_guard=None,
        ) -> str:
            resume_calls.append(
                {
                    "thread_id": thread_id,
                    "original_arg": original_arg,
                    "summary": summary.thread_id if summary is not None else "",
                    "retain_on_local_failure": retain_on_local_failure,
                    "exact_mutation_guard": exact_mutation_guard,
                }
            )
            _bind_thread(
                sender_id,
                chat_id,
                resume_summaries[thread_id],
                message_id=message_id,
            )
            return thread_id

        def _create_and_bind_thread(
            sender_id: str,
            chat_id: str,
            *,
            message_id: str = "",
            **kwargs,
        ):
            create_thread_calls.append(dict(kwargs))
            _bind_thread(
                sender_id,
                chat_id,
                create_thread_result.summary,
                message_id=message_id,
            )
            return create_thread_result

        def _start_turn(**kwargs):
            snapshot = dict(kwargs)
            input_items = [dict(item) for item in snapshot.get("input_items", [])]
            snapshot["input_items"] = input_items
            snapshot["text"] = "\n".join(
                item.get("text", "")
                for item in input_items
                if isinstance(item, dict) and item.get("type") == "text"
            )
            start_turn_calls.append(snapshot)
            value = start_turn_behavior["value"]
            if isinstance(value, Exception):
                if len(start_turn_calls) == 1 and isinstance(value, Exception):
                    start_turn_behavior["value"] = {
                        "turn": {"id": "submission-1"},
                    }
                    raise value
            return value

        def _interrupt_running_turn(*, thread_id: str, turn_id: str) -> None:
            interrupt_calls.append({"thread_id": thread_id, "turn_id": turn_id})
            if interrupt_behavior["exc"] is not None:
                raise interrupt_behavior["exc"]

        root_operation_issuer = 937
        next_root_operation_nonce = 0
        root_continuation_issuer = 938
        next_root_continuation_nonce = 0
        next_interrupt_candidate_claim_nonce = 0
        root_continuation_receipts: list[FeishuRootContinuationToken] = []
        root_operation_state: dict[int, dict] = {}

        def _require_root_operation_token(
            token: FeishuRootOperationToken,
        ) -> dict:
            current = root_operation_state.get(token._token_nonce)
            if (
                token._issuer_nonce != root_operation_issuer
                or current is None
                or current["token"] is not token
            ):
                raise RuntimeError("stale root operation token")
            return current

        def _admit_root_operation(
            binding_key,
            thread_id: str,
            *,
            chat_id: str,
            message_id: str = "",
            reason: str,
            operation_kind: str = "mutation",
        ) -> FeishuRootOperationToken:
            nonlocal next_root_operation_nonce
            denial_text = access_policy.prompt_write_denial_text(
                binding_key,
                chat_id,
                thread_id,
                message_id=message_id,
            )
            if denial_text:
                raise PermissionError(denial_text)
            interaction_acquire_calls.append((binding_key, thread_id))
            lease = binding_runtime.acquire_interaction_lease_for_binding(
                binding_key,
                thread_id,
            )
            if not lease.granted:
                raise PermissionError(
                    access_policy.interaction_denied_text(lease.lease)
                )
            next_root_operation_nonce += 1
            token = FeishuRootOperationToken(
                root_operation_issuer,
                next_root_operation_nonce,
            )
            root_operation_state[token._token_nonce] = {
                "token": token,
                "binding": binding_key,
                "thread_id": thread_id,
                "lease_acquired": lease.acquired,
                "continuation_generation": 0,
                "awaiting_start_identity": False,
                "operation_kind": operation_kind,
                "interrupt_candidate_id": "",
                "interrupt_candidate_claim": None,
            }
            root_operation_admissions.append(
                {
                    "token": token,
                    "binding": binding_key,
                    "thread_id": thread_id,
                    "reason": reason,
                    "operation_kind": operation_kind,
                }
            )
            return token

        def _arm_root_continuation(
            token: FeishuRootOperationToken,
            *,
            reason: str,
        ) -> FeishuRootContinuationToken:
            nonlocal next_root_continuation_nonce
            current = _require_root_operation_token(token)
            current["continuation_generation"] += 1
            next_root_continuation_nonce += 1
            receipt = FeishuRootContinuationToken(
                root_continuation_issuer,
                next_root_continuation_nonce,
            )
            root_continuation_receipts.append(receipt)
            root_continuation_arms.append((token, current["thread_id"], reason))
            return receipt

        def _await_root_start_identity(token: FeishuRootOperationToken) -> None:
            current = _require_root_operation_token(token)
            current["awaiting_start_identity"] = True
            root_start_identity_waits.append(token)

        def _accept_prompt_start(
            token: FeishuRootOperationToken,
            response_turn_id: str,
        ) -> None:
            current = _require_root_operation_token(token)
            _await_root_start_identity(token)
            current["interrupt_candidate_id"] = response_turn_id

        def _claim_prompt_interrupt_candidate(
            binding_key,
            thread_id: str,
        ) -> FeishuPromptInterruptCandidateClaim | None:
            nonlocal next_interrupt_candidate_claim_nonce
            current = next(
                (
                    item
                    for item in root_operation_state.values()
                    if item["binding"] == binding_key
                    and item["thread_id"] == thread_id
                    and item["interrupt_candidate_id"]
                    and item["interrupt_candidate_claim"] is None
                ),
                None,
            )
            if current is None:
                return None
            next_interrupt_candidate_claim_nonce += 1
            claim = FeishuPromptInterruptCandidateClaim(
                turn_id=current["interrupt_candidate_id"],
                _issuer_nonce=root_operation_issuer,
                _token=current["token"],
                _claim_nonce=next_interrupt_candidate_claim_nonce,
            )
            current["interrupt_candidate_id"] = ""
            current["interrupt_candidate_claim"] = claim
            return claim

        def _consume_prompt_interrupt_candidate(
            claim: FeishuPromptInterruptCandidateClaim,
        ) -> bool:
            current = root_operation_state.get(claim._token._token_nonce)
            if current is None or current["interrupt_candidate_claim"] is not claim:
                return False
            current["interrupt_candidate_claim"] = None
            return True

        def _restore_prompt_interrupt_candidate_after_pre_send(
            claim: FeishuPromptInterruptCandidateClaim,
            *,
            error: CodexRpcPreSendError,
        ) -> bool:
            if not isinstance(error, CodexRpcPreSendError):
                raise TypeError("typed pre-send required")
            current = root_operation_state.get(claim._token._token_nonce)
            if current is None or current["interrupt_candidate_claim"] is not claim:
                return False
            current["interrupt_candidate_claim"] = None
            current["interrupt_candidate_id"] = claim.turn_id
            return True

        def _finish_root_operation(
            token: FeishuRootOperationToken,
            *,
            outcome: str,
            reason: str,
            release_lease: bool,
        ) -> None:
            current = _require_root_operation_token(token)
            root_operation_outcomes.append((token, outcome, reason))
            root_operation_state.pop(token._token_nonce, None)
            if release_lease and current["lease_acquired"]:
                binding_runtime.release_interaction_lease_for_binding(
                    current["binding"],
                    current["thread_id"],
                )

        def _settle_root_known_failure(
            token: FeishuRootOperationToken,
            *,
            reason: str,
        ) -> None:
            _finish_root_operation(
                token,
                outcome="known_failure",
                reason=reason,
                release_lease=True,
            )

        def _settle_root_known_mutation(
            token: FeishuRootOperationToken,
            *,
            reason: str,
        ) -> None:
            _finish_root_operation(
                token,
                outcome="known_mutation",
                reason=reason,
                release_lease=True,
            )

        def _acknowledge_root_continuing(
            token: FeishuRootOperationToken,
            *,
            turn_id: str = "",
        ) -> None:
            _finish_root_operation(
                token,
                outcome="continuing",
                reason="",
                release_lease=False,
            )

        def _mark_root_outcome_unknown(
            token: FeishuRootOperationToken,
            *,
            reason: str,
        ) -> None:
            _require_root_operation_token(token)
            root_operation_unknowns.append((token, reason))
            _finish_root_operation(
                token,
                outcome="unknown",
                reason=reason,
                release_lease=False,
            )

        original_persist_session = execution_runtime.persist_session

        def _persist_session(session):
            binding_sync_calls.append((session.binding, session.current_thread_id))
            return original_persist_session(session)

        execution_runtime.persist_session = _persist_session

        def _open_initial_execution_page(
            session,
            parent_message_id: str,
            *,
            reply_in_thread: bool = False,
            reserved_message_id: str = "",
        ) -> InitialExecutionPageOpenResult:
            target = BindingExecutionTarget.from_session(session)
            message_id = reserved_message_id or "card-1"
            if not reserved_message_id:
                sent_execution_cards.append(
                    (session.binding[1], parent_message_id, reply_in_thread)
                )
            with lock:
                current = binding_runtime.session_snapshot_locked(session.handle)
                if not target.matches(current):
                    return InitialExecutionPageOpenResult(
                        status=InitialExecutionPageOpenStatus.STALE,
                        session=None,
                    )
                current_state = binding_runtime.resident_runtime_state_locked(
                    session.binding
                )
                assert current_state is not None
                set_execution_page_state(
                    current_state,
                    current_message_id=message_id,
                )
                updated = binding_runtime.session_snapshot_locked(session.handle)
            return InitialExecutionPageOpenResult(
                status=InitialExecutionPageOpenStatus.ACTIVE,
                session=updated,
                message_id=message_id,
            )

        controller = PromptTurnEntryController(
            execution_runtime=execution_runtime,
            ports=PromptTurnEntryPorts(
                session=ThreadSessionPort(
                    resolve_session=_resolve_session,
                    clear_thread_binding=_clear_thread_binding,
                    reattach_bound_thread=_reattach_bound_thread,
                    create_and_bind_thread=_create_and_bind_thread,
                    message_reply_in_thread=lambda message_id: message_id.startswith(
                        "thread-"
                    ),
                    group_actor_open_id=lambda message_id: "ou_actor"
                    if message_id
                    else "",
                    access_policy=access_policy,
                    detached_runtime_attach_check=lambda thread_id: ReasonedCheck.allow(),
                ),
                root_operation=FeishuRootOperationPort(
                    admit=_admit_root_operation,
                    arm_continuation=_arm_root_continuation,
                    await_start_identity=_await_root_start_identity,
                    accept_prompt_start=_accept_prompt_start,
                    claim_prompt_interrupt_candidate=(
                        _claim_prompt_interrupt_candidate
                    ),
                    consume_prompt_interrupt_candidate=(
                        _consume_prompt_interrupt_candidate
                    ),
                    restore_prompt_interrupt_candidate_after_pre_send=(
                        _restore_prompt_interrupt_candidate_after_pre_send
                    ),
                    settle_known_failure=_settle_root_known_failure,
                    settle_known_mutation=_settle_root_known_mutation,
                    acknowledge_continuing=_acknowledge_root_continuing,
                    mark_outcome_unknown=_mark_root_outcome_unknown,
                    continuation_may_autostart=lambda _thread_id: continuation_may_autostart,
                ),
                interaction=InteractionPort(
                    runtime_recovery_reason=lambda exc: str(exc),
                    operation_outcome_unknown=lambda exc: str(exc) == "unknown",
                    is_turn_thread_not_found_error=lambda exc: str(exc)
                    == "thread not found",
                    is_thread_not_found_error=lambda exc: str(exc) == "thread missing",
                    is_pre_send_error=lambda exc: isinstance(
                        exc,
                        CodexRpcPreSendError,
                    ),
                    is_turn_interrupt_rejected_error=lambda exc: str(exc)
                    == "rejected",
                    start_turn=_start_turn,
                    interrupt_running_turn=_interrupt_running_turn,
                    finalize_input_items=lambda _thread_id,
                    _requested_model,
                    items: items,
                ),
                presentation=PresentationPort(
                    claim_reserved_execution_card=lambda message_id: reserved_cards.pop(
                        message_id, ""
                    ),
                    patch_message=_confirmed_patch,
                    open_initial_execution_page=_open_initial_execution_page,
                    flush_execution_card_for_session=lambda session,
                    immediate=False: flushed.append(
                        (session.binding[0], session.binding[1], bool(immediate))
                    ),
                    schedule_mirror_watchdog=lambda sender_id,
                    chat_id: scheduled_watchdogs.append((sender_id, chat_id)),
                    reconcile_execution_snapshot=lambda sender_id,
                    chat_id,
                    *,
                    thread_id,
                    turn_id="": (
                        reconciled.append((sender_id, chat_id, thread_id, turn_id)),
                        False,
                    )[1],
                    refresh_terminal_card=lambda session: (
                        refreshed.append(session.binding),
                        True,
                    )[1],
                    finalize_execution=lambda session: (
                        finalized.append(session.binding),
                        True,
                    )[1],
                    mark_runtime_degraded=lambda sender_id,
                    chat_id,
                    *,
                    reason: degraded.append((sender_id, chat_id, reason)),
                    reply_text=lambda chat_id, text, **kwargs: replies.append(
                        (
                            chat_id,
                            text,
                            str(kwargs.get("message_id", "") or ""),
                            bool(kwargs.get("reply_in_thread", False)),
                        )
                    ),
                    mirror_watchdog_seconds=lambda: 8.0,
                ),
            ),
        )

        return {
            "lock": lock,
            "binding_runtime": binding_runtime,
            "chat_binding_store": chat_binding_store,
            "turn_execution": turn_execution,
            "execution_runtime": execution_runtime,
            "binding": binding,
            "state": state,
            "controller": controller,
            "bind_thread_fn": _bind_thread,
            "replies": replies,
            "create_thread_calls": create_thread_calls,
            "resume_calls": resume_calls,
            "start_turn_calls": start_turn_calls,
            "interrupt_calls": interrupt_calls,
            "sent_execution_cards": sent_execution_cards,
            "flushed": flushed,
            "reconciled": reconciled,
            "refreshed": refreshed,
            "finalized": finalized,
            "degraded": degraded,
            "scheduled_watchdogs": scheduled_watchdogs,
            "reserved_cards": reserved_cards,
            "root_operation_admissions": root_operation_admissions,
            "root_operation_outcomes": root_operation_outcomes,
            "root_continuation_arms": root_continuation_arms,
            "root_continuation_receipts": root_continuation_receipts,
            "root_start_identity_waits": root_start_identity_waits,
            "root_operation_unknowns": root_operation_unknowns,
            "root_operation_state": root_operation_state,
            "interaction_acquire_calls": interaction_acquire_calls,
            "binding_sync_calls": binding_sync_calls,
            "resume_summaries": resume_summaries,
            "start_turn_behavior": start_turn_behavior,
            "interrupt_behavior": interrupt_behavior,
        }

    def test_cancel_pre_send_failure_preserves_cancel_intent_and_reports_not_sent(
        self,
    ) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1")
        with env["lock"]:
            env["state"]["running"] = True
            env["state"]["current_turn_id"] = "turn-1"
        env["interrupt_behavior"]["exc"] = CodexRpcPreSendError(
            "turn/interrupt",
            RuntimeError("pre-send"),
        )

        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertFalse(ok)
        self.assertIn("未发送", message)
        self.assertIn("重试 `/cancel`", message)
        self.assertTrue(env["state"]["pending_cancel"])
        self.assertFalse(env["state"]["cancelled"])
        self.assertIn("pre-send", env["degraded"][0][2])

    def test_active_observer_cannot_cancel_current_turn(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1")
        with env["lock"]:
            env["state"]["running"] = True
            env["state"]["current_turn_id"] = "turn-1"
            env["state"]["current_execution_kind"] = (
                ACTIVE_OBSERVER_EXECUTION_KIND
            )

        ok, message = env["controller"].cancel_current_turn(
            "ou_user",
            "c1",
        )

        self.assertFalse(ok)
        self.assertIn("observer", message)
        self.assertEqual(env["interrupt_calls"], [])
        self.assertFalse(env["state"]["pending_cancel"])

    def test_cancel_uses_start_response_id_once_without_binding_lifecycle(self) -> None:
        env = self._make_controller()
        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )
        self.assertTrue(result.started)
        self.assertEqual(env["state"]["current_turn_id"], "")

        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(
            env["interrupt_calls"],
            [{"thread_id": "thread-created", "turn_id": "submission-1"}],
        )
        self.assertFalse(env["state"]["pending_cancel"])
        self.assertFalse(env["state"]["cancelled"])
        token = self._admitted_root_token(env)
        state = env["root_operation_state"][token._token_nonce]
        self.assertEqual(state["interrupt_candidate_id"], "")
        self.assertIsNone(state["interrupt_candidate_claim"])

    def test_cancel_prefers_actual_turn_id_over_unclaimed_start_candidate(self) -> None:
        env = self._make_controller()
        env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )
        with env["lock"]:
            env["state"]["current_turn_id"] = "turn-actual"

        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(
            env["interrupt_calls"],
            [{"thread_id": "thread-created", "turn_id": "turn-actual"}],
        )
        token = self._admitted_root_token(env)
        state = env["root_operation_state"][token._token_nonce]
        self.assertEqual(state["interrupt_candidate_id"], "submission-1")
        self.assertIsNone(state["interrupt_candidate_claim"])

    def test_candidate_is_restored_only_after_pre_send_failure(self) -> None:
        env = self._make_controller()
        env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )
        env["interrupt_behavior"]["exc"] = CodexRpcPreSendError(
            "turn/interrupt",
            RuntimeError("pre-send"),
        )

        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertFalse(ok)
        self.assertIn("未发送", message)
        self.assertTrue(env["state"]["pending_cancel"])
        token = self._admitted_root_token(env)
        state = env["root_operation_state"][token._token_nonce]
        self.assertEqual(state["interrupt_candidate_id"], "submission-1")

        env["interrupt_behavior"]["exc"] = None
        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(
            env["interrupt_calls"],
            [
                {"thread_id": "thread-created", "turn_id": "submission-1"},
                {"thread_id": "thread-created", "turn_id": "submission-1"},
            ],
        )
        self.assertFalse(env["state"]["pending_cancel"])

    def test_candidate_known_rejection_is_not_cancelled_and_is_not_retried(self) -> None:
        env = self._make_controller()
        env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )
        env["interrupt_behavior"]["exc"] = RuntimeError("rejected")

        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertFalse(ok)
        self.assertIn("未取消", message)
        self.assertFalse(env["state"]["pending_cancel"])
        env["interrupt_behavior"]["exc"] = None
        retry_ok, retry_message = env["controller"].cancel_current_turn(
            "ou_user", "c1"
        )
        self.assertFalse(retry_ok)
        self.assertIn("本次未取消", retry_message)
        self.assertEqual(len(env["interrupt_calls"]), 1)

    def test_candidate_unknown_is_distinct_and_is_not_retried(self) -> None:
        env = self._make_controller()
        env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )
        env["interrupt_behavior"]["exc"] = RuntimeError("unknown")

        ok, message = env["controller"].cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertIn("可能已发送", message)
        self.assertIn("结果未知", message)
        self.assertFalse(env["state"]["pending_cancel"])
        env["interrupt_behavior"]["exc"] = None
        retry_ok, _retry_message = env["controller"].cancel_current_turn(
            "ou_user", "c1"
        )
        self.assertFalse(retry_ok)
        self.assertEqual(len(env["interrupt_calls"]), 1)

    def test_cancel_action_from_sealed_page_cannot_interrupt_active_page(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1")
        with env["lock"]:
            env["state"]["running"] = True
            env["state"]["current_turn_id"] = "turn-1"
            set_execution_page_state(
                env["state"],
                last_message_id="sealed-card",
                current_message_id="active-card",
            )

        ok, message = env["controller"].cancel_current_turn(
            "ou_user",
            "c1",
            message_id="sealed-card",
            action_page_message_id="sealed-card",
        )

        self.assertFalse(ok)
        self.assertIn("已归档", message)
        self.assertEqual(env["interrupt_calls"], [])
        self.assertFalse(env["state"]["pending_cancel"])
        self.assertFalse(env["state"]["cancelled"])

    def test_plain_cancel_remains_available_after_execution_rollover(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1")
        with env["lock"]:
            env["state"]["running"] = True
            env["state"]["current_turn_id"] = "turn-1"
            set_execution_page_state(
                env["state"],
                last_message_id="sealed-card",
                current_message_id="active-card",
            )

        ok, message = env["controller"].cancel_current_turn(
            "ou_user",
            "c1",
        )

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(
            env["interrupt_calls"],
            [{"thread_id": "thread-1", "turn_id": "turn-1"}],
        )
        self.assertFalse(env["state"]["cancelled"])

    def _bind_thread(
        self, env, *, thread_id: str, runtime_state: str = "attached"
    ) -> None:
        thread = ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        env["resume_summaries"][thread_id] = thread
        env["bind_thread_fn"]("ou_user", "c1", thread)
        if runtime_state == "detached":
            with env["lock"]:
                env["binding_runtime"].unsubscribe_thread_locked(
                    env["binding"], thread_id
                )
                env["binding_runtime"]._apply_persisted_runtime_state_message_locked(
                    env["binding"],
                    env["state"],
                    ThreadStateChanged(feishu_runtime_state="detached"),
                )

    def _admitted_root_token(self, env) -> FeishuRootOperationToken:
        self.assertEqual(len(env["root_operation_admissions"]), 1)
        token = env["root_operation_admissions"][0]["token"]
        self.assertIsInstance(token, FeishuRootOperationToken)
        return token

    @staticmethod
    def _resume_settlement_error(
        *,
        thread_id: str = "thread-1",
        recovery_required: bool = False,
    ) -> ThreadResumeSettlementError:
        return ThreadResumeSettlementError(
            ThreadResumeSettlement(
                thread_id=thread_id,
                generation=1,
                outcome=ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION,
                recovery_required=recovery_required,
            ),
            "resume ACK 后 receipt settlement 失败",
        )

    @staticmethod
    def _resume_local_commit_error(
        *,
        thread_id: str = "thread-1",
        recovery_required: bool = False,
    ) -> ThreadResumeLocalCommitFailed:
        receipt = ThreadResumeLeaseReceipt(
            thread_id=thread_id,
            lease_was_newly_acquired=False,
            generation=1,
            _authority_token=object(),
            _receipt_token=object(),
        )
        return ThreadResumeLocalCommitFailed(
            lease_receipt=receipt,
            original_error=RuntimeError("binding commit callback failed"),
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            settlement=ThreadResumeSettlement(
                thread_id=thread_id,
                generation=1,
                outcome=ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION,
                recovery_required=recovery_required,
            ),
        )

    @staticmethod
    def _root_outcomes_for(env, token: FeishuRootOperationToken):
        return [
            (outcome, reason)
            for observed_token, outcome, reason in env["root_operation_outcomes"]
            if observed_token is token
        ]

    def test_handle_prompt_replies_when_turn_is_already_running(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        env["state"]["running"] = True
        env["state"]["current_thread_id"] = "thread-1"
        env["state"]["current_turn_id"] = "turn-1"
        set_execution_page_state(
            env["state"],
            current_message_id="card-1",
        )
        env["state"]["last_runtime_event_at"] = 0.0

        controller.handle_prompt("ou_user", "c1", "follow up", message_id="msg-1")

        self.assertEqual(env["start_turn_calls"], [])
        self.assertEqual(
            env["replies"],
            [
                (
                    "c1",
                    "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
                    "msg-1",
                    False,
                )
            ],
        )

    def test_start_prompt_turn_rejects_when_interaction_lease_is_held_by_another_binding(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        with env["lock"]:
            env["binding_runtime"].acquire_interaction_lease_for_binding(
                ("ou_other", "c2"), "thread-1"
            )

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual(env["start_turn_calls"], [])
        self.assertIn("当前线程正由另一飞书会话执行", env["replies"][-1][1])

    def test_detached_prompt_resumes_then_starts_turn(self) -> None:
        env = self._make_controller(continuation_may_autostart=False)
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertTrue(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [],
        )
        self.assertEqual(env["root_start_identity_waits"], [token])
        self.assertEqual(
            [call["thread_id"] for call in env["resume_calls"]],
            ["thread-1"],
        )
        self.assertEqual(
            [call["thread_id"] for call in env["start_turn_calls"]],
            ["thread-1"],
        )

    def test_binding_sync_failure_settles_the_exact_admission(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1")

        def _failed_sync(*_args, **_kwargs):
            raise RuntimeError("binding sync failed")

        env["execution_runtime"].persist_session = _failed_sync

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_binding_sync_failed")],
        )
        self.assertEqual(env["root_operation_state"], {})

    def test_continuation_arm_failure_settles_the_exact_admission(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        def _failed_arm(*_args, **_kwargs):
            raise RuntimeError("arm failed")

        env["controller"]._arm_root_continuation = _failed_arm

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(env["resume_calls"], [])
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_prestart_fence_failed")],
        )

    def test_continuation_arm_requires_an_exact_typed_receipt(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")
        env["controller"]._arm_root_continuation = lambda *_args, **_kwargs: None

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        self.assertEqual(env["resume_calls"], [])
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_prestart_fence_failed")],
        )

    def test_start_prompt_turn_rebinds_detached_thread_before_starting(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual(
            [call["thread_id"] for call in env["resume_calls"]], ["thread-1"]
        )
        self.assertTrue(env["resume_calls"][0]["retain_on_local_failure"])
        self.assertEqual(env["start_turn_calls"][-1]["thread_id"], "thread-1")
        self.assertEqual(env["state"]["feishu_runtime_state"], "attached")

    def test_start_prompt_turn_pure_rejects_detached_thread_when_live_runtime_owner_blocks_attach(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")
        controller._detached_runtime_attach_check = (
            lambda thread_id: ReasonedCheck.deny(
                PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
                "当前线程正由实例 `default` 的本地 `fcodex` 持有 live runtime；当前不支持跨实例继续。",
            )
        )

        started = controller.start_prompt_turn(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(started)
        self.assertEqual(env["resume_calls"], [])
        self.assertEqual(env["start_turn_calls"], [])
        self.assertEqual(
            env["replies"][-1],
            (
                "c1",
                "当前线程正由实例 `default` 的本地 `fcodex` 持有 live runtime；当前不支持跨实例继续。",
                "msg-1",
                False,
            ),
        )

    def test_start_prompt_turn_retries_after_thread_not_found(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        env["start_turn_behavior"]["value"] = RuntimeError("thread not found")

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual(
            [call["thread_id"] for call in env["resume_calls"]], ["thread-1"]
        )
        self.assertTrue(env["resume_calls"][0]["retain_on_local_failure"])
        self.assertEqual(
            [call["thread_id"] for call in env["start_turn_calls"]],
            ["thread-1", "thread-1"],
        )
        self.assertEqual(env["state"]["current_turn_id"], "")
        self.assertTrue(env["state"]["awaiting_local_turn_started"])
        self.assertEqual(env["scheduled_watchdogs"], [("ou_user", "c1")])

    def test_turn_start_submission_id_is_not_bound_as_active_turn(self) -> None:
        env = self._make_controller()
        self._bind_thread(env, thread_id="thread-1")

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertTrue(result.started)
        self.assertEqual(result.turn_id, "")
        self.assertEqual(env["state"]["current_turn_id"], "")
        self.assertTrue(env["state"]["awaiting_local_turn_started"])
        token = self._admitted_root_token(env)
        self.assertEqual(env["root_start_identity_waits"], [token])
        self.assertEqual(
            env["root_operation_state"][token._token_nonce]["thread_id"],
            "thread-1",
        )

    def test_safe_fallback_resume_then_known_rejection_settles_known_mutation(
        self,
    ) -> None:
        env = self._make_controller(continuation_may_autostart=False)
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")

        def _start_then_reject(**kwargs):
            env["start_turn_calls"].append(dict(kwargs))
            if len(env["start_turn_calls"]) == 1:
                raise RuntimeError("thread not found")
            raise RuntimeError("rejected")

        controller._start_turn = _start_then_reject
        causal_order: list[tuple[str, bool, bool]] = []
        original_transition = controller._transition_prompt_start_failure
        settlement_service = controller._operation_settlement
        original_settle = settlement_service._ports.settle_known_mutation
        original_cleanup = controller._cleanup_prompt_start_failure

        def _record_transition(*args, **kwargs):
            original_transition(*args, **kwargs)
            causal_order.append(
                (
                    "state",
                    bool(env["state"]["running"]),
                    env["turn_execution"].has_active_execution_locked(env["state"]),
                )
            )

        def _record_settlement(*args, **kwargs):
            causal_order.append(
                (
                    "owner",
                    bool(env["state"]["running"]),
                    env["turn_execution"].has_active_execution_locked(env["state"]),
                )
            )
            return original_settle(*args, **kwargs)

        def _record_cleanup(*args, **kwargs):
            causal_order.append(
                (
                    "cleanup",
                    bool(env["state"]["running"]),
                    env["turn_execution"].has_active_execution_locked(env["state"]),
                )
            )
            return original_cleanup(*args, **kwargs)

        controller._transition_prompt_start_failure = _record_transition
        settlement_service._ports = replace(
            settlement_service._ports,
            settle_known_mutation=_record_settlement,
        )
        controller._cleanup_prompt_start_failure = _record_cleanup

        result = controller.start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(env["root_continuation_arms"], [])
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_mutation", "feishu_prompt_start_after_resume_failed")],
        )
        self.assertEqual(
            causal_order,
            [
                ("state", False, False),
                ("owner", False, False),
                ("cleanup", False, False),
            ],
        )

    def test_fenced_fallback_resume_then_known_rejection_keeps_continuing(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")

        def _start_then_reject(**kwargs):
            env["start_turn_calls"].append(dict(kwargs))
            if len(env["start_turn_calls"]) == 1:
                raise RuntimeError("thread not found")
            raise RuntimeError("already running")

        controller._start_turn = _start_then_reject

        result = controller.start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_continuation_arms"],
            [(token, "thread-1", "feishu_prompt_fallback_resume_prestart")],
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("continuing", "")],
        )
        self.assertEqual(env["root_start_identity_waits"], [])

    def test_start_prompt_turn_releases_pre_attached_lease_by_detached_thread_id_on_all_mode_exclusivity_violation(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        controller.ensure_binding_runtime_attached = lambda *args, **kwargs: "thread-2"
        controller._access_policy.all_mode_thread_exclusivity_violation = (
            lambda *args, **kwargs: "sharing denied"
        )

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual(env["replies"][-1][1], "sharing denied")
        with env["lock"]:
            owner = env["binding_runtime"].interaction_owner_snapshot_locked("thread-1")
        self.assertEqual(owner["kind"], "none")

    def test_start_prompt_turn_creates_new_thread_without_instance_profile_seed(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]

        started = controller.start_prompt_turn(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertTrue(started)
        self.assertNotIn("profile", env["start_turn_calls"][-1])

    def test_start_prompt_turn_uses_seeded_runtime_model_and_effort(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertTrue(result.started)
        self.assertEqual(env["start_turn_calls"][-1]["model"], "gpt-5.4")
        self.assertEqual(env["start_turn_calls"][-1]["reasoning_effort"], "medium")

    def test_start_prompt_turn_uses_runtime_effort_only(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        env["state"]["reasoning_effort"] = "high"
        env["start_turn_calls"].clear()

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertTrue(result.started)
        self.assertEqual(env["start_turn_calls"][-1]["reasoning_effort"], "high")

    def test_start_prompt_turn_does_not_send_collaboration_mode(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        env["start_turn_calls"].clear()

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertTrue(result.started)
        self.assertNotIn("collaboration_mode", env["start_turn_calls"][-1])

    def test_start_prompt_turn_continues_when_execution_card_is_rejected(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        controller._open_initial_execution_page = (
            lambda session, _parent_message_id, **_kwargs: (
                InitialExecutionPageOpenResult(
                    status=InitialExecutionPageOpenStatus.REJECTED,
                    session=session,
                )
            )
        )

        started = controller.start_prompt_turn(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertTrue(started)
        self.assertEqual(len(env["start_turn_calls"]), 1)
        self.assertEqual(env["scheduled_watchdogs"], [("ou_user", "c1")])
        self.assertEqual(env["replies"], [])
        self.assertTrue(env["state"]["running"])
        self.assertEqual(
            env["state"]["execution_pages"].current_message_id,
            "",
        )
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_operation_admissions"][0],
            {
                "token": token,
                "binding": env["binding"],
                "thread_id": "thread-created",
                "reason": "feishu_prompt_claimed",
                "operation_kind": "prompt",
            },
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [],
        )
        self.assertEqual(env["root_start_identity_waits"], [token])

    def test_execution_card_claim_exception_settles_before_turn_start(self) -> None:
        env = self._make_controller()

        def _raise_claim(_message_id: str) -> str:
            raise RuntimeError("claim failed")

        env["controller"]._claim_reserved_execution_card = _raise_claim

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        self.assertEqual(result.reason_code, "execution_card_send_failed")
        self.assertEqual(env["start_turn_calls"], [])
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_card_send_failed")],
        )
        self.assertEqual(env["root_operation_state"], {})

    def test_reserved_execution_page_open_exception_settles_before_turn_start(
        self,
    ) -> None:
        env = self._make_controller()
        env["reserved_cards"]["msg-1"] = "reserved-card"

        def _raise_open(*_args, **_kwargs):
            raise RuntimeError("patch failed")

        env["controller"]._open_initial_execution_page = _raise_open

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        self.assertEqual(result.reason_code, "execution_card_send_failed")
        self.assertEqual(env["start_turn_calls"], [])
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_card_send_failed")],
        )
        self.assertEqual(env["root_operation_state"], {})

    def test_reserved_execution_page_rejection_does_not_block_turn_start(self) -> None:
        env = self._make_controller()
        env["reserved_cards"]["msg-1"] = "reserved-card"

        env["controller"]._open_initial_execution_page = (
            lambda session, _parent_message_id, **_kwargs: (
                InitialExecutionPageOpenResult(
                    status=InitialExecutionPageOpenStatus.REJECTED,
                    session=session,
                )
            )
        )

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertTrue(result.started)
        self.assertEqual(len(env["start_turn_calls"]), 1)
        self.assertTrue(env["state"]["running"])
        self.assertEqual(env["state"]["execution_pages"].pages, ())
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [],
        )
        self.assertEqual(env["root_start_identity_waits"], [token])

    def test_execution_card_send_exception_settles_before_turn_start(self) -> None:
        env = self._make_controller()

        def _raise_send(*_args, **_kwargs):
            raise RuntimeError("send failed")

        env["controller"]._open_initial_execution_page = _raise_send

        result = env["controller"].start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        self.assertEqual(result.reason_code, "execution_card_send_failed")
        self.assertEqual(env["start_turn_calls"], [])
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_card_send_failed")],
        )
        self.assertEqual(env["root_operation_state"], {})

    def test_confirmed_turn_start_failure_discards_the_pre_start_operation_claim(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")

        def _rejected_start(**kwargs):
            env["start_turn_calls"].append(dict(kwargs))
            raise RuntimeError("rejected")

        controller._start_turn = _rejected_start
        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_operation_admissions"][0]["thread_id"],
            "thread-1",
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_start_failed")],
        )
        with env["lock"]:
            owner = env["binding_runtime"].interaction_owner_snapshot_locked("thread-1")
        self.assertEqual(owner["kind"], "none")

    def test_unknown_turn_start_failure_keeps_the_pre_start_operation_claim_closed(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")

        def _unknown_start(**kwargs):
            env["start_turn_calls"].append(dict(kwargs))
            raise RuntimeError("unknown")

        controller._start_turn = _unknown_start
        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(result.started)
        self.assertEqual(result.disposition, "blocked_unsettled")
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_operation_unknowns"],
            [(token, "feishu_prompt_start_failed_outcome_unknown")],
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("unknown", "feishu_prompt_start_failed_outcome_unknown")],
        )
        with env["lock"]:
            owner = env["binding_runtime"].interaction_owner_snapshot_locked("thread-1")
        self.assertEqual(owner["kind"], "feishu")

    def test_detached_resume_then_known_turn_rejection_keeps_local_submission_pending(
        self,
    ) -> None:
        """An active goal may have started between resume ACK and turn/start.

        A known rejection of the explicit prompt is not proof that the resumed
        root is idle.  Keep this process's exact submission pending until a
        later lifecycle event supplies that proof; no cross-restart writer is
        created.
        """

        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        def _rejected_start(**kwargs):
            env["start_turn_calls"].append(dict(kwargs))
            raise RuntimeError("already running")

        controller._start_turn = _rejected_start

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(result.started)
        self.assertEqual(result.reason_code, "turn_start_failed")
        self.assertIn("未释放操作所有权", result.reason_text)
        self.assertEqual(
            [call["thread_id"] for call in env["resume_calls"]], ["thread-1"]
        )
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_continuation_arms"],
            [(token, "thread-1", "feishu_prompt_resume_prestart")],
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("continuing", "")],
        )
        self.assertEqual(env["root_start_identity_waits"], [])
        self.assertEqual(env["root_operation_unknowns"], [])
        self.assertEqual(len(env["degraded"]), 1)
        self.assertIn(
            "uncertain resume followed by turn/start rejection",
            env["degraded"][0][2],
        )
        with env["lock"]:
            owner = env["binding_runtime"].interaction_owner_snapshot_locked("thread-1")
        self.assertEqual(owner["kind"], "feishu")

    def test_detached_safe_goal_resume_does_not_create_a_sticky_prestart_fence(
        self,
    ) -> None:
        """A reviewed non-continuing/no-goal resume needs no autonomous fence."""

        env = self._make_controller(continuation_may_autostart=False)
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertTrue(result.started)
        self.assertEqual(
            [call["thread_id"] for call in env["resume_calls"]], ["thread-1"]
        )
        self.assertFalse(env["resume_calls"][0]["retain_on_local_failure"])
        token = self._admitted_root_token(env)
        self.assertEqual(env["root_continuation_arms"], [])
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [],
        )
        self.assertEqual(env["root_start_identity_waits"], [token])

    def test_detached_resume_known_rejection_settles_its_exact_token(self) -> None:
        """A failed resume settles only the typed admission that armed it."""

        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        def _rejected_resume(*_args, **_kwargs):
            raise RuntimeError("resume rejected")

        controller._reattach_bound_thread = _rejected_resume

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_continuation_arms"],
            [(token, "thread-1", "feishu_prompt_resume_prestart")],
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_resume_failed")],
        )

    def test_detached_resume_ack_settlement_error_never_clears_its_fence(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")
        acknowledged = self._resume_settlement_error(recovery_required=False)

        def _ack_then_fail_settlement(*_args, **_kwargs):
            raise acknowledged

        controller._reattach_bound_thread = _ack_then_fail_settlement

        result = controller.start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("continuing", "")],
        )
        self.assertEqual(len(env["root_continuation_receipts"]), 1)
        self.assertIsInstance(
            env["root_continuation_receipts"][0],
            FeishuRootContinuationToken,
        )
        self.assertEqual(env["root_operation_unknowns"], [])

    def test_safe_detached_resume_ack_settlement_error_is_a_known_mutation(
        self,
    ) -> None:
        env = self._make_controller(continuation_may_autostart=False)
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")
        acknowledged = self._resume_settlement_error(recovery_required=False)

        def _ack_then_fail_settlement(*_args, **_kwargs):
            raise acknowledged

        controller._reattach_bound_thread = _ack_then_fail_settlement

        result = controller.start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(env["root_continuation_receipts"], [])
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_mutation", "feishu_prompt_resume_failed")],
        )
        self.assertEqual(env["root_operation_unknowns"], [])

    def test_fallback_resume_ack_local_commit_error_never_clears_its_fence(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        env["start_turn_behavior"]["value"] = RuntimeError("thread not found")
        acknowledged = self._resume_local_commit_error(recovery_required=False)

        def _ack_then_fail_local_commit(*_args, **_kwargs):
            raise acknowledged

        controller._reattach_bound_thread = _ack_then_fail_local_commit

        result = controller.start_prompt_turn_result(
            "ou_user",
            "c1",
            "hello",
            message_id="msg-1",
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("continuing", "")],
        )
        self.assertEqual(len(env["root_continuation_receipts"]), 1)
        self.assertIsInstance(
            env["root_continuation_receipts"][0],
            FeishuRootContinuationToken,
        )
        self.assertEqual(env["root_operation_unknowns"], [])

    def test_fallback_resume_known_rejection_settles_its_exact_token(self) -> None:
        """The not-loaded fallback resume uses the same typed admission."""

        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        env["start_turn_behavior"]["value"] = RuntimeError("thread not found")

        def _rejected_resume(*_args, **_kwargs):
            raise RuntimeError("resume rejected")

        controller._reattach_bound_thread = _rejected_resume

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_continuation_arms"],
            [(token, "thread-1", "feishu_prompt_fallback_resume_prestart")],
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [("known_failure", "feishu_prompt_fallback_resume_failed")],
        )

    def test_detached_resume_then_unknown_turn_outcome_marks_durable_orphan_fence(
        self,
    ) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="detached")

        def _unknown_start(**kwargs):
            env["start_turn_calls"].append(dict(kwargs))
            raise RuntimeError("unknown")

        controller._start_turn = _unknown_start

        result = controller.start_prompt_turn_result(
            "ou_user", "c1", "hello", message_id="msg-1"
        )

        self.assertFalse(result.started)
        token = self._admitted_root_token(env)
        self.assertEqual(
            env["root_continuation_arms"],
            [(token, "thread-1", "feishu_prompt_resume_prestart")],
        )
        self.assertEqual(
            env["root_operation_unknowns"],
            [(token, "feishu_prompt_start_after_fenced_resume_outcome_unknown")],
        )
        self.assertEqual(
            self._root_outcomes_for(env, token),
            [
                (
                    "unknown",
                    "feishu_prompt_start_after_fenced_resume_outcome_unknown",
                )
            ],
        )
        with env["lock"]:
            owner = env["binding_runtime"].interaction_owner_snapshot_locked("thread-1")
        self.assertEqual(owner["kind"], "feishu")


if __name__ == "__main__":
    unittest.main()
