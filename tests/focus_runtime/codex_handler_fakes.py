"""Shared CodexHandler test doubles.

These fakes intentionally model only the adapter and Feishu bot boundaries
used by tests in this package. They are test infrastructure, not runtime
contracts.
"""

from __future__ import annotations

import json
import pathlib

from bot.adapters.base import (
    RuntimeConfigSummary,
    RuntimeModelSummary,
    RuntimeReasoningEffortOption,
    ThreadGoalSummary,
    ThreadItemsPage,
    ThreadResumePage,
    ThreadSearchOccurrencesPage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.card_text_projection import project_interactive_card_text
from bot.codex_protocol.connection import (
    CodexRpcConnectionGenerationMismatchError,
)
from bot.feishu_message_codec import InteractiveMessageReadResult
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.server_request_contract import ServerRequestIdentity
from bot.service_runtime_lifecycle import ServiceRuntimePhase


def _register_handler(handler, bot) -> None:
    handler._service_runtime_lifecycle._set_phase(ServiceRuntimePhase.ASSEMBLED)
    handler.start(bot)


def _capture_reconcile(handler, *args, **kwargs):
    return handler._execution_recovery.capture_terminal_reconcile_target(
        *args,
        **kwargs,
    )


def _run_reconcile(handler, target) -> None:
    handler._execution_recovery.run_terminal_execution_reconcile(target)


def _detach_thread(handler, thread_id: str):
    return handler._runtime_call(handler._runtime_admin.detach_thread, thread_id)


def _flush_execution(handler, *args, **kwargs) -> None:
    handler._execution_output.flush_execution_card(*args, **kwargs)


def _runtime_state(handler, sender_id: str, chat_id: str, message_id: str = ""):
    """Return raw state only for Handler white-box assertions."""

    with handler._lock:
        existing_binding = handler._binding_runtime.existing_chat_binding_key_locked(
            sender_id,
            chat_id,
        )
        if existing_binding is not None:
            existing_state = handler._binding_runtime.resident_runtime_state_locked(
                existing_binding
            )
            if existing_state is None:
                raise AssertionError("existing binding has no resident runtime")
            return existing_state

    def read():
        session = handler._binding_runtime.resolve_session(sender_id, chat_id, message_id)
        with handler._lock:
            state = handler._binding_runtime.resident_runtime_state_locked(
                session.binding
            )
        if state is None:
            raise AssertionError("resolved binding has no resident runtime")
        return state

    if handler._runtime_loop._is_worker_thread():
        return read()
    return handler._runtime_call(read)


def _bind_authoritative_thread(
    handler,
    sender_id: str,
    chat_id: str,
    thread: ThreadSummary,
    *,
    message_id: str = "",
) -> None:
    """Seed the adapter fact, then call the real Feishu session owner command."""

    handler._adapter.thread_snapshots[(thread.thread_id, None)] = ThreadSnapshot(
        summary=thread
    )
    handler._runtime_call(
        handler._feishu_thread_sessions.bind_thread,
        sender_id,
        chat_id,
        thread,
        message_id=message_id,
    )


def _store_pending_interaction(
    handler,
    request_key: str,
    pending: dict,
) -> None:
    """Install one exact Feishu action capability for white-box tests."""

    state = dict(pending)
    state.setdefault(
        "identity",
        ServerRequestIdentity(
            request_id=state.get("rpc_request_id", request_key),
            connection_generation=1,
            method=str(state.get("method", "item/commandExecution/requestApproval")),
            params=dict(state.get("params") or {}),
        ),
    )
    state.setdefault("response_capability", f"test-capability:{request_key}")
    handler._interaction_requests.store_pending_request(request_key, state)


def _store_canonical_pending_interaction(
    handler,
    pending: dict,
) -> str:
    """Install one registry identity and its exact Feishu projection."""

    state = dict(pending)
    identity = ServerRequestIdentity(
        request_id=state["rpc_request_id"],
        connection_generation=1,
        method=str(state.get("method", "item/commandExecution/requestApproval")),
        params=dict(state.get("params") or {}),
    )

    def register_canonical_identity() -> None:
        if handler._server_request_registry.connection_generation == 0:
            handler._server_request_coordinator.activate_connection_epoch(
                identity.connection_generation
            )
        if (
            handler._server_request_registry.connection_generation
            != identity.connection_generation
        ):
            raise AssertionError("canonical pending interaction has a stale epoch")
        registration = handler._server_request_registry.register(identity)
        if registration.outcome != "new" or registration.identity is not identity:
            raise AssertionError(
                "unable to register canonical pending interaction: "
                f"{registration.outcome}"
            )

    handler._runtime_call(register_canonical_identity)
    request_key = identity.request_key
    state["identity"] = identity
    state.setdefault("response_capability", f"test-capability:{request_key}")
    handler._interaction_requests.store_pending_request(request_key, state)
    return request_key


def _bind_pending_interaction_action(
    handler,
    request_key: str,
    action: dict,
) -> dict:
    """Bind a synthetic callback to the capability shown by its test card."""

    pending = handler._interaction_requests.pending_request_snapshot(request_key)
    if pending is None:
        raise AssertionError(f"missing pending interaction: {request_key}")
    identity = pending.get("identity")
    if not isinstance(identity, ServerRequestIdentity):
        raise AssertionError(f"missing pending identity: {request_key}")
    return {
        **action,
        "connection_generation": identity.connection_generation,
        "response_capability": pending["response_capability"],
    }


class _FakeAdapter:
    def __init__(
        self,
        config,
        *,
        on_notification=None,
        on_request=None,
        on_disconnect_ingress=None,
        on_disconnect=None,
        app_server_runtime_store=None,
        issue_outbound_request=None,
        guard_outbound_send=None,
        confirm_outbound_request=None,
    ) -> None:
        self.config = config
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_disconnect_ingress = on_disconnect_ingress
        self.on_disconnect = on_disconnect
        self.app_server_runtime_store = app_server_runtime_store
        self.issue_outbound_request = issue_outbound_request
        self.guard_outbound_send = guard_outbound_send
        self.confirm_outbound_request = confirm_outbound_request
        self.start_calls = 0
        self.stop_calls = 0
        self.connection_generation_value = 1
        self.create_thread_calls: list[dict] = []
        self.resume_thread_calls: list[dict] = []
        self.set_thread_goal_calls: list[dict] = []
        self.update_thread_settings_calls: list[dict] = []
        self.operation_log: list[tuple[str, str, str | None]] = []
        self.start_turn_calls: list[dict] = []
        self.ordinary_start_turn_calls: list[dict] = []
        self.start_turn_results: list[dict | Exception] = []
        self.steer_turn_calls: list[dict] = []
        self.steer_turn_results: list[object | Exception] = []
        self.interrupt_turn_calls: list[dict] = []
        self.respond_calls: list[dict] = []
        self.archive_thread_calls: list[str] = []
        self.unarchive_thread_calls: list[str] = []
        self.delete_thread_calls: list[str] = []
        self.unsubscribe_thread_calls: list[str] = []
        self.compact_thread_calls: list[str] = []
        self.rename_thread_calls: list[tuple[str, str]] = []
        self.read_thread_calls: list[dict] = []
        self.list_thread_turns_calls: list[dict] = []
        self.list_thread_items_calls: list[dict] = []
        self.search_thread_occurrences_calls: list[dict] = []
        self.thread_snapshots: dict[tuple[str, bool | None], ThreadSnapshot | Exception] = {}
        # A persisted cold snapshot is distinct from live runtime residency.
        self.loaded_thread_ids: set[str] = set()
        self.thread_turns: dict[str, list[dict]] = {}
        self.thread_goals: dict[str, ThreadGoalSummary] = {}
        self.models: list[RuntimeModelSummary] = [
            RuntimeModelSummary(
                model="gpt-5.5",
                display_name="gpt-5.5",
                is_default=True,
                default_reasoning_effort="high",
                supported_reasoning_efforts=[
                    RuntimeReasoningEffortOption(reasoning_effort="medium"),
                    RuntimeReasoningEffortOption(reasoning_effort="high"),
                    RuntimeReasoningEffortOption(reasoning_effort="xhigh"),
                    RuntimeReasoningEffortOption(reasoning_effort="ultra"),
                ],
                input_modalities=["text", "image"],
            ),
            RuntimeModelSummary(
                model="gpt-5.4",
                display_name="gpt-5.4",
                default_reasoning_effort="medium",
                supported_reasoning_efforts=[
                    RuntimeReasoningEffortOption(reasoning_effort="low"),
                    RuntimeReasoningEffortOption(reasoning_effort="medium"),
                    RuntimeReasoningEffortOption(reasoning_effort="high"),
                ],
                input_modalities=["text"],
            ),
        ]

    def require_owned_backend_lifecycle(self) -> None:
        return None

    def stop(self) -> None:
        self.stop_calls += 1

    def start(self) -> None:
        self.start_calls += 1

    def rotate_server_request_authority_after_backend_stop(self) -> object:
        return object()

    def current_app_server_url(self) -> str:
        return self.config.app_server_url

    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int:
        del timeout
        del require_existing_connection
        return self.connection_generation_value

    def _require_connection_generation(
        self,
        method: str,
        expected_connection_generation: int | None,
    ) -> None:
        if (
            expected_connection_generation is not None
            and expected_connection_generation != self.connection_generation_value
        ):
            raise CodexRpcConnectionGenerationMismatchError(
                method,
                expected_generation=expected_connection_generation,
                observed_generation=self.connection_generation_value,
            )

    def create_thread(
        self,
        *,
        cwd: str,
        config_overrides: dict | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
    ):
        self.create_thread_calls.append(
            {
                "cwd": cwd,
                "config_overrides": config_overrides,
                "model": model,
                "model_provider": model_provider,
                "approval_policy": approval_policy,
                "permissions_profile_id": permissions_profile_id,
                "sandbox": sandbox,
            }
        )
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd=cwd,
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            effective_model=model or "gpt-5.5",
            effective_reasoning_effort=self.config.reasoning_effort or None,
            effective_approval_policy=approval_policy or self.config.approval_policy,
            effective_permissions_profile_id=(
                permissions_profile_id or self.config.permissions_profile_id
            ),
        )
        # A successful fake create establishes later authoritative reads.
        self.thread_snapshots[(snapshot.summary.thread_id, None)] = snapshot
        self.loaded_thread_ids.add(snapshot.summary.thread_id)
        return snapshot

    def read_thread(
        self,
        thread_id: str,
        include_turns: bool = False,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ):
        del timeout
        del require_existing_connection
        self._require_connection_generation(
            "thread/read",
            expected_connection_generation,
        )
        self.read_thread_calls.append({"thread_id": thread_id, "include_turns": include_turns})
        snapshot = self.thread_snapshots.get((thread_id, include_turns))
        if snapshot is None:
            snapshot = self.thread_snapshots.get((thread_id, None))
        if snapshot is None:
            raise NotImplementedError
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot

    def get_thread_goal(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> ThreadGoalSummary | None:
        self._require_connection_generation(
            "thread/goal/get",
            expected_connection_generation,
        )
        return self.thread_goals.get(thread_id)

    def set_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
    ) -> ThreadGoalSummary:
        self.set_thread_goal_calls.append(
            {
                "thread_id": thread_id,
                "objective": objective,
                "status": status,
                "token_budget": token_budget,
            }
        )
        self.operation_log.append(("set_thread_goal", thread_id, status))
        existing = self.thread_goals.get(thread_id)
        if existing is None:
            if not objective:
                raise ValueError("cannot update goal when no goal exists")
            goal = ThreadGoalSummary(
                thread_id=thread_id,
                objective=objective,
                status=status or "active",
                token_budget=token_budget,
                tokens_used=0,
                time_used_seconds=0,
                created_at=1712476800,
                updated_at=1712476800,
            )
        else:
            goal = ThreadGoalSummary(
                thread_id=thread_id,
                objective=objective or existing.objective,
                status=status or existing.status,
                token_budget=token_budget if token_budget is not None else existing.token_budget,
                tokens_used=existing.tokens_used,
                time_used_seconds=existing.time_used_seconds,
                created_at=existing.created_at,
                updated_at=1712476801,
            )
        self.thread_goals[thread_id] = goal
        return goal

    def clear_thread_goal(self, thread_id: str) -> bool:
        return self.thread_goals.pop(thread_id, None) is not None

    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        return RuntimeConfigSummary(current_model_provider="provider1_api")

    def list_models(self, *, include_hidden: bool = False) -> list[RuntimeModelSummary]:
        if include_hidden:
            return list(self.models)
        return [item for item in self.models if not item.hidden]

    def list_loaded_thread_ids(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> list[str]:
        del timeout
        del require_existing_connection
        self._require_connection_generation(
            "thread/loaded/list",
            expected_connection_generation,
        )
        return sorted(self.loaded_thread_ids)

    def list_loaded_thread_ids_for_control(self, *, timeout: float) -> list[str]:
        del timeout
        return sorted(self.loaded_thread_ids)

    def resume_thread(
        self,
        thread_id: str,
        *,
        config_overrides: dict | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
    ):
        self._require_connection_generation(
            "thread/resume",
            expected_connection_generation,
        )
        self.resume_thread_calls.append({
            "thread_id": thread_id,
            "config_overrides": config_overrides,
            "model": model,
            "model_provider": model_provider,
            "approval_policy": approval_policy,
            "permissions_profile_id": permissions_profile_id,
        })
        self.operation_log.append(("resume_thread", thread_id, model))
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id=thread_id,
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
            effective_model=model or "gpt-5.5",
            effective_reasoning_effort=self.config.reasoning_effort or None,
            effective_approval_policy=approval_policy or self.config.approval_policy,
            effective_permissions_profile_id=(
                permissions_profile_id or self.config.permissions_profile_id
            ),
        )
        self.thread_snapshots.setdefault((thread_id, None), snapshot)
        self.loaded_thread_ids.add(thread_id)
        return snapshot

    def resume_thread_page(
        self,
        thread_id: str,
        *,
        limit: int,
        **kwargs,
    ) -> ThreadResumePage:
        snapshot = self.resume_thread(thread_id, **kwargs)
        turns = list(self.thread_turns.get(thread_id, []))
        return ThreadResumePage(
            snapshot=snapshot,
            initial_turns_page=ThreadTurnsPage(
                turns=turns[-limit:],
                next_cursor="older" if len(turns) > limit else None,
            ),
        )

    def update_thread_settings(
        self,
        thread_id: str,
        *,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.update_thread_settings_calls.append(
            {
                "thread_id": thread_id,
                "approval_policy": approval_policy,
                "permissions_profile_id": permissions_profile_id,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
        )
        self.operation_log.append(("update_thread_settings", thread_id, model))

    def unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        self._require_connection_generation(
            "thread/unsubscribe",
            expected_connection_generation,
        )
        self.unsubscribe_thread_calls.append(thread_id)
        self.loaded_thread_ids.discard(thread_id)

    def compact_thread(self, thread_id: str) -> None:
        self.compact_thread_calls.append(thread_id)

    def rename_thread(self, thread_id: str, name: str) -> None:
        self.rename_thread_calls.append((thread_id, name))

    def list_threads_all(
        self,
        *,
        cwd: str | None = None,
        limit: int = 100,
        search_term: str | None = None,
        sort_key: str = "updated_at",
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
        archived: bool | None = None,
        parent_thread_id: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> list[ThreadSummary]:
        del cwd
        del limit
        del search_term
        del sort_key
        del source_kinds
        del model_providers
        del archived
        del timeout
        del require_existing_connection
        self._require_connection_generation(
            "thread/list",
            expected_connection_generation,
        )
        summaries: dict[str, ThreadSummary] = {}
        for (_thread_id, _include_turns), snapshot in self.thread_snapshots.items():
            if isinstance(snapshot, ThreadSnapshot):
                summaries[snapshot.summary.thread_id] = snapshot.summary
        values = list(summaries.values())
        if parent_thread_id:
            values = [
                summary
                for summary in values
                if summary.parent_thread_id == parent_thread_id
            ]
        return values

    def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        search_term: str | None = None,
        sort_key: str = "updated_at",
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
        archived: bool | None = None,
        parent_thread_id: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> tuple[list[ThreadSummary], str | None]:
        start = max(int(cursor or 0), 0)
        page_size = max(int(limit or 0), 1)
        fetch_limit = start + page_size
        threads = self.list_threads_all(
            cwd=cwd,
            limit=fetch_limit,
            search_term=search_term,
            sort_key=sort_key,
            source_kinds=source_kinds,
            model_providers=model_providers,
            archived=archived,
            parent_thread_id=parent_thread_id,
            timeout=timeout,
            require_existing_connection=require_existing_connection,
            expected_connection_generation=expected_connection_generation,
        )
        end = start + page_size
        next_cursor = str(end) if end < len(threads) else None
        return list(threads[start:end]), next_cursor

    def list_thread_turns(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str = "desc",
        items_view: str = "full",
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadTurnsPage:
        del timeout
        del require_existing_connection
        self._require_connection_generation(
            "thread/turns/list",
            expected_connection_generation,
        )
        self.list_thread_turns_calls.append(
            {
                "thread_id": thread_id,
                "cursor": cursor,
                "limit": limit,
                "sort_direction": sort_direction,
                "items_view": items_view,
            }
        )
        turns = list(self.thread_turns.get(thread_id, []))
        if not turns:
            snapshot = self.thread_snapshots.get((thread_id, True))
            if snapshot is None:
                snapshot = self.thread_snapshots.get((thread_id, None))
            if isinstance(snapshot, ThreadSnapshot):
                turns = list(snapshot.turns)
        if limit is not None:
            turns = turns[: max(int(limit), 0)]
        return ThreadTurnsPage(turns=turns)

    def list_thread_items(self, thread_id: str, **kwargs) -> ThreadItemsPage:
        expected_connection_generation = kwargs.pop(
            "expected_connection_generation",
            None,
        )
        self._require_connection_generation(
            "thread/items/list",
            expected_connection_generation,
        )
        self.list_thread_items_calls.append({"thread_id": thread_id, **kwargs})
        return ThreadItemsPage()

    def search_thread_occurrences(
        self,
        thread_id: str,
        **kwargs,
    ) -> ThreadSearchOccurrencesPage:
        expected_connection_generation = kwargs.pop(
            "expected_connection_generation",
            None,
        )
        self._require_connection_generation(
            "thread/searchOccurrences",
            expected_connection_generation,
        )
        self.search_thread_occurrences_calls.append(
            {"thread_id": thread_id, **kwargs}
        )
        return ThreadSearchOccurrencesPage()

    def archive_thread(self, thread_id: str) -> None:
        self.archive_thread_calls.append(thread_id)

    def unarchive_thread(self, thread_id: str) -> ThreadSummary:
        self.unarchive_thread_calls.append(thread_id)
        snapshot = self.thread_snapshots.get((thread_id, None))
        if isinstance(snapshot, Exception):
            raise snapshot
        if isinstance(snapshot, ThreadSnapshot):
            return snapshot.summary
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

    def delete_thread(self, thread_id: str) -> None:
        self.delete_thread_calls.append(thread_id)

    def start_turn(
        self,
        *,
        thread_id: str,
        input_items,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        client_user_message_id: str | None = None,
    ):
        call = self._record_turn_start_call(
            thread_id=thread_id,
            input_items=input_items,
            cwd=cwd,
            model=model,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            sandbox=sandbox,
            reasoning_effort=reasoning_effort,
            client_user_message_id=client_user_message_id,
        )
        self.ordinary_start_turn_calls.append(call)
        result = (
            self.start_turn_results.pop(0)
            if self.start_turn_results
            else {"turn": {"id": "turn-1"}}
        )
        if isinstance(result, Exception):
            raise result
        return result

    def _record_turn_start_call(
        self,
        *,
        thread_id: str,
        input_items,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        client_user_message_id: str | None = None,
    ) -> dict:
        text_items = [
            item.get("text", "")
            for item in input_items or []
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        call = {
            "thread_id": thread_id,
            "text": "\n".join(part for part in text_items if part),
            "input_items": [dict(item) for item in input_items or []],
            "cwd": cwd,
            "model": model,
            "approval_policy": approval_policy,
            "permissions_profile_id": permissions_profile_id,
            "sandbox": sandbox,
            "reasoning_effort": reasoning_effort,
            "client_user_message_id": client_user_message_id,
        }
        self.start_turn_calls.append(call)
        return call

    def steer_turn(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_items,
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ):
        self.steer_turn_calls.append(
            {
                "thread_id": thread_id,
                "expected_turn_id": expected_turn_id,
                "input_items": [dict(item) for item in input_items or []],
                "client_user_message_id": client_user_message_id,
                "expected_connection_generation": expected_connection_generation,
            }
        )
        result = (
            self.steer_turn_results.pop(0)
            if self.steer_turn_results
            else {"turnId": expected_turn_id}
        )
        if isinstance(result, Exception):
            raise result
        return result

    def interrupt_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> None:
        del timeout
        del require_existing_connection
        del expected_connection_generation
        self.interrupt_turn_calls.append({"thread_id": thread_id, "turn_id": turn_id})

    def respond(
        self,
        request_id: str,
        *,
        connection_generation: int,
        result=None,
        error=None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> None:
        del timeout
        del require_existing_connection
        self.respond_calls.append(
            {
                "request_id": request_id,
                "connection_generation": connection_generation,
                "result": result,
                "error": error,
            }
        )

    def respond_with_existing_backend_authority(
        self,
        request_id: str,
        *,
        connection_generation: int,
        result=None,
        error=None,
        timeout: float | None = None,
    ) -> None:
        self.respond(
            request_id,
            connection_generation=connection_generation,
            result=result,
            error=error,
            timeout=timeout,
            require_existing_connection=True,
        )

    def trigger_disconnect(self, connection_generation: int = 1) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect(connection_generation)


def _outbound_success(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str,
    message_id: str,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.CONFIRMED,
        destination_liveness=FeishuDestinationLiveness.REACHABLE,
        chat_id=chat_id,
        attempt_id="fake-attempt-success",
        message_id=message_id,
    )


def _outbound_rejected(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.REJECTED,
        destination_liveness=FeishuDestinationLiveness.UNKNOWN,
        chat_id=chat_id,
        attempt_id="fake-attempt-rejected",
        error_code="230013",
    )


class _FakeBot:
    def __init__(self, data_dir: pathlib.Path) -> None:
        del data_dir
        self.app_id = "cli_test_app"
        self.replies: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []
        self.reply_refs: list[tuple[str, str, str]] = []
        self.reply_ref_calls: list[tuple[str, str, str, bool]] = []
        self.reply_parents: list[tuple[str, str, str]] = []
        self.reply_parent_calls: list[tuple[str, str, str, bool]] = []
        self.card_parents: list[tuple[str, dict, str]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.patch_results: dict[str, bool] = {}
        self.message_contexts: dict[str, dict] = {}
        self.group_modes: dict[str, str] = {}
        self.group_activations: dict[str, dict] = {}
        self.chat_types: dict[str, str] = {}
        self.fetched_chat_types: dict[str, str] = {}
        self.cached_sender_names: dict[str, str] = {}
        self.chat_display_names: dict[str, str] = {}
        self.forgotten_destination_chats: list[str] = []
        self.refreshed_chat_display_names: list[str] = []
        self.reserved_execution_cards: dict[str, str] = {}
        self.admin_open_ids = {"ou_admin"}
        self.bot_identity = {
            "app_id": self.app_id,
            "configured_open_id": "ou_bot",
            "discovered_open_id": "ou_bot",
            "trigger_open_ids": [],
        }
        self.runtime_bot_open_id = "ou_bot"
        self.downloaded_resources: dict[tuple[str, str, str], object] = {}
        self.history_messages: list[object] = []
        self.list_recent_messages_calls: list[dict[str, object]] = []
        self.raw_card_results: dict[str, InteractiveMessageReadResult] = {}
        self.queued_prompt_preparations: list[dict[str, object]] = []
        self.queued_prompt_text_overrides: dict[str, str | None] = {}

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> bool:
        self.replies.append((chat_id, text))
        if parent_message_id:
            self.reply_parents.append((chat_id, text, parent_message_id))
            self.reply_parent_calls.append((chat_id, text, parent_message_id, reply_in_thread))
        return True

    def reply_get_id(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str:
        self.replies.append((chat_id, text))
        if parent_message_id:
            self.reply_parents.append((chat_id, text, parent_message_id))
            self.reply_parent_calls.append((chat_id, text, parent_message_id, reply_in_thread))
            return "text-reply-1"
        self.sent_messages.append((chat_id, "text", json.dumps({"text": text}, ensure_ascii=False)))
        return "text-message-1"

    def reply_card(self, chat_id: str, card: dict, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.cards.append((chat_id, card))
        if parent_message_id:
            self.card_parents.append((chat_id, card, parent_message_id))

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        del attempt_id
        self.reply_refs.append((parent_id, msg_type, content))
        self.reply_ref_calls.append((parent_id, msg_type, content, reply_in_thread))
        return _outbound_success(
            FeishuOutboundOperation.REPLY_MESSAGE,
            chat_id=chat_id,
            message_id="plan-card-1",
        )

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        del attempt_id
        self.sent_messages.append((chat_id, msg_type, content))
        return _outbound_success(
            FeishuOutboundOperation.CREATE_MESSAGE,
            chat_id=chat_id,
            message_id="plan-card-2",
        )

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        del attempt_id
        self.patches.append((message_id, content))
        if self.patch_results.get(message_id, True):
            return _outbound_success(
                FeishuOutboundOperation.PATCH_MESSAGE,
                chat_id=chat_id,
                message_id=message_id,
            )
        return _outbound_rejected(
            FeishuOutboundOperation.PATCH_MESSAGE,
            chat_id=chat_id,
        )

    def delete_message(self, message_id: str) -> bool:
        self.deletes.append(message_id)
        return True

    def make_card_response(self, card=None, toast=None, toast_type="info"):
        return {"card": card, "toast": toast, "toast_type": toast_type}

    def get_message_context(self, message_id: str) -> dict:
        return dict(self.message_contexts.get(message_id, {}))

    def list_recent_messages(
        self,
        *,
        chat_id: str,
        thread_id: str = "",
        limit: int = 20,
        card_msg_content_type: str = "",
    ) -> list[object]:
        self.list_recent_messages_calls.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "limit": limit,
                "card_msg_content_type": card_msg_content_type,
            }
        )
        normalized_thread_id = str(thread_id or "").strip()
        items = [
            item
            for item in self.history_messages
            if str(getattr(item, "thread_id", "") or "").strip() == normalized_thread_id
        ]
        return items[:limit]

    def read_interactive_message(
        self,
        message_id: str,
        *,
        content_dict: dict | None = None,
    ) -> InteractiveMessageReadResult:
        normalized_message_id = str(message_id or "").strip()
        if normalized_message_id in self.raw_card_results:
            return self.raw_card_results[normalized_message_id]
        if not isinstance(content_dict, dict):
            return InteractiveMessageReadResult(text="", card_kind="")
        projection = project_interactive_card_text(content_dict)
        title = str(content_dict.get("title", "") or "").strip()
        if not title and isinstance(content_dict.get("header"), dict):
            title = str(
                ((content_dict.get("header") or {}).get("title") or {}).get("content", "") or ""
            ).strip()
        card_kind = "other"
        if title == "Codex":
            card_kind = "terminal"
        elif title.startswith("Codex 执行过程"):
            card_kind = "execution"
        return InteractiveMessageReadResult(
            text=projection.text,
            card_kind=card_kind,
            has_authoritative_text=projection.has_authoritative_final_reply,
        )

    def read_interactive_message_text(self, message_id: str, *, content_dict: dict | None = None) -> str:
        return self.read_interactive_message(message_id, content_dict=content_dict).text

    def prepare_queued_prompt_text(self, **kwargs) -> str | None:
        self.queued_prompt_preparations.append(dict(kwargs))
        message_id = str(kwargs.get("message_id", "") or "")
        if message_id in self.queued_prompt_text_overrides:
            return self.queued_prompt_text_overrides[message_id]
        return str(kwargs.get("text", "") or "")

    def download_message_resource(self, message_id: str, resource_key: str, *, resource_type: str):
        resource = self.downloaded_resources.get((message_id, resource_type, resource_key))
        if resource is None:
            raise RuntimeError("missing downloaded resource")
        if isinstance(resource, Exception):
            raise resource
        return resource

    def lookup_chat_type(self, chat_id: str) -> str:
        return self.chat_types.get(chat_id, "")

    def fetch_runtime_chat_type(self, chat_id: str) -> str:
        return self.fetched_chat_types.get(chat_id, "")

    def claim_reserved_execution_card(self, message_id: str) -> str:
        return self.reserved_execution_cards.pop(message_id, "")

    def get_sender_display_name(self, *, user_id: str = "", open_id: str = "", sender_type: str = "user") -> str:
        if sender_type == "app":
            return f"机器人:{(open_id or user_id or 'unknown')[:8]}"
        if open_id:
            resolved = {"ou_admin": "Admin", "ou_user": "User", "ou_user2": "Alice"}.get(open_id, open_id[:8])
            self.cached_sender_names[open_id] = resolved
            return resolved
        if user_id:
            return user_id[:8]
        return "unknown"

    def lookup_cached_sender_name(self, sender_id: str) -> str:
        return self.cached_sender_names.get(sender_id, "")

    def lookup_chat_display_name(self, chat_id: str) -> str:
        return self.chat_display_names.get(chat_id, "")

    def get_chat_display_name(self, chat_id: str) -> str:
        return self.lookup_chat_display_name(chat_id)

    def forget_chat_state_after_destination_loss(self, chat_id: str) -> None:
        self.forgotten_destination_chats.append(chat_id)
        self.chat_types.pop(chat_id, None)
        self.chat_display_names.pop(chat_id, None)

    def refresh_chat_display_name(self, chat_id: str) -> str:
        self.refreshed_chat_display_names.append(chat_id)
        return self.chat_display_names.get(chat_id, "")

    def debug_sender_name_resolution(self, open_id: str) -> dict[str, object]:
        resolved_name = self.get_sender_display_name(open_id=open_id)
        return {
            "open_id": open_id,
            "cache_hit": open_id == "ou_user",
            "cached_name": "User" if open_id == "ou_user" else "",
            "resolved_name": resolved_name,
            "used_fallback": open_id not in {"ou_admin", "ou_user", "ou_user2"},
            "fallback_reason": "" if open_id in {"ou_admin", "ou_user", "ou_user2"} else "api_non_success",
            "api_code": "" if open_id in {"ou_admin", "ou_user", "ou_user2"} else 403,
            "api_msg": "" if open_id in {"ou_admin", "ou_user", "ou_user2"} else "permission denied",
            "exception": "",
            "source": "contact_api" if open_id in {"ou_admin", "ou_user", "ou_user2"} else "fallback",
        }

    def is_admin(self, *, open_id: str = "") -> bool:
        return open_id in self.admin_open_ids

    def add_admin_open_id(self, open_id: str) -> list[str]:
        if open_id:
            self.admin_open_ids.add(open_id)
        return sorted(self.admin_open_ids)

    def list_admin_open_ids(self) -> list[str]:
        return sorted(self.admin_open_ids)

    def set_configured_bot_open_id(self, open_id: str) -> str:
        normalized = str(open_id or "").strip()
        self.runtime_bot_open_id = normalized
        self.bot_identity["configured_open_id"] = normalized
        return normalized

    def get_bot_identity_snapshot(self) -> dict[str, object]:
        return dict(self.bot_identity)

    def get_group_mode(self, chat_id: str) -> str:
        return self.group_modes.get(chat_id, "assistant")

    def set_group_mode(self, chat_id: str, mode: str) -> str:
        self.group_modes[chat_id] = mode
        return mode

    def get_group_activation_snapshot(self, chat_id: str) -> dict:
        snapshot = self.group_activations.setdefault(
            chat_id,
            {"activated": False, "activated_by": "", "activated_at": 0},
        )
        return dict(snapshot)

    def activate_group_chat(self, chat_id: str, *, activated_by: str) -> dict:
        snapshot = {
            "activated": True,
            "activated_by": activated_by,
            "activated_at": 1712476800000,
        }
        self.group_activations[chat_id] = snapshot
        return dict(snapshot)

    def deactivate_group_chat(self, chat_id: str) -> dict:
        snapshot = {"activated": False, "activated_by": "", "activated_at": 0}
        self.group_activations[chat_id] = snapshot
        return dict(snapshot)

    def is_group_admin(self, *, open_id: str = "") -> bool:
        return self.is_admin(open_id=open_id)

    def is_group_user_allowed(self, chat_id: str, *, open_id: str = "") -> bool:
        if self.is_group_admin(open_id=open_id):
            return True
        snapshot = self.group_activations.setdefault(
            chat_id,
            {"activated": False, "activated_by": "", "activated_at": 0},
        )
        return bool(snapshot["activated"])

    def extract_non_bot_mentions(self, message_id: str) -> list[dict[str, str]]:
        context = self.get_message_context(message_id)
        return list(context.get("mentions", []))
