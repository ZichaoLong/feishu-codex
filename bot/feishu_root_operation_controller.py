"""RuntimeLoop-owned Feishu submission and active-turn ownership.

``InteractionLeaseStore`` is the only cross-frontend writer authority. This
controller owns short-lived process-local facts around one outbound
submission: its opaque admission token, an optional resume-continuation
receipt, and an unknown start outcome waiting for lifecycle reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from bot.binding_runtime_contract import (
    BindingOwnerLossCommand,
    BindingOwnerLossSettlementReceipt,
    BindingOwnerRevisionReceipt,
)
from bot.codex_protocol.client import CodexRpcPreSendError
from bot.feishu_root_operation_contract import (
    ChatBindingKey,
    FeishuPromptInterruptCandidateClaim,
    FeishuRootBackendEpochRetirementReceipt,
    FeishuRootContinuationToken,
    FeishuRootOperationPoisoned,
    FeishuRootOperationPorts,
    FeishuRootOperationRetentionError,
    FeishuRootOperationSnapshot,
    FeishuRootOperationToken,
    FeishuRootOperationTokenError,
)
from bot.runtime_loop import RuntimeContextGuard
from bot.runtime_state import is_confirmed_inactive_backend_thread_status
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseAcquireResult,
    InteractionLeaseHolder,
)


@dataclass(slots=True)
class _AdmissionState:
    token: FeishuRootOperationToken
    binding: ChatBindingKey
    root_thread_id: str
    holder: InteractionLeaseHolder
    interaction_lease: InteractionLeaseAcquireResult
    admission_id: int
    operation_kind: str
    awaiting_start_identity: bool = False
    outcome_unknown_reason: str = ""
    interrupt_candidate_installed: bool = False
    interrupt_candidate_id: str = ""
    interrupt_candidate_claim: FeishuPromptInterruptCandidateClaim | None = None


@dataclass(frozen=True, slots=True)
class _ContinuationState:
    receipt: FeishuRootContinuationToken
    root_thread_id: str
    generation: int
    origin_admission_id: int


@dataclass(frozen=True, slots=True)
class _OwnerLossReservation:
    command: BindingOwnerLossCommand
    transaction_nonce: int


class FeishuRootOperationController:
    """Own exact Feishu submission tokens without inventing a root lifecycle."""

    _issuer_ids = count(1)

    def __init__(
        self,
        *,
        ports: FeishuRootOperationPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        required = tuple(getattr(ports, name) for name in ports.__dataclass_fields__)
        if any(not callable(capability) for capability in required):
            raise TypeError("Feishu root-operation owner 的 capability 必须全部可调用。")
        if not callable(runtime_context_guard):
            raise TypeError("Feishu root-operation owner 缺少 RuntimeLoop context guard。")
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._issuer_nonce = next(self._issuer_ids)
        self._next_token_nonce = 0
        self._next_admission_id = 0
        self._next_continuation_token_nonce = 0
        self._next_interrupt_candidate_claim_nonce = 0
        self._next_continuation_generation_by_root: dict[str, int] = {}
        self._next_owner_loss_transaction_nonce = 0
        self._admissions_by_nonce: dict[int, _AdmissionState] = {}
        self._continuations_by_nonce: dict[int, _ContinuationState] = {}
        self._pending_admission_ids_by_root: dict[str, set[int]] = {}
        self._local_holder_by_root: dict[str, InteractionLeaseHolder] = {}
        self._owner_loss_by_owner: dict[
            BindingOwnerRevisionReceipt, _OwnerLossReservation
        ] = {}

    def admit(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
        *,
        chat_id: str,
        message_id: str = "",
        reason: str,
        operation_kind: str = "mutation",
    ) -> FeishuRootOperationToken:
        """Acquire one exact submission lease before an upstream mutation."""

        del reason
        self._runtime_context_guard()
        root_id = self._normalize_root(root_thread_id)
        normalized_kind = str(operation_kind or "mutation").strip()
        if normalized_kind not in {"mutation", "prompt", "compact"}:
            raise ValueError("未知的 Feishu root operation kind。")
        if self._unknown_admissions(root_id):
            raise FeishuRootOperationPoisoned(
                "上一笔 Feishu submission 的上游结果仍未知，正在等待生命周期对账。"
            )
        if self._pending_admission_ids_by_root.get(root_id):
            raise FeishuRootOperationRetentionError(
                "该 thread 已有一笔 Feishu submission 正在发送。"
            )
        # A persisted binding and a routing cache are not mutation authority.
        # Re-read every exact target before a legacy ThreadSpawn binding can
        # acquire a writer lease.
        self._ports.verify_direct_thread_target(root_id)
        admission = self._ports.prompt_write_admission(
            binding,
            str(chat_id or "").strip(),
            root_id,
            str(message_id or "").strip(),
        )
        if not admission.allowed:
            raise PermissionError(admission.reason_text)

        lease_result = self._ports.acquire_interaction_lease(binding, root_id)
        if not lease_result.granted or lease_result.lease is None:
            raise PermissionError("当前飞书会话不拥有该 thread 的 interaction lease。")
        holder = self._ports.holder_for_binding(binding)
        exact_lease = lease_result.lease
        try:
            if (
                exact_lease.thread_id != root_id
                or not exact_lease.holder.same_holder(holder)
            ):
                raise FeishuRootOperationRetentionError(
                    "Focus 未取得与 Feishu binding 匹配的 exact submission lease。"
                )
            if exact_lease.turn_id:
                raise PermissionError("当前 main turn 尚未完成，不能启动下一笔 turn。")
            local_holder = self._local_holder_by_root.get(root_id)
            if local_holder is not None and not local_holder.same_holder(holder):
                raise FeishuRootOperationRetentionError(
                    "同一 thread 的 process-local Feishu holder 发生冲突。"
                )
        except Exception:
            if lease_result.acquired:
                self._release_exact_interaction_lease(exact_lease)
            raise

        self._next_token_nonce += 1
        self._next_admission_id += 1
        token = FeishuRootOperationToken(
            self._issuer_nonce,
            self._next_token_nonce,
        )
        state = _AdmissionState(
            token=token,
            binding=binding,
            root_thread_id=root_id,
            holder=holder,
            interaction_lease=lease_result,
            admission_id=self._next_admission_id,
            operation_kind=normalized_kind,
        )
        self._admissions_by_nonce[token._token_nonce] = state
        self._pending_admission_ids_by_root[root_id] = {state.admission_id}
        self._local_holder_by_root[root_id] = holder
        return token

    def arm_continuation(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str = "feishu_continuation_prestart",
    ) -> FeishuRootContinuationToken:
        """Register process-local uncertainty around a resume that may autostart."""

        del reason
        self._runtime_context_guard()
        state = self._require_token(token)
        generation = (
            self._next_continuation_generation_by_root.get(state.root_thread_id, 0)
            + 1
        )
        self._next_continuation_generation_by_root[state.root_thread_id] = generation
        self._next_continuation_token_nonce += 1
        receipt = FeishuRootContinuationToken(
            self._issuer_nonce,
            self._next_continuation_token_nonce,
        )
        self._continuations_by_nonce[receipt._token_nonce] = _ContinuationState(
            receipt=receipt,
            root_thread_id=state.root_thread_id,
            generation=generation,
            origin_admission_id=state.admission_id,
        )
        return receipt

    def commit_resume_owner(self, token: FeishuRootOperationToken) -> None:
        """Verify that the exact submission lease survived a local resume commit."""

        self._runtime_context_guard()
        state = self._require_token(token)
        expected = state.interaction_lease.lease
        if expected is None or self._ports.lookup_interaction_lease(
            state.root_thread_id
        ) != expected:
            raise FeishuRootOperationRetentionError(
                "Feishu resume local commit 时 submission lease 已失效。"
            )

    def settle_continuation_failure(
        self,
        receipt: FeishuRootContinuationToken,
        *,
        reason: str,
    ) -> None:
        """Consume one exact process-local continuation receipt."""

        del reason
        self._runtime_context_guard()
        _state, continuation = self._require_continuation_receipt(receipt)
        self._continuations_by_nonce.pop(continuation.receipt._token_nonce, None)
        self._prune_local_holder(continuation.root_thread_id)

    def await_start_identity(self, token: FeishuRootOperationToken) -> None:
        """Keep an accepted turn-producing submission until its exact id arrives."""

        self._runtime_context_guard()
        state = self._require_token(token)
        state.awaiting_start_identity = True

    def accept_prompt_start(
        self,
        token: FeishuRootOperationToken,
        response_turn_id: str,
    ) -> None:
        """Retain one accepted prompt id only as a one-shot interrupt candidate."""

        self._runtime_context_guard()
        state = self._require_token(token)
        normalized_turn_id = str(response_turn_id or "").strip()
        if state.operation_kind != "prompt":
            raise FeishuRootOperationTokenError(
                "只有 exact prompt admission 可以保留 interrupt candidate。"
            )
        if not normalized_turn_id:
            raise FeishuRootOperationTokenError(
                "Feishu prompt interrupt candidate id 不能为空。"
            )
        if state.interrupt_candidate_installed:
            raise FeishuRootOperationTokenError(
                "Feishu prompt admission 已安装过 interrupt candidate。"
            )
        state.interrupt_candidate_installed = True
        state.awaiting_start_identity = True
        state.interrupt_candidate_id = normalized_turn_id

    def claim_prompt_interrupt_candidate(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
    ) -> FeishuPromptInterruptCandidateClaim | None:
        """Consume one exact accepted prompt id before an interrupt attempt."""

        self._runtime_context_guard()
        root_id = self._normalize_root(root_thread_id)
        candidates = [
            state
            for state in self._admissions_by_nonce.values()
            if state.root_thread_id == root_id
            and state.binding == binding
            and state.operation_kind == "prompt"
            and state.awaiting_start_identity
            and bool(state.interrupt_candidate_id)
            and state.interrupt_candidate_claim is None
        ]
        if len(candidates) > 1:
            raise FeishuRootOperationRetentionError(
                "同一 binding/root 存在多个 prompt interrupt candidate。"
            )
        if not candidates:
            return None
        state = candidates[0]
        self._next_interrupt_candidate_claim_nonce += 1
        claim = FeishuPromptInterruptCandidateClaim(
            turn_id=state.interrupt_candidate_id,
            _issuer_nonce=self._issuer_nonce,
            _token=state.token,
            _claim_nonce=self._next_interrupt_candidate_claim_nonce,
        )
        state.interrupt_candidate_id = ""
        state.interrupt_candidate_claim = claim
        return claim

    def consume_prompt_interrupt_candidate(
        self,
        claim: FeishuPromptInterruptCandidateClaim,
    ) -> bool:
        """Permanently consume one claimed candidate after a non-pre-send outcome."""

        state = self._require_prompt_interrupt_candidate_claim(claim)
        if state is None:
            return False
        state.interrupt_candidate_claim = None
        return True

    def restore_prompt_interrupt_candidate_after_pre_send(
        self,
        claim: FeishuPromptInterruptCandidateClaim,
        *,
        error: CodexRpcPreSendError,
    ) -> bool:
        """Restore one exact claim only from a typed pre-send failure."""

        self._runtime_context_guard()
        if not isinstance(error, CodexRpcPreSendError):
            raise FeishuRootOperationTokenError(
                "只有 typed CodexRpcPreSendError 可以恢复 interrupt candidate。"
            )
        if error.method != "turn/interrupt":
            raise FeishuRootOperationTokenError(
                "只有 turn/interrupt pre-send failure 可以恢复 interrupt candidate。"
            )
        state = self._require_prompt_interrupt_candidate_claim(claim)
        if state is None:
            return False
        state.interrupt_candidate_claim = None
        if (
            state.operation_kind != "prompt"
            or not state.awaiting_start_identity
            or state.interrupt_candidate_id
        ):
            return False
        state.interrupt_candidate_id = claim.turn_id
        return True

    def _require_prompt_interrupt_candidate_claim(
        self,
        claim: FeishuPromptInterruptCandidateClaim,
    ) -> _AdmissionState | None:
        self._runtime_context_guard()
        if not isinstance(claim, FeishuPromptInterruptCandidateClaim):
            raise FeishuRootOperationTokenError(
                "无效的 Feishu prompt interrupt candidate claim。"
            )
        try:
            state = self._require_token(claim._token)
        except FeishuRootOperationTokenError:
            return None
        if (
            claim._issuer_nonce != self._issuer_nonce
            or state.interrupt_candidate_claim is not claim
        ):
            raise FeishuRootOperationTokenError(
                "Feishu prompt interrupt candidate claim 已结算、伪造或过期。"
            )
        return state

    def acknowledge_async_start(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
        turn_id: str,
    ) -> bool:
        """Bind an accepted compact submission to its exact active turn."""

        self._runtime_context_guard()
        state = self._find_awaiting_compact(binding, root_thread_id)
        if state is None:
            return False
        self._activate_main_turn(state, turn_id)
        self._discard_continuations_for_admission(state)
        self._finish(state)
        return True

    def mark_awaiting_start_outcome_unknown(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
        *,
        reason: str,
    ) -> None:
        """Keep one compact submission local when its turn identity timed out."""

        self._runtime_context_guard()
        state = self._find_awaiting_compact(binding, root_thread_id)
        if state is None:
            raise FeishuRootOperationTokenError(
                "找不到等待 turn identity 的 exact compact admission。"
            )
        self.mark_outcome_unknown(state.token, reason=reason)

    def settle_owner_loss(
        self,
        command: BindingOwnerLossCommand,
    ) -> BindingOwnerLossSettlementReceipt:
        """Settle one binding revision without creating a durable writer state."""

        self._runtime_context_guard()
        if not isinstance(command, BindingOwnerLossCommand):
            raise FeishuRootOperationRetentionError(
                "Focus 收到无效的 Feishu owner-loss command。"
            )
        try:
            self._ports.validate_binding_owner_receipt(command.owner)
        except Exception as exc:
            raise FeishuRootOperationRetentionError(
                "Feishu owner-loss command 已伪造、替换或过期。"
            ) from exc
        reservation = self._owner_loss_by_owner.get(command.owner)
        if reservation is None:
            self._next_owner_loss_transaction_nonce += 1
            reservation = _OwnerLossReservation(
                command=command,
                transaction_nonce=self._next_owner_loss_transaction_nonce,
            )
            self._owner_loss_by_owner[command.owner] = reservation
        elif reservation.command != command:
            raise FeishuRootOperationRetentionError(
                "同一 binding revision 已由另一 owner-loss command 保留。"
            )

        thread_id = str(command.thread_id or "").strip()
        if thread_id:
            root_id = thread_id
            holder = self._ports.holder_for_binding(command.binding)
            lease = self._ports.lookup_interaction_lease(root_id)
            matching_admissions = tuple(
                state
                for state in self._admissions_by_nonce.values()
                if state.root_thread_id == root_id
                and state.holder.same_holder(holder)
            )
            for state in matching_admissions:
                state.interrupt_candidate_id = ""
                state.interrupt_candidate_claim = None
            # A live main turn remains the cross-frontend owner until its
            # matching completion.  A blank accepted/unknown submission may
            # also have produced an upstream effect, so binding loss cannot
            # retire its exact lease or local lifecycle correlation.  Only a
            # matching admission which has not crossed that boundary belongs
            # solely to the disappearing binding revision.
            retain_for_lifecycle = any(
                state.awaiting_start_identity or state.outcome_unknown_reason
                for state in matching_admissions
            )
            if not retain_for_lifecycle:
                if (
                    lease is not None
                    and not lease.turn_id
                    and lease.holder.same_holder(holder)
                    and (
                        not matching_admissions
                        or any(
                            state.interaction_lease.lease == lease
                            for state in matching_admissions
                        )
                    )
                ):
                    self._release_exact_interaction_lease(lease)
                for state in matching_admissions:
                    self._discard_continuations_for_admission(state)
                    self._finish(state)
        return BindingOwnerLossSettlementReceipt(
            command=command,
            _settler_nonce=self._issuer_nonce,
            _transaction_nonce=reservation.transaction_nonce,
        )

    def reconcile_notification(self, method: str, params: dict) -> bool:
        """Reconcile only exact submission or main-turn lifecycle evidence."""

        self._runtime_context_guard()
        thread_id = str(params.get("threadId", "") or "").strip()
        if not thread_id:
            return False
        if method == "turn/started":
            turn_id = self._turn_id_from_notification(method, params)
            return bool(turn_id and self._activate_waiting_turn(thread_id, turn_id))
        if method == "item/started":
            turn_id = self._turn_id_from_notification(method, params)
            return bool(
                turn_id
                and self._activate_waiting_turn(
                    thread_id,
                    turn_id,
                    required_operation_kind="compact",
                )
            )
        if method == "turn/completed":
            turn_id = self._turn_id_from_notification(method, params)
            if not turn_id:
                return False
            lease = self._ports.lookup_interaction_lease(thread_id)
            if lease is None or lease.turn_id != turn_id:
                return False
            return self._release_exact_interaction_lease(lease)
        if method == "thread/closed":
            lease = self._ports.lookup_interaction_lease(thread_id)
            released = bool(
                lease is not None
                and self._release_exact_interaction_lease(lease)
            )
            had_local = bool(self._pending_admission_ids_by_root.get(thread_id))
            self._clear_root_local(thread_id)
            return released or had_local
        if method == "thread/status/changed":
            return self.reconcile_terminal(thread_id)
        return False

    def reconcile_terminal(
        self,
        root_thread_id: str,
    ) -> bool:
        """Release an accepted blank submission after exact root inactivity."""

        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        awaiting = self._terminal_reconcilable_admissions(root_id)
        if not root_id or not awaiting:
            return False
        try:
            status = self._ports.read_root_status(root_id)
        except Exception:
            return False
        if not is_confirmed_inactive_backend_thread_status(status):
            return False
        expected = awaiting[0].interaction_lease.lease
        current = self._ports.lookup_interaction_lease(root_id)
        if (
            expected is None
            or current != expected
            or current.turn_id
            or any(state.interaction_lease.lease != expected for state in awaiting)
        ):
            return False
        if not self._release_exact_interaction_lease(expected):
            return False
        for state in tuple(awaiting):
            self._discard_continuations_for_admission(state)
            self._finish(state)
        return True

    def settle_known_failure(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None:
        """Release a submission after a proven no-effect result."""

        del reason
        self._runtime_context_guard()
        state = self._require_token(token)
        self._discard_continuations_for_admission(state)
        self._finish(state)
        self._release_submission_if_idle(state)

    def settle_known_mutation(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None:
        """Finish a known non-turn mutation without extending writer ownership."""

        self.settle_known_failure(token, reason=reason)

    def settle_noncontinuing(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None:
        """Settle a resume only when the root is authoritatively inactive."""

        self._runtime_context_guard()
        state = self._require_token(token)
        try:
            status = self._ports.read_root_status(state.root_thread_id)
        except Exception as exc:
            self.mark_outcome_unknown(token, reason=reason)
            raise FeishuRootOperationRetentionError(
                "Focus 无法确认该 mutation 是否启动了 main turn；"
                "正在等待生命周期对账。"
            ) from exc
        if not is_confirmed_inactive_backend_thread_status(status):
            self.mark_outcome_unknown(token, reason=reason)
            raise FeishuRootOperationRetentionError(
                "resume 后 root 仍可能 active；正在等待 exact turn identity。"
            )
        self.settle_known_mutation(token, reason=reason)

    def acknowledge_continuing(
        self,
        token: FeishuRootOperationToken,
        *,
        turn_id: str = "",
    ) -> None:
        """Commit a known turn, or retain a local submission uncertainty."""

        self._runtime_context_guard()
        state = self._require_token(token)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            self.mark_outcome_unknown(
                token,
                reason="accepted_continuation_awaiting_turn_identity",
            )
            return
        self._activate_main_turn(state, normalized_turn_id)
        self._discard_continuations_for_admission(state)
        self._finish(state)

    def mark_outcome_unknown(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None:
        """Keep only this process's exact submission uncertainty."""

        self._runtime_context_guard()
        state = self._require_token(token)
        state.outcome_unknown_reason = str(
            reason or "feishu_submission_outcome_unknown"
        ).strip()

    def retire_backend_epoch_after_stop(
        self,
    ) -> FeishuRootBackendEpochRetirementReceipt:
        """Retire only this owner's facts after confirmed backend stop.

        The shared interaction lease remains untouched. Its exact current-
        process retirement belongs to ``InteractionLeaseStore`` and is
        ordered separately by the backend-reset transaction.
        """

        self._runtime_context_guard()
        root_thread_ids = tuple(
            sorted(
                {
                    *(
                        state.root_thread_id
                        for state in self._admissions_by_nonce.values()
                    ),
                    *(
                        continuation.root_thread_id
                        for continuation in self._continuations_by_nonce.values()
                    ),
                    *self._pending_admission_ids_by_root,
                    *self._local_holder_by_root,
                }
            )
        )
        receipt = FeishuRootBackendEpochRetirementReceipt(
            root_thread_ids=root_thread_ids,
            admission_count=len(self._admissions_by_nonce),
            continuation_count=len(self._continuations_by_nonce),
            interrupt_candidate_count=sum(
                bool(
                    state.interrupt_candidate_id
                    or state.interrupt_candidate_claim is not None
                )
                for state in self._admissions_by_nonce.values()
            ),
        )
        self._admissions_by_nonce.clear()
        self._continuations_by_nonce.clear()
        self._pending_admission_ids_by_root.clear()
        self._local_holder_by_root.clear()
        return receipt

    def snapshot(self, root_thread_id: str) -> FeishuRootOperationSnapshot:
        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        unknown = self._unknown_admissions(root_id)
        local_holder = self._local_holder_by_root.get(root_id)
        return FeishuRootOperationSnapshot(
            root_thread_id=root_id,
            pending_admission_count=len(
                self._pending_admission_ids_by_root.get(root_id, set())
            ),
            continuation_generations=tuple(
                sorted(self._continuation_generations(root_id))
            ),
            submission_outcome_unknown=bool(unknown),
            submission_unknown_reason="; ".join(
                state.outcome_unknown_reason for state in unknown
            ),
            local_holder_kind=(local_holder.kind if local_holder is not None else ""),
            local_holder_id=(
                local_holder.holder_id if local_holder is not None else ""
            ),
        )

    def _require_token(
        self,
        token: FeishuRootOperationToken,
    ) -> _AdmissionState:
        if not isinstance(token, FeishuRootOperationToken):
            raise FeishuRootOperationTokenError("无效的 Feishu operation token。")
        state = self._admissions_by_nonce.get(token._token_nonce)
        if (
            token._issuer_nonce != self._issuer_nonce
            or state is None
            or state.token is not token
        ):
            raise FeishuRootOperationTokenError(
                "Feishu operation token 已结算、伪造或属于另一 controller。"
            )
        return state

    def _require_continuation_receipt(
        self,
        receipt: FeishuRootContinuationToken,
    ) -> tuple[_AdmissionState, _ContinuationState]:
        if not isinstance(receipt, FeishuRootContinuationToken):
            raise FeishuRootOperationTokenError(
                "无效的 Feishu continuation receipt。"
            )
        continuation = self._continuations_by_nonce.get(receipt._token_nonce)
        if (
            receipt._issuer_nonce != self._issuer_nonce
            or continuation is None
            or continuation.receipt is not receipt
        ):
            raise FeishuRootOperationTokenError(
                "Feishu continuation receipt 已结算、伪造或属于另一 controller。"
            )
        state = next(
            (
                candidate
                for candidate in self._admissions_by_nonce.values()
                if candidate.admission_id == continuation.origin_admission_id
                and candidate.root_thread_id == continuation.root_thread_id
            ),
            None,
        )
        if state is None:
            raise FeishuRootOperationTokenError(
                "Feishu continuation receipt 的 submission 已结束。"
            )
        return self._require_token(state.token), continuation

    def _find_awaiting_compact(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
    ) -> _AdmissionState | None:
        root_id = self._normalize_root(root_thread_id)
        holder = self._ports.holder_for_binding(binding)
        candidates = [
            state
            for state in self._admissions_by_nonce.values()
            if state.root_thread_id == root_id
            and state.operation_kind == "compact"
            and state.awaiting_start_identity
            and state.holder.same_holder(holder)
        ]
        if len(candidates) > 1:
            raise FeishuRootOperationRetentionError(
                "同一 thread 存在多个等待 turn identity 的 compact submission。"
            )
        return candidates[0] if candidates else None

    def _activate_main_turn(
        self,
        state: _AdmissionState,
        turn_id: str,
    ) -> InteractionLease:
        expected = state.interaction_lease.lease
        if expected is None:
            raise FeishuRootOperationRetentionError(
                "Feishu main turn 缺少 submission lease。"
            )
        active = self._ports.activate_interaction_turn(expected, turn_id)
        if active is None:
            raise FeishuRootOperationRetentionError(
                "Feishu submission lease 已被替换或绑定到另一 turn。"
            )
        return active

    def _activate_waiting_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        required_operation_kind: str = "",
    ) -> bool:
        candidates = [
            state
            for state in self._admissions_by_nonce.values()
            if state.root_thread_id == thread_id
            and (state.awaiting_start_identity or state.outcome_unknown_reason)
            and (
                not required_operation_kind
                or state.operation_kind == required_operation_kind
            )
        ]
        if not candidates:
            return False
        expected = candidates[0].interaction_lease.lease
        if expected is None or any(
            state.interaction_lease.lease != expected for state in candidates
        ):
            return False
        active = self._ports.activate_interaction_turn(expected, turn_id)
        if active is None:
            return False
        for state in tuple(candidates):
            self._discard_continuations_for_admission(state)
            self._finish(state)
        return True

    @staticmethod
    def _turn_id_from_notification(method: str, params: dict) -> str:
        direct = str(params.get("turnId", "") or "").strip()
        if direct:
            return direct
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            return str(turn.get("id", "") or "").strip()
        if method == "item/started":
            item = params.get("item") if isinstance(params.get("item"), dict) else {}
            if str(item.get("type", "") or "").strip() == "contextCompaction":
                return str(params.get("turnId", "") or "").strip()
        return ""

    def _release_submission_if_idle(self, state: _AdmissionState) -> bool:
        if self._pending_admission_ids_by_root.get(state.root_thread_id):
            return False
        expected = state.interaction_lease.lease
        if expected is None or expected.turn_id:
            return False
        current = self._ports.lookup_interaction_lease(state.root_thread_id)
        if current is None or current != expected or current.turn_id:
            return False
        return self._release_exact_interaction_lease(expected)

    def _release_exact_interaction_lease(
        self,
        expected: InteractionLease,
    ) -> bool:
        if not isinstance(expected, InteractionLease):
            raise FeishuRootOperationRetentionError(
                "interaction lease cleanup 缺少 typed exact generation。"
            )
        current = self._ports.lookup_interaction_lease(expected.thread_id)
        if current is None:
            return False
        if current != expected:
            return False
        try:
            released = self._ports.release_exact_interaction_lease(expected)
        except Exception as exc:
            remaining = self._ports.lookup_interaction_lease(expected.thread_id)
            if remaining is None:
                return True
            if remaining != expected:
                return False
            raise FeishuRootOperationRetentionError(
                "Focus 未能确认 exact interaction lease release。"
            ) from exc
        if released is True:
            return True
        remaining = self._ports.lookup_interaction_lease(expected.thread_id)
        if remaining is None:
            return True
        if remaining != expected:
            return False
        raise FeishuRootOperationRetentionError(
            "Focus 未能确认 exact interaction lease release。"
        )

    def _finish(self, state: _AdmissionState) -> None:
        current = self._admissions_by_nonce.get(state.token._token_nonce)
        if current is not state:
            raise FeishuRootOperationTokenError(
                "Feishu operation admission 已被结算。"
            )
        state.interrupt_candidate_id = ""
        state.interrupt_candidate_claim = None
        self._admissions_by_nonce.pop(state.token._token_nonce, None)
        pending = self._pending_admission_ids_by_root.get(state.root_thread_id)
        if pending is not None:
            pending.discard(state.admission_id)
            if not pending:
                self._pending_admission_ids_by_root.pop(state.root_thread_id, None)
        self._prune_local_holder(state.root_thread_id)

    def _discard_continuations_for_admission(
        self,
        state: _AdmissionState,
    ) -> None:
        for nonce, continuation in tuple(self._continuations_by_nonce.items()):
            if continuation.origin_admission_id == state.admission_id:
                self._continuations_by_nonce.pop(nonce, None)

    def _clear_root_local(self, root_thread_id: str) -> None:
        for state in tuple(self._admissions_by_nonce.values()):
            if state.root_thread_id == root_thread_id:
                self._discard_continuations_for_admission(state)
                self._finish(state)
        for nonce, continuation in tuple(self._continuations_by_nonce.items()):
            if continuation.root_thread_id == root_thread_id:
                self._continuations_by_nonce.pop(nonce, None)
        self._pending_admission_ids_by_root.pop(root_thread_id, None)
        self._local_holder_by_root.pop(root_thread_id, None)

    def _unknown_admissions(self, root_thread_id: str) -> list[_AdmissionState]:
        return [
            state
            for state in self._admissions_by_nonce.values()
            if state.root_thread_id == root_thread_id
            and bool(state.outcome_unknown_reason)
        ]

    def _terminal_reconcilable_admissions(
        self,
        root_thread_id: str,
    ) -> list[_AdmissionState]:
        return [
            state
            for state in self._admissions_by_nonce.values()
            if state.root_thread_id == root_thread_id
            and (
                bool(state.outcome_unknown_reason)
                or (
                    state.awaiting_start_identity
                    and state.operation_kind != "compact"
                )
            )
        ]

    def _continuation_generations(self, root_thread_id: str) -> set[int]:
        return {
            continuation.generation
            for continuation in self._continuations_by_nonce.values()
            if continuation.root_thread_id == root_thread_id
        }

    def _prune_local_holder(self, root_thread_id: str) -> None:
        if not (
            self._pending_admission_ids_by_root.get(root_thread_id)
            or self._continuation_generations(root_thread_id)
        ):
            self._local_holder_by_root.pop(root_thread_id, None)

    @staticmethod
    def _normalize_root(root_thread_id: str) -> str:
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            raise ValueError("Feishu root operation 缺少 thread id。")
        return root_id
