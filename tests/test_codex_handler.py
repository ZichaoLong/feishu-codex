import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.cards import build_ask_user_card, build_execution_card, build_terminal_result_card
from bot.adapters.base import (
    RuntimeConfigSummary,
    ThreadGoalSummary,
    RuntimeModelSummary,
    RuntimeReasoningEffortOption,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.feishu_bot import InteractiveMessageReadResult
from bot.card_text_projection import project_interactive_card_text, terminal_result_checksum
from bot.codex_handler import CodexHandler, _replace_text_input_items
from bot.codex_protocol.client import CodexRpcError
from bot.execution_transcript import ExecutionReplySegment
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.service_instance_lease import ServiceInstanceLease, ServiceInstanceLeaseError
from bot.stores.interaction_lease_store import InteractionLeaseStore, make_fcodex_interaction_holder
from bot.stores.terminal_result_store import TerminalResultRecord
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease

_DISPLAY_INIT_COMMAND = feishu_visible_command_syntax("/init <token>")
_DISPLAY_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")
_DISPLAY_RESUME_COMMAND = feishu_visible_command_syntax("/resume <thread_id|thread_name>")
_DISPLAY_LOCAL_RESUME_COMMAND = feishu_visible_command_syntax("fcodex resume <thread_id|thread_name>")
_DISPLAY_CD_COMMAND = feishu_visible_command_syntax("/cd <path>")
_DISPLAY_RENAME_COMMAND = feishu_visible_command_syntax("/rename <title>")
_DISPLAY_LOCAL_THREAD_UNSUBSCRIBE = feishu_visible_command_syntax(
    "focusctl thread detach --thread-id <thread_id>"
)


class _FakeAdapter:
    def __init__(
        self,
        config,
        *,
        on_notification=None,
        on_request=None,
        on_disconnect=None,
        app_server_runtime_store=None,
    ) -> None:
        self.config = config
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_disconnect = on_disconnect
        self.app_server_runtime_store = app_server_runtime_store
        self.start_calls = 0
        self.create_thread_calls: list[dict] = []
        self.resume_thread_calls: list[dict] = []
        self.set_thread_goal_calls: list[dict] = []
        self.update_thread_settings_calls: list[dict] = []
        self.operation_log: list[tuple[str, str, str | None]] = []
        self.start_turn_calls: list[dict] = []
        self.interrupt_turn_calls: list[dict] = []
        self.respond_calls: list[dict] = []
        self.archive_thread_calls: list[str] = []
        self.unsubscribe_thread_calls: list[str] = []
        self.compact_thread_calls: list[str] = []
        self.read_thread_calls: list[dict] = []
        self.thread_snapshots: dict[tuple[str, bool | None], ThreadSnapshot | Exception] = {}
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
            ),
        ]

    def stop(self) -> None:
        return None

    def start(self) -> None:
        self.start_calls += 1

    def current_app_server_url(self) -> str:
        return self.config.app_server_url

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
        return ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd=cwd,
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            )
        )

    def read_thread(self, thread_id: str, include_turns: bool = False):
        self.read_thread_calls.append({"thread_id": thread_id, "include_turns": include_turns})
        snapshot = self.thread_snapshots.get((thread_id, include_turns))
        if snapshot is None:
            snapshot = self.thread_snapshots.get((thread_id, None))
        if snapshot is None:
            raise NotImplementedError
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot

    def get_thread_goal(self, thread_id: str) -> ThreadGoalSummary | None:
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

    def list_loaded_thread_ids(self) -> list[str]:
        return sorted(
            {
                thread_id
                for (thread_id, _include_turns), snapshot in self.thread_snapshots.items()
                if snapshot is not None
                and not isinstance(snapshot, Exception)
                and str(snapshot.summary.status or "").strip() != "notLoaded"
            }
        )

    def resume_thread(
        self,
        thread_id: str,
        *,
        config_overrides: dict | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ):
        self.resume_thread_calls.append({
            "thread_id": thread_id,
            "config_overrides": config_overrides,
            "model": model,
            "model_provider": model_provider,
            "approval_policy": approval_policy,
            "permissions_profile_id": permissions_profile_id,
        })
        self.operation_log.append(("resume_thread", thread_id, model))
        return ThreadSnapshot(
            summary=ThreadSummary(
                thread_id=thread_id,
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            )
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

    def unsubscribe_thread(self, thread_id: str) -> None:
        self.unsubscribe_thread_calls.append(thread_id)

    def compact_thread(self, thread_id: str) -> None:
        self.compact_thread_calls.append(thread_id)

    def list_threads_all(
        self,
        *,
        cwd: str | None = None,
        limit: int = 100,
        search_term: str | None = None,
        sort_key: str = "updated_at",
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
    ) -> list[ThreadSummary]:
        del cwd
        del limit
        del search_term
        del sort_key
        del source_kinds
        del model_providers
        summaries: dict[str, ThreadSummary] = {}
        for (_thread_id, _include_turns), snapshot in self.thread_snapshots.items():
            if isinstance(snapshot, ThreadSnapshot):
                summaries[snapshot.summary.thread_id] = snapshot.summary
        return list(summaries.values())

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
        )
        end = start + page_size
        next_cursor = str(end) if end < len(threads) else None
        return list(threads[start:end]), next_cursor

    def archive_thread(self, thread_id: str) -> None:
        self.archive_thread_calls.append(thread_id)

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
    ):
        text_items = [
            item.get("text", "")
            for item in input_items or []
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        self.start_turn_calls.append(
            {
                "thread_id": thread_id,
                "text": "\n".join(part for part in text_items if part),
                "input_items": [dict(item) for item in input_items or []],
                "cwd": cwd,
                "model": model,
                "approval_policy": approval_policy,
                "permissions_profile_id": permissions_profile_id,
                "sandbox": sandbox,
                "reasoning_effort": reasoning_effort,
            }
        )
        return {"turn": {"id": "turn-1"}}

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        self.interrupt_turn_calls.append({"thread_id": thread_id, "turn_id": turn_id})

    def respond(self, request_id: str, *, result=None, error=None) -> None:
        self.respond_calls.append({"request_id": request_id, "result": result, "error": error})

    def trigger_disconnect(self) -> None:
        if self.on_disconnect is not None:
            self.on_disconnect()


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

    def reply_to_message(self, parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str:
        self.reply_refs.append((parent_id, msg_type, content))
        self.reply_ref_calls.append((parent_id, msg_type, content, reply_in_thread))
        return "plan-card-1"

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str:
        self.sent_messages.append((chat_id, msg_type, content))
        return "plan-card-2"

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return self.patch_results.get(message_id, True)

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


class CodexHandlerTests(unittest.TestCase):
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
    def _binding_keys(handler: CodexHandler) -> tuple[tuple[str, str], ...]:
        with handler._lock:
            return handler._binding_runtime.binding_keys_locked()

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
    def _store_pending_request(handler: CodexHandler, request_key: str, pending: dict) -> None:
        handler._interaction_requests.store_pending_request(request_key, pending)

    @staticmethod
    def _has_pending_request(handler: CodexHandler, request_key: str) -> bool:
        return handler._interaction_requests.has_pending_request(request_key)

    @staticmethod
    def _pending_rename_form_snapshot(handler: CodexHandler, message_id: str) -> dict[str, str] | None:
        return handler._threads_ui_domain.pending_rename_form_snapshot(message_id)

    @staticmethod
    def _register_pending_rename_form(handler: CodexHandler, message_id: str, *, thread_id: str) -> None:
        handler._threads_ui_domain.register_pending_rename_form(message_id, thread_id=thread_id)

    def test_replace_text_input_items_preserves_non_text_items(self) -> None:
        items = [
            {"type": "text", "text": "old"},
            {"type": "input_image", "image_url": "file:///tmp/a.png"},
            {"type": "text", "text": "duplicate"},
        ]

        replaced = _replace_text_input_items(items, "new")

        self.assertEqual(
            replaced,
            [
                {"type": "text", "text": "new"},
                {"type": "input_image", "image_url": "file:///tmp/a.png"},
            ],
        )

    @staticmethod
    def _service_runtime_holder_ids(handler: CodexHandler, thread_id: str) -> tuple[str, ...]:
        lease = handler._thread_runtime_lease_store.load(thread_id)
        if lease is None:
            return ()
        return tuple(holder.holder_id for holder in lease.holders)

    def _make_handler(
        self,
        cfg: dict | None = None,
        *,
        data_dir: pathlib.Path | None = None,
        instance_name: str = "default",
    ) -> tuple[CodexHandler, _FakeBot]:
        if data_dir is None:
            tempdir = tempfile.TemporaryDirectory()
            self.addCleanup(tempdir.cleanup)
            data_dir = pathlib.Path(tempdir.name)
        effective_cfg = {"mirror_watchdog_seconds": 999999}
        effective_cfg.update(dict(cfg or {}))
        config_patch = patch("bot.codex_handler.load_config_file", return_value=effective_cfg)
        adapter_patch = patch("bot.codex_handler.CodexAppServerAdapter", _FakeAdapter)
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
        handler = CodexHandler(data_dir=data_dir)
        self.addCleanup(handler.shutdown)
        bot = _FakeBot(data_dir)
        handler.bot = bot
        return handler, bot

    def test_model_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/model gpt-5.5")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["model"], "gpt-5.5")
        self.assertIn("已切换当前会话的 model override：`gpt-5.5`", bot.replies[-1][1])
        self.assertIn("共享 Codex thread", bot.replies[-1][1])

    def test_model_command_auto_clears_override(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["model"] = "gpt-5.5"

        handler.handle_message("ou_user", "c1", "/model auto")

        self.assertEqual(state["model"], "")
        self.assertIn("已切换当前会话的 model override：`auto`", bot.replies[-1][1])

    def test_effort_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/effort high")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertIn("已切换当前会话的 effort override：`high`", bot.replies[-1][1])
        self.assertIn("共享 Codex thread", bot.replies[-1][1])

    def test_effort_command_auto_clears_override(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["reasoning_effort"] = "medium"

        handler.handle_message("ou_user", "c1", "/effort auto")

        self.assertEqual(state["reasoning_effort"], "")
        self.assertIn("已切换当前会话的 effort override：`auto`", bot.replies[-1][1])

    def test_on_register_eagerly_starts_adapter(self) -> None:
        handler, bot = self._make_handler()

        handler.on_register(bot)

        self.assertIs(handler.bot, bot)
        self.assertEqual(handler._adapter.start_calls, 1)

    def test_on_register_fails_fast_when_service_instance_is_already_owned(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        lease = ServiceInstanceLease(data_dir)
        lease.acquire(control_endpoint="tcp://127.0.0.1:32001")
        self.addCleanup(lease.release)

        with self.assertRaises(ServiceInstanceLeaseError):
            handler.on_register(bot)

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
        handler._terminal_result_store.upsert(
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
        handler._terminal_result_store.upsert(
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
        state = handler._get_runtime_state("ou_admin", "c1", "msg-main")
        state["current_thread_id"] = "codex-thread-1"
        handler._terminal_result_store.upsert(
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
        state = handler._get_runtime_state("ou_admin", "c1", "msg-thread")
        state["current_thread_id"] = "codex-thread-1"
        handler._terminal_result_store.upsert(
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

        handler.on_register(bot)

        metadata = handler._service_instance_lease.load_metadata()
        status = control_request(data_dir, "service/status")

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.owner_pid, os.getpid())
        self.assertNotEqual(metadata.owner_token, "stale-owner-token")
        self.assertTrue(handler._service_instance_lease.owns_current_lease())
        self.assertTrue(metadata.control_endpoint.startswith("tcp://127.0.0.1:"))
        self.assertEqual(status["pid"], os.getpid())

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
            handler.on_register(bot)

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
            handler.on_register(bot)

        self.assertTrue(handler._runtime_loop._closed)
        self.assertEqual(stop_calls, ["adapter"])
        self.assertEqual(handler._service_control_plane.control_endpoint, "")
        self.assertIsNone(handler._service_instance_lease.load_metadata())

    def test_external_turn_started_opens_new_execution_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "old-card"
            state["execution_transcript"].set_reply_text("收到")
            state["execution_transcript"].append_process_note("old log")
            state["running"] = False

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-2"}})
        handler._handle_agent_message_delta({"threadId": "thread-1", "turnId": "turn-2", "delta": "新的回复"})

        self.assertEqual(len(bot.sent_messages), 1)
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_message_id"], "plan-card-2")
        self.assertEqual(
            handler._get_runtime_state("ou_user", "c1")["execution_transcript"].reply_text(),
            "新的回复",
        )

    def test_external_turn_started_finalizes_previous_execution_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "old-card"
            state["execution_transcript"].set_reply_text("上一轮回复")
            state["execution_transcript"].append_process_note("上一轮日志")
            state["running"] = False

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-2"}})

        self._wait_until(lambda: any(message_id == "old-card" for message_id, _ in bot.patches))
        patched = json.loads(next(content for message_id, content in bot.patches if message_id == "old-card"))
        body_elements = patched["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_message_id"], "plan-card-2")

    def test_prompt_start_response_sets_current_turn_id_immediately(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["current_turn_id"], "turn-1")

    def test_cancel_before_turn_started_is_applied_after_turn_started(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_turn_id"] = ""

        ok, message = handler._cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(handler._adapter.interrupt_turn_calls, [])
        self.assertTrue(handler._get_runtime_state("ou_user", "c1")["pending_cancel"])

        handler._handle_turn_started({"threadId": "thread-created", "turn": {"id": "turn-1"}})

        self.assertEqual(
            handler._adapter.interrupt_turn_calls,
            [{"thread_id": "thread-created", "turn_id": "turn-1"}],
        )
        self.assertFalse(handler._get_runtime_state("ou_user", "c1")["pending_cancel"])

    def test_cancel_recovers_from_missing_thread(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        def _raise_missing_thread(*, thread_id: str, turn_id: str):
            del thread_id
            del turn_id
            raise CodexRpcError("turn/interrupt", {"code": -32000, "message": "thread not found: thread-created"})

        handler._adapter.interrupt_turn = _raise_missing_thread

        ok, message = handler._cancel_current_turn("ou_user", "c1")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertTrue(ok)
        self.assertEqual(message, "当前执行已结束，已刷新卡片状态。")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["current_turn_id"], "")

    def test_continue_auto_resumes_bound_thread_when_loaded_thread_is_missing(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["running"] = False
            state["current_turn_id"] = ""

        original_start_turn = handler._adapter.start_turn
        attempts: list[str] = []

        def _start_turn_with_missing_loaded_thread(**kwargs):
            attempts.append(kwargs["thread_id"])
            if len(attempts) == 1:
                raise CodexRpcError("turn/start", {"code": -32000, "message": "thread not found: thread-created"})
            return original_start_turn(**kwargs)

        handler._adapter.start_turn = _start_turn_with_missing_loaded_thread

        handler.handle_message("ou_user", "c1", "继续")

        self.assertEqual(attempts, ["thread-created", "thread-created"])
        self.assertEqual(
            handler._adapter.resume_thread_calls,
            [
                {
                    "thread_id": "thread-created",
                    "config_overrides": None,
                    "model": None,
                    "model_provider": None,
                    "approval_policy": None,
                    "permissions_profile_id": None,
                }
            ],
        )
        self.assertEqual(len(handler._adapter.create_thread_calls), 1)
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "thread-created")
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_reconcile_runtime_loss_keeps_thread_binding_for_next_prompt(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler._adapter.thread_snapshots[("thread-created", True)] = CodexRpcError(
            "thread/read",
            {"code": -32000, "message": "thread not found: thread-created"},
        )
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_thread_id"], "thread-created")

        original_start_turn = handler._adapter.start_turn
        attempts: list[str] = []

        def _start_turn_with_missing_loaded_thread(**kwargs):
            attempts.append(kwargs["thread_id"])
            if len(attempts) == 1:
                raise CodexRpcError("turn/start", {"code": -32000, "message": "thread not found: thread-created"})
            return original_start_turn(**kwargs)

        handler._adapter.start_turn = _start_turn_with_missing_loaded_thread

        handler.handle_message("ou_user", "c1", "继续")

        self.assertEqual(attempts, ["thread-created", "thread-created"])
        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-created",
                "config_overrides": None,
                "model": None,
                "model_provider": None,
                "approval_policy": None,
                "permissions_profile_id": None,
            },
        )
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_running_p2p_prompt_queues_and_drains_after_current_turn(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler.handle_message("ou_user", "c1", "follow up", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：1"))

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "follow up")

    def test_recalled_queued_prompt_is_removed_before_drain(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello", message_id="m-1")
        handler.handle_message("ou_user", "c1", "follow up", message_id="m-2")

        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：1"))

        handler._handle_message_recalled_impl("c1", "m-2")
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)

    def test_recalled_running_prompt_does_not_cancel_current_turn(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello", message_id="m-1")
        handler._handle_message_recalled_impl("c1", "m-1")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertTrue(state["running"])
        self.assertEqual(state["current_prompt_message_id"], "m-1")
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)

    def test_queued_group_prompt_keeps_origin_context_after_message_context_expires(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        bot.message_contexts["m-2"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler.handle_message("ou_user", "chat-group", "第二轮", message_id="m-2")
        bot.message_contexts.pop("m-2", None)

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_user", "chat-group", "m-2")
        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "第二轮")
        self.assertEqual(state["current_actor_open_id"], "ou_user")
        self.assertEqual(bot.reply_ref_calls[-1][0], "m-2")
        self.assertTrue(bot.reply_ref_calls[-1][3])

    def test_running_group_turn_routes_same_binding_followup(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")

        self.assertTrue(handler.should_route_group_followup_prompt("ou_user", "chat-group", message_id="m-2"))

    def test_group_followup_route_requires_running_turn_but_not_same_actor(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        self.assertFalse(handler.should_route_group_followup_prompt("ou_user", "chat-group", message_id="m-1"))

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")

        self.assertTrue(handler.should_route_group_followup_prompt("ou_user2", "chat-group", message_id="m-2"))

    def test_running_group_prompt_queues_for_different_actor_on_same_binding(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler.handle_message("ou_user2", "chat-group", "插播", message_id="m-2")

        self.assertEqual(bot.reply_parents[-1], ("chat-group", "已排队，将在当前执行结束后继续。队列位置：1", "m-2"))

    def test_queued_prompt_prepares_deferred_assistant_context_before_dequeue_start(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user2",
            "sender_user_id": "u-user2",
            "thread_id": "om-thread",
            "assistant_context_mode": "deferred_recovery",
            "assistant_context_seq": 7,
            "created_at": 1712476800000,
            "sender_name": "Alice",
        }
        bot.queued_prompt_text_overrides["m-2"] = "prepared assistant prompt"

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler.handle_message("ou_user2", "chat-group", "请处理", message_id="m-2")
        bot.message_contexts.pop("m-2", None)

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "prepared assistant prompt")
        self.assertEqual(
            bot.queued_prompt_preparations[-1],
            {
                "chat_id": "chat-group",
                "message_id": "m-2",
                "text": "请处理",
                "assistant_context_mode": "deferred_recovery",
                "assistant_context_created_at": 1712476800000,
                "assistant_context_seq": 7,
                "assistant_context_sender_name": "Alice",
                "origin_feishu_thread_id": "om-thread",
            },
        )

    def test_queued_prompt_prepare_failure_does_not_block_following_queue_item(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user2",
            "assistant_context_mode": "deferred_recovery",
            "assistant_context_seq": 7,
            "created_at": 1712476800000,
            "sender_name": "Alice",
        }
        bot.message_contexts["m-3"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.queued_prompt_text_overrides["m-2"] = None

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler.handle_message("ou_user2", "chat-group", "会失败的 queued mention", message_id="m-2")
        handler.handle_message("ou_user", "chat-group", "后续 prompt", message_id="m-3")

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual([call["message_id"] for call in bot.queued_prompt_preparations], ["m-2", "m-3"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "后续 prompt")

    def test_snapshot_timeout_only_marks_runtime_degraded(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler._adapter.thread_snapshots[("thread-created", True)] = TimeoutError(
            "Codex request timed out: thread/read"
        )

        finalized = handler._reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-created",
            turn_id="turn-1",
        )

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(finalized)
        self.assertTrue(state["running"])
        self.assertEqual(state["runtime_channel_state"], "degraded")
        self.assertEqual(state["current_turn_id"], "turn-1")
        self.assertEqual(state["current_message_id"], "plan-card-2")

    def test_terminal_signal_finalizes_immediately_before_background_reconcile(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["last_execution_message_id"], "plan-card-2")
        schedule_reconcile.assert_called_once()

    def test_background_terminal_reconcile_only_patches_old_card(self) -> None:
        handler, bot = self._make_handler()

        target = handler._capture_terminal_reconcile_target("ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        self.assertIsNone(target)

        handler.handle_message("ou_user", "c1", "hello")
        target = handler._capture_terminal_reconcile_target("ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None

        handler._finalize_execution_card_from_state("ou_user", "c1")
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "new-card"
            state["current_turn_id"] = "turn-2"
            state["running"] = True
            state["awaiting_local_turn_started"] = False

        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [
                        {"type": "agentMessage", "text": "hello final answer"},
                    ],
                }
            ],
        )

        handler._run_terminal_execution_reconcile(target)

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["current_message_id"], "new-card")
        self.assertTrue(any(message_id == target.card_message_id for message_id, _ in bot.patches))
        self.assertFalse(any(message_id == "new-card" for message_id, _ in bot.patches))

    def test_group_prompts_share_backend_state_by_chat_id(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})
        handler.handle_message("ou_user2", "chat-group", "第二轮", message_id="m-2")

        self.assertEqual(len(handler._adapter.create_thread_calls), 1)
        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.start_turn_calls],
            ["thread-created", "thread-created"],
        )
        self.assertIs(handler._get_runtime_state("ou_user", "chat-group"), handler._get_runtime_state("ou_user2", "chat-group"))

    def test_p2p_stored_binding_survives_handler_restart(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        project_dir = data_dir / "project"
        project_dir.mkdir()

        handler1, _ = self._make_handler(data_dir=data_dir)
        handler1.handle_message("ou_user", "c1", f"/cd {project_dir}")
        handler1.handle_message("ou_user", "c1", "/permissions danger-full-access")
        handler1.handle_message("ou_user", "c1", "/model gpt-5.5")
        handler1.handle_message("ou_user", "c1", "/effort high")
        handler1.handle_message("ou_user", "c1", "hello")

        handler2, _ = self._make_handler(data_dir=data_dir)
        state = handler2._get_runtime_state("ou_user", "c1")

        self.assertEqual(state["working_dir"], str(project_dir))
        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["current_thread_title"], "（无标题）")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(state["model"], "gpt-5.5")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertFalse(state["running"])

        handler2.handle_message("ou_user", "c1", "follow up")

        self.assertEqual(len(handler2._adapter.create_thread_calls), 0)
        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-created")

    def test_p2p_stored_binding_hydrates_detached_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, _ = self._make_handler(data_dir=data_dir)
        handler1.handle_message("ou_user", "c1", "hello")

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        state = handler2._get_runtime_state("ou_user", "c1")

        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._thread_subscribers("thread-created"), ())

        handler2.handle_message("ou_user", "c1", "follow up")
        handler2._handle_turn_started({"threadId": "thread-created", "turn": {"id": "turn-1"}})
        handler2._handle_agent_message_delta(
            {"threadId": "thread-created", "turnId": "turn-1", "delta": "恢复后事件正常路由"}
        )
        handler2._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-created")
        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-created")
        self.assertEqual(handler2._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertTrue(bot2.patches)
        self.assertTrue(
            any("恢复后事件正常路由" in payload for _message_id, payload in bot2.patches)
        )

    def test_group_stored_binding_survives_handler_restart(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, bot1 = self._make_handler(data_dir=data_dir)
        bot1.message_contexts["m-bind"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        handler1._bind_thread(
            "ou_user",
            "chat-group",
            ThreadSummary(
                thread_id="thread-group",
                cwd="/tmp/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            message_id="m-bind",
        )

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        bot2.message_contexts["m-status"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        state = handler2._get_runtime_state("ou_user2", "chat-group", "m-status")

        self.assertEqual(state["current_thread_id"], "thread-group")
        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler2))

        bot2.message_contexts["m-prompt"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        handler2.handle_message("ou_user2", "chat-group", "第二轮", message_id="m-prompt")

        self.assertEqual(len(handler2._adapter.create_thread_calls), 0)
        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-group")

    def test_group_stored_binding_hydrates_detached_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, bot1 = self._make_handler(data_dir=data_dir)
        bot1.message_contexts["m-bind"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        handler1._bind_thread(
            "ou_user",
            "chat-group",
            ThreadSummary(
                thread_id="thread-group",
                cwd="/tmp/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            message_id="m-bind",
        )

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        bot2.message_contexts["m-status"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        state = handler2._get_runtime_state("ou_user2", "chat-group", "m-status")

        self.assertEqual(state["current_thread_id"], "thread-group")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._thread_subscribers("thread-group"), ())

        bot2.message_contexts["m-prompt"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        handler2.handle_message("ou_user2", "chat-group", "继续", message_id="m-prompt")
        handler2._handle_turn_started({"threadId": "thread-group", "turn": {"id": "turn-1"}})
        handler2._handle_agent_message_delta(
            {"threadId": "thread-group", "turnId": "turn-1", "delta": "群重启后事件正常路由"}
        )
        handler2._handle_turn_completed({"threadId": "thread-group", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-group")
        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-group")
        self.assertEqual(handler2._get_runtime_state("ou_user2", "chat-group", "m-status")["feishu_runtime_state"], "attached")
        self.assertTrue(bot2.patches)
        self.assertTrue(
            any("群重启后事件正常路由" in payload for _message_id, payload in bot2.patches)
        )

    def test_restart_downgrades_multi_subscriber_feishu_runtime_and_owner(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler1, _ = self._make_handler(data_dir=data_dir)
        handler1._bind_thread("ou_user", "chat-a", thread)
        handler1._bind_thread("ou_user", "chat-b", thread)
        handler1.handle_message("ou_user", "chat-a", "first turn")

        handler2, bot2 = self._make_handler(data_dir=data_dir)

        self.assertEqual(handler2._thread_subscribers("thread-1"), ())
        interaction_owner = handler2._binding_runtime.interaction_owner_snapshot_locked(
            "thread-1",
            current_binding=("ou_user", "chat-a"),
        )
        self.assertEqual(interaction_owner["kind"], "none")
        self.assertEqual(handler2._get_runtime_state("ou_user", "chat-a")["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._get_runtime_state("ou_user", "chat-b")["feishu_runtime_state"], "detached")

        handler2._handle_adapter_request_impl(
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )
        handler2._handle_agent_message_delta({"threadId": "thread-1", "turnId": "turn-1", "delta": "恢复后继续"})

        self.assertEqual(bot2.sent_messages, [])
        self.assertEqual(handler2._get_runtime_state("ou_user", "chat-a")["execution_transcript"].reply_text(), "")
        self.assertEqual(handler2._get_runtime_state("ou_user", "chat-b")["execution_transcript"].reply_text(), "")

    def test_adapter_disconnect_fail_closes_attached_runtime_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler._handle_turn_started({"threadId": "thread-created", "turn": {"id": "turn-1"}})
        handler._handle_agent_message_delta({"threadId": "thread-created", "turnId": "turn-1", "delta": "partial"})

        handler._handle_adapter_disconnect_impl()

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(handler._thread_subscribers("thread-created"), ())
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertIn("Codex websocket disconnected", state["execution_transcript"].process_text())
        self.assertTrue(any("Codex websocket disconnected" in payload for _message_id, payload in bot.patches))

    def test_adapter_disconnect_fail_closes_pending_interaction_requests_without_upstream_response(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler._interaction_requests.store_pending_request("req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-created"},
            "thread_id": "thread-created",
            "title": "Codex 命令执行审批",
            "message_id": "approval-card-1",
            "chat_id": "c1",
            "sender_id": "ou_user",
            "status": "pending",
        })

        handler._handle_adapter_disconnect_impl()

        self.assertFalse(handler._interaction_requests.has_pending_request("req-1"))
        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertTrue(any(message_id == "approval-card-1" for message_id, _payload in bot.patches))
        self.assertTrue(
            any("websocket 已断开" in payload for message_id, payload in bot.patches if message_id == "approval-card-1")
        )

    def test_adapter_disconnect_fail_closes_pending_interaction_requests_even_without_attached_binding(self) -> None:
        handler, bot = self._make_handler()

        handler._interaction_requests.store_pending_request("req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-created"},
            "thread_id": "thread-created",
            "title": "Codex 用户输入",
            "message_id": "approval-card-1",
            "chat_id": "c1",
            "sender_id": "ou_user",
            "status": "pending",
        })

        handler._handle_adapter_disconnect_impl()

        self.assertFalse(handler._interaction_requests.has_pending_request("req-1"))
        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertTrue(any(message_id == "approval-card-1" for message_id, _payload in bot.patches))
        self.assertTrue(
            any("websocket 已断开" in payload for message_id, payload in bot.patches if message_id == "approval-card-1")
        )

    def test_status_shows_untitled_instead_of_unbound_when_thread_exists(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("当前线程：`thread-c…` （无标题）", rendered)
        self.assertNotIn("（未绑定线程）", rendered)

    def test_turn_completed_finalizes_immediately_and_schedules_terminal_reconcile(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler._handle_agent_message_delta({"threadId": "thread-created", "turnId": "turn-1", "delta": "完整"})
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "完整")
        schedule_reconcile.assert_called_once()
        patched_card = json.loads(bot.patches[-1][1])
        self.assertIn("完整", json.dumps(patched_card, ensure_ascii=False))

    def test_thread_status_inactive_finalizes_immediately_and_schedules_terminal_reconcile(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            handler._handle_thread_status_changed({"threadId": "thread-created", "status": {"type": "idle"}})

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        schedule_reconcile.assert_called_once()
        patched_card = json.loads(bot.patches[-1][1])
        body_elements = patched_card["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )

    def test_thread_closed_finalizes_immediately_without_clearing_binding(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            handler._handle_thread_closed({"threadId": "thread-created"})

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_thread_id"], "thread-created")
        schedule_reconcile.assert_called_once()
        patched_card = json.loads(bot.patches[-1][1])
        body_elements = patched_card["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )

    def test_watchdog_reconciles_missed_terminal_notifications(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "c1", "hello")
        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [
                        {"type": "agentMessage", "text": "watchdog final"},
                    ],
                }
            ],
        )
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            generation = state["mirror_watchdog_generation"]
            if state["mirror_watchdog_timer"] is not None:
                state["mirror_watchdog_timer"].cancel()
                state["mirror_watchdog_timer"] = None

        handler._run_mirror_watchdog("ou_user", "c1", generation)

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["terminal_result_text"], "watchdog final")
        card = json.loads(bot.sent_messages[-1][2])
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("watchdog final", card["body"]["elements"][-1]["content"])

    def test_cancel_refreshes_stale_execution_card_when_turn_already_finished(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["running"] = False
            state["current_turn_id"] = ""
            state["execution_transcript"].set_reply_text("done")

        ok, message = handler._cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(message, "当前执行已结束，已刷新卡片状态。")
        patched_card = json.loads(bot.patches[-1][1])
        body_elements = patched_card["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )

    def test_local_turn_started_reuses_existing_execution_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "existing-card"
            state["awaiting_local_turn_started"] = True
            state["running"] = True

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})

        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_message_id"], "existing-card")

    def test_duplicate_turn_started_does_not_open_second_execution_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "existing-card"
            state["current_turn_id"] = "turn-1"
            state["running"] = True
            state["awaiting_local_turn_started"] = False

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})

        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_message_id"], "existing-card")

    def test_group_thread_binding_is_not_treated_as_takeover_for_same_chat(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._bind_thread("ou_user", "chat-group", thread)
        handler._bind_thread("ou_user2", "chat-group", thread)

        self.assertEqual(bot.replies, [])

    def test_rebinding_same_thread_does_not_unsubscribe_current_subscription(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._bind_thread("ou_user", "c1", thread)
        handler._bind_thread("ou_user", "c1", thread)

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_bind_thread_failure_keeps_existing_service_runtime_lease(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler.on_register(bot)
        handler._bind_thread("ou_user", "c1", thread)
        holder_ids_before = self._service_runtime_holder_ids(handler, "thread-1")

        with patch.object(handler, "_resolve_runtime_binding", side_effect=RuntimeError("bind failed")):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                handler._bind_thread("ou_user2", "c2", thread)

        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-1"), holder_ids_before)
        self.assertEqual(handler._thread_subscribers("thread-1"), (("ou_user", "c1"),))

    def test_group_terminal_result_card_stays_on_trigger_message(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        target = handler._capture_terminal_reconcile_target(
            "ou_user",
            "chat-group",
            thread_id="thread-created",
            turn_id="turn-1",
        )
        assert target is not None
        handler._finalize_execution_card_from_state("ou_user", "chat-group")
        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="completed",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [{"type": "agentMessage", "text": "123456789"}],
                }
            ],
        )

        handler._run_terminal_execution_reconcile(target)

        self.assertEqual(bot.reply_refs[-1][0], "m-thread")
        self.assertEqual(bot.reply_ref_calls[-1][3], True)
        card = json.loads(bot.reply_refs[-1][2])
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("123456789", card["body"]["elements"][-1]["content"])

    def test_group_terminal_result_card_stays_in_topic_after_message_context_is_gone(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        target = handler._capture_terminal_reconcile_target(
            "ou_user",
            "chat-group",
            thread_id="thread-created",
            turn_id="turn-1",
        )
        assert target is not None
        bot.message_contexts.pop("m-thread", None)
        handler._finalize_execution_card_from_state("ou_user", "chat-group")
        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="completed",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [{"type": "agentMessage", "text": "123456789"}],
                }
            ],
        )

        handler._run_terminal_execution_reconcile(target)

        self.assertEqual(bot.reply_refs[-1][0], "m-thread")
        self.assertEqual(bot.reply_ref_calls[-1][3], True)

    def test_multiple_bindings_share_thread_but_only_owner_can_write_until_turn_finishes(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._bind_thread("ou_user", "chat-a", thread)
        handler._bind_thread("ou_user", "chat-b", thread)
        self.assertEqual(handler._thread_subscribers("thread-1"), (("ou_user", "chat-a"), ("ou_user", "chat-b")))

        handler.handle_message("ou_user", "chat-a", "first turn")

        self.assertEqual(
            handler._binding_runtime.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=("ou_user", "chat-a"),
            )["relation"],
            "current",
        )
        self.assertEqual(handler._adapter.start_turn_calls[-1]["thread_id"], "thread-1")

        handler.handle_message("ou_user", "chat-b", "second turn")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1][0], "chat-b")
        self.assertIn("当前线程正由另一飞书会话执行", bot.replies[-1][1])
        self.assertEqual(
            handler._binding_runtime.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=("ou_user", "chat-a"),
            )["relation"],
            "current",
        )

        handler._handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler._binding_runtime.interaction_owner_snapshot_locked("thread-1")["kind"], "none")

        handler.handle_message("ou_user", "chat-b", "third turn")

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(
            handler._binding_runtime.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=("ou_user", "chat-b"),
            )["relation"],
            "current",
        )

    def test_resume_rejects_thread_shared_by_all_mode_group(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-a"] = "group"
        bot.chat_types["chat-b"] = "group"
        bot.group_modes["chat-a"] = "all"
        bot.message_contexts["m-b"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "chat-a", thread)

        handler._resume_thread_on_runtime("ou_user2", "chat-b", "thread-1", message_id="m-b")

        self.assertEqual(handler._get_runtime_state("ou_user2", "chat-b", "m-b")["current_thread_id"], "")
        self.assertIn("`all` 模式", bot.replies[-1][1])
        self.assertIn("其他群聊独占", bot.replies[-1][1])

    def test_turn_completion_finalizes_all_subscribers_without_owner_notice(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._bind_thread("ou_user", "chat-a", thread)
        handler._bind_thread("ou_user", "chat-b", thread)
        handler.handle_message("ou_user", "chat-a", "first turn")
        handler.handle_message("ou_user", "chat-b", "second turn")

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})
        handler._handle_agent_message_delta({"threadId": "thread-1", "turnId": "turn-1", "delta": "done"})
        handler._handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertNotIn(
            ("chat-b", "线程 `thread-1…` 的上一轮执行已结束；本会话现在可继续提问。"),
            bot.replies,
        )
        state_b = handler._get_runtime_state("ou_user", "chat-b")
        self.assertEqual(state_b["current_message_id"], "")
        self.assertTrue(state_b["last_execution_message_id"])
        self.assertEqual(state_b["terminal_result_text"], "done")

    def test_handle_chat_unavailable_clears_binding_and_persistence(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        bot.chat_types["chat-group"] = "group"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "chat-group", thread)

        handler.handle_chat_unavailable("chat-group", reason="disbanded")

        self.assertNotIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])

        handler2, _ = self._make_handler(data_dir=data_dir)
        state = handler2._get_runtime_state("ou_user", "chat-group", "m-group")
        self.assertEqual(state["current_thread_id"], "")

    def test_turn_completion_skips_inactive_non_owner_subscribers(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._bind_thread("ou_user", "chat-a", thread)
        handler._bind_thread("ou_user", "chat-b", thread)
        handler.handle_message("ou_user", "chat-a", "first turn")

        handler._handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertNotIn(
            ("chat-b", "线程 `thread-1…` 的上一轮执行已结束；本会话现在可继续提问。"),
            bot.replies,
        )

    def test_prompt_is_denied_when_shared_interaction_lease_is_owned_by_fcodex(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:other", owner_pid=os.getpid()),
        )
        handler._bind_thread(
            "ou_user",
            "c1",
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
        )

        handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies[-1][0], "c1")
        self.assertIn("当前线程正由另一终端执行", bot.replies[-1][1])

    def test_approval_request_is_suppressed_when_shared_interaction_owner_is_fcodex(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler._bind_thread(
            "ou_user",
            "c1",
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
        )
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:other", owner_pid=os.getpid()),
        )

        handler._handle_adapter_request_impl(
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(bot.sent_messages, [])
        self.assertEqual(bot.reply_refs, [])
        self.assertFalse(self._has_pending_request(handler, "req-1"))

    def test_approval_request_reply_stays_in_topic_after_message_context_is_gone(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        bot.message_contexts.pop("m-thread", None)

        handler._handle_adapter_request_impl(
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-created",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(bot.reply_refs[-1][0], "m-thread")
        self.assertTrue(bot.reply_ref_calls[-1][3])

    def test_approval_request_routes_to_current_interaction_owner(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._bind_thread("ou_user", "chat-a", thread)
        handler._bind_thread("ou_user", "chat-b", thread)
        handler.handle_message("ou_user", "chat-a", "first turn")

        handler._handle_adapter_request_impl(
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(bot.sent_messages[-1][0], "chat-a")
        self.assertNotEqual(bot.sent_messages[-1][0], "chat-b")

    def test_server_request_resolved_closes_approval_card_as_handled_elsewhere(self) -> None:
        handler, bot = self._make_handler()
        self._store_pending_request(handler, "req-1", {
            "message_id": "msg-approval",
            "title": "Codex 命令执行审批",
            "method": "item/commandExecution/requestApproval",
        })

        handler._handle_server_request_resolved({"requestId": "req-1"})

        self.assertFalse(self._has_pending_request(handler, "req-1"))
        self.assertEqual(bot.patches[-1][0], "msg-approval")
        self.assertIn("在其他终端处理", bot.patches[-1][1])

    def test_server_request_resolved_closes_user_input_card_as_handled_elsewhere(self) -> None:
        handler, bot = self._make_handler()
        self._store_pending_request(handler, "req-1", {
            "message_id": "msg-input",
            "title": "Codex 用户输入",
            "method": "item/tool/requestUserInput",
        })

        handler._handle_server_request_resolved({"requestId": "req-1"})

        self.assertFalse(self._has_pending_request(handler, "req-1"))
        self.assertEqual(bot.patches[-1][0], "msg-input")
        self.assertIn("该请求已在其他终端处理", bot.patches[-1][1])

    def test_turn_completion_releases_shared_interaction_lease(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir)
        store = InteractionLeaseStore(data_dir)
        handler._bind_thread(
            "ou_user",
            "c1",
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
        )

        handler.handle_message("ou_user", "c1", "first turn")

        self.assertIsNotNone(store.load("thread-1"))

        handler._handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertIsNone(store.load("thread-1"))

    def test_terminal_reconcile_fallback_does_not_duplicate_terminal_result_delivery(self) -> None:
        handler, bot = self._make_handler()
        handler._card_reply_limit = 5
        bot.message_contexts["msg-1"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user",
        }

        handler.handle_message("ou_user", "c1", "hello", message_id="msg-1")
        bot.patch_results["plan-card-1"] = False
        handler._handle_agent_message_delta({"threadId": "thread-created", "turnId": "turn-1", "delta": "123456789"})
        target = handler._capture_terminal_reconcile_target("ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})
        self._wait_until(
            lambda: any(
                parent_id == "msg-1"
                and msg_type == "interactive"
                and json.loads(content)["header"]["title"]["content"] == "Codex"
                for parent_id, msg_type, content in bot.reply_refs
            )
        )
        reply_refs_before_reconcile = list(bot.reply_refs)
        handler._adapter.thread_snapshots[("thread-created", True)] = RuntimeError("snapshot down")
        handler._run_terminal_execution_reconcile(target)

        self.assertEqual(bot.replies, [])
        self.assertEqual(bot.reply_refs, reply_refs_before_reconcile)
        terminal_cards = [
            json.loads(content)
            for parent_id, msg_type, content in bot.reply_refs
            if parent_id == "msg-1" and msg_type == "interactive"
        ]
        self.assertEqual(len(terminal_cards), 2)
        card = next(card for card in terminal_cards if card["header"]["title"]["content"] == "Codex")
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("123456789", card["body"]["elements"][-1]["content"])

    def test_terminal_reconcile_sends_authoritative_result_card_from_snapshot_without_live_reply_delta(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "c1", "hello")
        target = handler._capture_terminal_reconcile_target("ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None
        handler._finalize_execution_card_from_state("ou_user", "c1")
        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="completed",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [{"type": "agentMessage", "text": "snapshot final answer"}],
                }
            ],
        )

        handler._run_terminal_execution_reconcile(target)

        self.assertEqual(bot.sent_messages[-1][1], "interactive")
        card = json.loads(bot.sent_messages[-1][2])
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("snapshot final answer", card["body"]["elements"][-1]["content"])
        self.assertEqual(bot.deletes, [])

    def test_execution_card_is_patchable_shared_card(self) -> None:
        card = build_execution_card("", [], running=True)

        self.assertTrue(card["config"]["update_multi"])

    def test_execution_card_renders_native_reply_divider(self) -> None:
        card = build_execution_card(
            "",
            [
                ExecutionReplySegment("assistant", "第一段"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "第二段"),
            ],
            running=False,
        )

        reply_panel = next(
            element
            for element in card["body"]["elements"]
            if isinstance(element, dict)
            and element.get("tag") == "collapsible_panel"
            and element.get("header", {}).get("title", {}).get("content") == "回复"
        )
        self.assertEqual(
            [element["tag"] for element in reply_panel["elements"]],
            ["markdown", "hr", "markdown"],
        )

    def test_execution_card_process_panel_defaults_to_collapsed(self) -> None:
        card = build_execution_card(
            "process log",
            [ExecutionReplySegment("assistant", "reply")],
            running=True,
        )

        process_panel = next(
            element
            for element in card["body"]["elements"]
            if isinstance(element, dict)
            and element.get("tag") == "collapsible_panel"
            and element.get("header", {}).get("title", {}).get("content") == "执行过程"
        )
        self.assertFalse(process_panel["expanded"])

    def test_agent_message_completed_without_delta_preserves_divider_after_work(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler._handle_agent_message_delta({"threadId": "thread-created", "turnId": "turn-1", "delta": "第一段"})
        handler._handle_item_started(
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "command": "ls", "cwd": "/tmp/project"},
            }
        )
        handler._handle_item_completed(
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "status": "completed", "exitCode": 0},
            }
        )
        handler._handle_item_completed(
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "第二段"},
            }
        )
        handler._flush_execution_card("ou_user", "c1", immediate=True)

        patched = json.loads(bot.patches[-1][1])
        reply_panel = next(
            element
            for element in patched["body"]["elements"]
            if isinstance(element, dict)
            and element.get("tag") == "collapsible_panel"
            and element.get("header", {}).get("title", {}).get("content") == "回复"
        )
        self.assertEqual(
            [element["tag"] for element in reply_panel["elements"]],
            ["markdown", "hr", "markdown"],
        )

    def test_whoami_command_in_p2p_returns_identity_and_admin_config_hint(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_user_id": "u2",
            "sender_open_id": "ou_user",
            "sender_type": "user",
        }

        handler.handle_message("ou_user", "chat-p2p", "/whoami", message_id="m-p2p")

        reply = bot.replies[-1][1]
        self.assertIn("name: `User`", reply)
        self.assertIn("user_id: `u2`", reply)
        self.assertIn("open_id: `ou_user`", reply)
        self.assertIn("admin_open_ids", reply)

    def test_whoami_command_in_group_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user2", "chat-group", "/whoami", message_id="m-group")

        self.assertIn("请私聊机器人执行", bot.replies[-1][1])

    def test_bot_status_command_returns_bot_identity(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "ou_bot",
            "discovered_open_id": "ou_bot",
            "trigger_open_ids": ["ou_alias_1", "ou_alias_2"],
        }

        handler.handle_message("ou_user", "chat-p2p", "/bot-status")

        reply = bot.replies[-1][1]
        self.assertIn("机器人身份信息", reply)
        self.assertIn("app_id: `cli_test_app`", reply)
        self.assertIn("configured bot_open_id: `ou_bot`", reply)
        self.assertIn("discovered open_id: `ou_bot`", reply)
        self.assertIn("runtime mention matching: `enabled`", reply)
        self.assertIn("trigger_open_ids: `ou_alias_1, ou_alias_2`", reply)
        self.assertIn("system.yaml.bot_open_id", reply)

    def test_bot_status_reports_missing_bot_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "",
            "discovered_open_id": "",
            "trigger_open_ids": [],
        }

        handler.handle_message("ou_user", "chat-p2p", "/bot-status")

        reply = bot.replies[-1][1]
        self.assertIn("configured bot_open_id: `（空）`", reply)
        self.assertIn("discovered open_id: `（空）`", reply)
        self.assertIn("runtime mention matching: `disabled`", reply)
        self.assertIn("application:application:self_manage", reply)

    def test_debug_contact_command_in_p2p_returns_resolution_diagnostics(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {"chat_type": "p2p", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user", "chat-p2p", "/debug-contact ou_user", message_id="m-p2p")

        reply = bot.replies[-1][1]
        self.assertIn("联系人解析诊断", reply)
        self.assertIn("open_id: `ou_user`", reply)
        self.assertIn("cache: `hit`", reply)
        self.assertIn("resolved_name: `User`", reply)

    def test_debug_contact_command_in_group_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_admin", "chat-group", "/debug-contact ou_user", message_id="m-group")

        self.assertIn(f"请私聊机器人执行 `{_DISPLAY_DEBUG_CONTACT_COMMAND}`", bot.replies[-1][1])

    def test_init_command_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/init abc", message_id="m-group")

        self.assertIn(f"请私聊机器人执行 `{_DISPLAY_INIT_COMMAND}`", bot.replies[-1][1])

    def test_init_command_with_token_updates_admin_and_bot_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user2",
            "sender_type": "user",
        }
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "open_id": "ou_bot_new",
            "source": "auto-discovered",
            "configured_open_id": "",
            "discovered_open_id": "ou_bot_new",
            "trigger_open_ids": "",
        }
        with patch("bot.codex_settings_domain.ensure_init_token", return_value="secret-1"), patch(
            "bot.codex_settings_domain.load_system_config_raw",
            return_value={
                "app_id": "cli_test_app",
                "app_secret": "secret",
                "admin_open_ids": ["ou_admin"],
            },
        ), patch("bot.codex_settings_domain.save_system_config") as save_config:
            handler.handle_message("ou_user2", "chat-p2p", "/init secret-1", message_id="m-p2p")

        saved = save_config.call_args.args[0]
        self.assertEqual(saved["admin_open_ids"], ["ou_admin", "ou_user2"])
        self.assertEqual(saved["bot_open_id"], "ou_bot_new")
        self.assertIn("ou_user2", bot.admin_open_ids)
        self.assertEqual(bot.runtime_bot_open_id, "ou_bot_new")
        reply = bot.replies[-1][1]
        self.assertIn("初始化结果", reply)
        self.assertIn("已加入 `Alice`", reply)
        self.assertIn("`ou_bot_new`", reply)

    def test_init_command_does_not_write_runtime_only_admins_back_to_config(self) -> None:
        handler, bot = self._make_handler()
        bot.admin_open_ids = {"ou_admin", "ou_stale_runtime"}
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user2",
            "sender_type": "user",
        }
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "open_id": "",
            "source": "auto-discovered",
            "configured_open_id": "",
            "discovered_open_id": "",
            "trigger_open_ids": "",
        }
        with patch("bot.codex_settings_domain.ensure_init_token", return_value="secret-1"), patch(
            "bot.codex_settings_domain.load_system_config_raw",
            return_value={
                "app_id": "cli_test_app",
                "app_secret": "secret",
                "admin_open_ids": ["ou_admin"],
            },
        ), patch("bot.codex_settings_domain.save_system_config") as save_config:
            handler.handle_message("ou_user2", "chat-p2p", "/init secret-1", message_id="m-p2p")

        saved = save_config.call_args.args[0]
        self.assertEqual(saved["admin_open_ids"], ["ou_admin", "ou_user2"])
        self.assertNotIn("ou_stale_runtime", saved["admin_open_ids"])

    def test_init_command_rejects_invalid_token(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user",
            "sender_type": "user",
        }
        with patch("bot.codex_settings_domain.ensure_init_token", return_value="secret-1"):
            handler.handle_message("ou_user", "chat-p2p", "/init bad-token", message_id="m-p2p")

        self.assertIn("初始化口令错误", bot.replies[-1][1])

    def test_group_mode_command_without_arg_shows_group_mode_card(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group-mode", message_id="m-group")

        card = bot.cards[-1][1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊工作态")
        action_elements = self._action_elements(card)
        actions = action_elements[0]["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["assistant", "all", "mention-only"])
        self.assertEqual(actions[0]["type"], "primary")
        self.assertEqual(action_elements[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_group_mode_command_can_use_cached_chat_type_without_message_context(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-group"] = {"sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group-mode", message_id="m-group")

        self.assertEqual(bot.cards[-1][1]["header"]["title"]["content"], "Codex 群聊工作态")

    def test_group_mode_command_updates_group_mode_for_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group-mode assistant", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertIn("已切换群聊工作态：`assistant`", bot.replies[-1][1])

    def test_group_mode_command_uses_sender_id_fallback_when_message_context_lacks_sender_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group"}

        handler.handle_message("ou_admin", "chat-group", "/group-mode all", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "all")
        self.assertIn("已切换群聊工作态：`all`", bot.replies[-1][1])

    def test_group_mode_command_rejects_all_when_thread_is_shared(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.chat_types["chat-other"] = "group"
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "chat-group", thread)
        handler._bind_thread("ou_user2", "chat-other", thread)

        handler.handle_message("ou_user", "chat-group", "/group-mode all", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertIn("`all` 模式", bot.replies[-1][1])
        self.assertIn("不能与其他飞书会话共享", bot.replies[-1][1])
        self.assertIn("/new", bot.replies[-1][1])
        self.assertIn("/cd <目录>", bot.replies[-1][1])

    def test_group_mode_command_rejects_non_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/group-mode all", message_id="m-group")

        self.assertIn("群里的 `/` 命令仅管理员可用", bot.replies[-1][1])
        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")

    def test_group_command_without_arg_shows_group_activation_card(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group", message_id="m-group")

        card = bot.cards[-1][1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊授权")
        markdown = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("未激活", markdown)
        self.assertIn("/group activate", markdown)

    def test_group_command_activates_group_chat(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group activate", message_id="m-group")

        snapshot = bot.get_group_activation_snapshot("chat-group")
        self.assertTrue(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "ou_admin")
        self.assertIn("已激活当前群聊", bot.replies[-1][1])

    def test_group_command_uses_sender_id_fallback_for_activation_actor(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group"}

        handler.handle_message("ou_admin", "chat-group", "/group activate", message_id="m-group")

        snapshot = bot.get_group_activation_snapshot("chat-group")
        self.assertTrue(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "ou_admin")

    def test_group_mode_card_action_updates_group_mode(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_mode", "mode": "assistant", "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(handler.bot.get_group_mode("chat-group"), "assistant")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("assistant", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊工作态")
        self.assertEqual(self._action_elements(response["card"])[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_group_mode_card_action_rejects_all_when_thread_is_shared(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.chat_types["chat-other"] = "group"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "chat-group", thread)
        handler._bind_thread("ou_user2", "chat-other", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_mode", "mode": "all", "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "切换失败；已发送处理建议。")
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊工作态")
        self.assertIn("切换到 `all` 失败", bot.reply_parents[-1][1])
        self.assertIn("/new", bot.reply_parents[-1][1])
        self.assertIn("/cd <目录>", bot.reply_parents[-1][1])
        self.assertEqual(bot.reply_parents[-1][2], "m1")

    def test_group_activation_card_action_updates_group_status(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_activation", "activated": True, "_operator_open_id": "ou_admin"},
        ))

        self.assertTrue(handler.bot.get_group_activation_snapshot("chat-group")["activated"])
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("已激活当前群聊", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊授权")
        markdown = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("/group activate", markdown)
        self.assertIn("/group deactivate", markdown)

    def test_group_command_accepts_group_chat_after_api_type_lookup(self) -> None:
        handler, bot = self._make_handler()
        bot.fetched_chat_types["oc_group123"] = "group"
        bot.message_contexts["m-group"] = {"sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "oc_group123", "/group-mode", message_id="m-group")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊工作态")

    def test_group_command_binds_shared_state_from_message_context_before_chat_cache(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-status"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/status", message_id="m-status")

        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertNotIn(("ou_user", "chat-group"), self._binding_keys(handler))
        self.assertIs(handler._get_runtime_state("ou_user", "chat-group"), handler._get_runtime_state("ou_user2", "chat-group"))

    def test_group_settings_card_action_uses_shared_chat_binding_key(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "chat-group",
                "m-group",
                {"action": "set_model", "model": "gpt-5.5", "_operator_open_id": "ou_admin"},
            )
        )

        self.assertEqual(handler._get_runtime_state("ou_user", "chat-group", "m-group")["model"], "gpt-5.5")
        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertNotIn(("ou_user", "chat-group"), self._binding_keys(handler))
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("gpt-5.5", response["toast"])

    def test_resolve_runtime_binding_reuses_existing_group_state(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        first = handler._resolve_runtime_binding("ou_user", "chat-group", "m-group")
        second = handler._resolve_runtime_binding("ou_user2", "chat-group")

        self.assertEqual(first.binding, ("__group__", "chat-group"))
        self.assertEqual(second.binding, ("__group__", "chat-group"))
        self.assertIs(first.state, second.state)

    def test_permissions_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions danger-full-access")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertIn("Danger Full Access", bot.replies[-1][1])
        self.assertIn(":danger-full-access", bot.replies[-1][1])

    def test_permissions_command_without_arg_shows_permissions_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 权限基线")
        self.assertIn("它只决定执行边界", card["elements"][0]["content"])
        self.assertIn("审批策略请单独使用 `/approval`", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(len(action_elements), 2)
        self.assertEqual(action_elements[0]["layout"], "trisection")
        self.assertEqual(action_elements[1]["actions"][0]["text"]["content"], "返回帮助")

    def test_model_command_without_arg_shows_model_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/model")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 模型 / Effort")
        self.assertIn("model override: `auto`", card["elements"][0]["content"])
        self.assertIn("effort override: `auto`", card["elements"][0]["content"])
        self.assertIn("validation: `validated`", card["elements"][0]["content"])
        self.assertNotIn("startup profile", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(action_elements[0]["actions"][0]["text"]["content"], "auto")
        self.assertEqual(action_elements[1]["actions"][0]["text"]["content"], "auto")
        self.assertEqual(action_elements[1]["actions"][0]["type"], "primary")

    def test_effort_command_without_arg_shows_combined_runtime_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/effort")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 模型 / Effort")
        self.assertIn("effort override: `auto`", card["elements"][0]["content"])

    def test_approval_command_without_arg_shows_approval_boundary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/approval")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 审批策略")
        self.assertIn("只决定什么时候停下来等你确认", card["elements"][0]["content"])
        self.assertIn("优先使用 `/permissions`", card["elements"][0]["content"])

    def test_help_execute_approval_action_adds_return_help_and_preserves_it_after_toggle(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "help_execute_command", "command": "/approval", "title": "Codex 审批策略"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 审批策略")
        action_elements = self._action_elements(response["card"])
        self.assertEqual(action_elements[-1]["actions"][0]["text"]["content"], "返回帮助")
        policy_action = action_elements[0]["actions"][0]["value"]
        self.assertEqual(policy_action["help_origin"], "overview")

        updated = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            policy_action,
        ))

        self.assertEqual(updated["card"]["header"]["title"]["content"], "Codex 审批策略")
        self.assertEqual(self._action_elements(updated["card"])[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_show_help_page_action_ignores_help_origin_redecoration(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "overview", "help_origin": "overview"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台")
        self.assertEqual(len(self._action_elements(response["card"])), 3)

    def test_model_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_model", "model": "gpt-5.4"},
        ))

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["model"], "gpt-5.4")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("gpt-5.4", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_model_form_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "submit_model_override", "_form_value": {"model_override": "glm-4.5"}},
        ))

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["model"], "glm-4.5")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("glm-4.5", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_model_form_value_only_callback_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"_form_value": {"model_override": "glm-4.5"}},
        ))

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["model"], "glm-4.5")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("glm-4.5", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_effort_form_value_only_callback_updates_state_and_preserves_case(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"_form_value": {"reasoning_effort_override": "Future-Max"}},
        ))

        self.assertEqual(
            handler._get_runtime_state("ou_user", "c1")["reasoning_effort"],
            "Future-Max",
        )
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("Future-Max", response["toast"])
        self.assertIn("validation: deferred", response["toast"])

    def test_effort_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_reasoning_effort", "reasoning_effort": "high"},
        ))

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["reasoning_effort"], "high")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("high", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_stale_effort_card_action_revalidates_against_current_model(self) -> None:
        handler, _ = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["model"] = "gpt-5.4"
        state["reasoning_effort"] = "high"

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_reasoning_effort", "reasoning_effort": "ultra"},
        ))

        self.assertEqual(state["reasoning_effort"], "high")
        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("未保存", response["toast"])
        self.assertIn("validation: `validated`", response["card"]["elements"][0]["content"])

    def test_permissions_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_permissions_profile", "profile": "danger-full-access"},
        ))

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("Danger Full Access", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 权限基线")
        self.assertEqual(self._action_elements(response["card"])[1]["actions"][0]["text"]["content"], "返回帮助")

    def test_turn_plan_updated_sends_then_patches_plan_card(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        with handler._lock:
            state["current_message_id"] = "exec-1"
            state["current_turn_id"] = "turn-1"

        handler._handle_turn_plan_updated(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "explanation": "先规划再执行。",
                "plan": [{"step": "确认需求", "status": "pending"}],
            }
        )

        self.assertEqual(len(bot.reply_refs), 1)
        first_card = json.loads(bot.reply_refs[0][2])
        self.assertEqual(first_card["header"]["title"]["content"], "Codex 计划 turn-1…")
        self.assertTrue(
            any("确认需求" in element.get("content", "") for element in first_card["elements"])
        )

        handler._handle_turn_plan_updated(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "explanation": "先规划再执行。",
                "plan": [{"step": "确认需求", "status": "completed"}],
            }
        )

        self.assertEqual(len(bot.patches), 1)
        patched_card = json.loads(bot.patches[0][1])
        self.assertTrue(
            any("[x] 确认需求" in element.get("content", "") for element in patched_card["elements"])
        )

    def test_plan_item_completion_sends_plan_card(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        with handler._lock:
            state["current_message_id"] = "exec-1"
            state["current_turn_id"] = "turn-1"

        handler._handle_item_completed(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "plan", "text": "1. 先确认需求\n2. 再实现"},
            }
        )

        self.assertEqual(len(bot.reply_refs), 1)
        card = json.loads(bot.reply_refs[0][2])
        self.assertIn("计划正文", card["elements"][0]["content"])
        self.assertIn("先确认需求", card["elements"][0]["content"])

    def test_custom_user_input_is_hidden_for_option_only_questions(self) -> None:
        card = build_ask_user_card(
            "req-1",
            [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}, {"label": "暂缓步骤", "description": ""}],
                    "isOther": False,
                }
            ],
        )

        self.assertFalse(any(element.get("tag") == "form" for element in card["elements"]))

    def test_execution_card_uses_process_title_without_help_hint(self) -> None:
        card = build_execution_card("", [], running=True)

        self.assertEqual(card["header"]["title"]["content"], "Codex 执行过程（执行中）")
        self.assertNotIn("/help", json.dumps(card, ensure_ascii=False))

    def test_terminal_empty_execution_card_shows_minimal_placeholder(self) -> None:
        card = build_execution_card("", [], running=False)

        self.assertEqual(card["header"]["title"]["content"], "Codex 执行过程")
        self.assertEqual(card["body"]["elements"], [{"tag": "markdown", "content": "无"}])

    def test_execution_card_sanitizes_embedded_image_markdown_in_runtime_text(self) -> None:
        card = build_execution_card(
            "命令输出：![日志图](/tmp/log.png)",
            [ExecutionReplySegment("assistant", "![示意图](/tmp/demo.png)\n\n已生成。")],
            running=False,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("![示意图](/tmp/demo.png)", card_json)
        self.assertNotIn("![日志图](/tmp/log.png)", card_json)
        self.assertIn("【图片】示意图", card_json)
        self.assertIn("路径：`/tmp/demo.png`", card_json)
        self.assertIn("路径：`/tmp/log.png`", card_json)

    def test_execution_card_sanitizes_markdown_links_to_visible_urls(self) -> None:
        card = build_execution_card(
            "参考：[示例地图链接](https://maps.example.invalid/shanghai/live)",
            [
                ExecutionReplySegment(
                    "assistant",
                    "[示例扩散条件图](https://weather.example.invalid/china/dispersion-24h)",
                )
            ],
            running=False,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("[示例地图链接](", card_json)
        self.assertNotIn("[示例扩散条件图](", card_json)
        self.assertIn("示例地图链接 (https://maps.example.invalid/shanghai/live)", card_json)
        self.assertIn(
            "示例扩散条件图 (https://weather.example.invalid/china/dispersion-24h)",
            card_json,
        )

    def test_execution_card_sanitizes_markdown_headings_to_visible_labels(self) -> None:
        card = build_execution_card(
            "# 过程标题",
            [ExecutionReplySegment("assistant", "## 回复小节\n\n- 条目")],
            running=False,
        )

        card_json = json.dumps(card, ensure_ascii=False)
        self.assertIn("【标题】 过程标题", card_json)
        self.assertIn("【小节】 回复小节", card_json)
        self.assertNotIn("# 过程标题", card_json)
        self.assertNotIn("## 回复小节", card_json)

    def test_status_includes_user_facing_summary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/status")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 当前状态")
        content = card["elements"][0]["content"]
        self.assertIn("权限基线：`Danger Full Access`", content)
        self.assertIn("审批策略：`never`", content)
        self.assertNotIn("Codex 协作模式", content)
        self.assertIn("Codex effort override：`auto`", content)
        self.assertNotIn("新 thread seed profile", content)
        self.assertNotIn("当前 provider", content)
        self.assertNotIn("binding：", content)

    def test_status_hides_runtime_debug_fields(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("权限基线：`Danger Full Access`", content)
        self.assertNotIn("Codex 协作模式", content)
        self.assertIn("Codex effort override：`auto`", content)
        self.assertNotIn("startup profile", content)
        self.assertNotIn("binding：", content)
        self.assertNotIn("feishu runtime：", content)
        self.assertNotIn("backend thread status：", content)
        self.assertNotIn("交互 owner：", content)
        self.assertNotIn("re-profile possible：", content)
        self.assertNotIn("unsubscribe：", content)
        self.assertNotIn("当前直接提问：", content)

    def test_bind_thread_backfills_goal_projection_from_backend(self) -> None:
        handler, _ = self._make_handler()
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        handler._bind_thread("ou_user", "c1", thread)

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["goal_objective"], "ship goal support")
        self.assertEqual(state["goal_status"], "active")
        self.assertEqual(state["goal_token_budget"], 100)

    def test_status_shows_goal_summary_when_available(self) -> None:
        handler, bot = self._make_handler()
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        handler._bind_thread("ou_user", "c1", thread)
        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("当前 goal：`active`", content)
        self.assertIn("goal 摘要：预算：`100`；已用 tokens：`12`；时长：`34s`", content)

    def test_goal_command_supports_show_set_pause_resume_and_clear(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        handler.handle_message("ou_user", "c1", "/goal")
        _, show_card = bot.cards[-1]
        self.assertIn("当前 thread 暂无 goal。", show_card["elements"][0]["content"])

        handler.handle_message("ou_user", "c1", "/goal set ship goal support")
        _, set_card = bot.cards[-1]
        self.assertIn("已设置当前 thread goal。", set_card["elements"][0]["content"])
        self.assertIn("目标：ship goal support", set_card["elements"][0]["content"])
        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["goal_objective"], "ship goal support")
        self.assertEqual(state["goal_status"], "active")

        card_count = len(bot.cards)
        handler.handle_message("ou_user", "c1", "/goal text")
        self.assertEqual(len(bot.cards), card_count)
        goal_text = bot.replies[-1][1]
        self.assertIn("thread: thread-1", goal_text)
        self.assertIn("title: demo", goal_text)
        self.assertIn("status: active", goal_text)
        self.assertIn("objective:\nship goal support", goal_text)

        handler.handle_message("ou_user", "c1", "/goal pause")
        _, pause_card = bot.cards[-1]
        self.assertIn("状态：`paused`", pause_card["elements"][0]["content"])
        self.assertEqual(state["goal_status"], "paused")

        pending_count = len(bot.cards)
        handler.handle_message("ou_user", "c1", "/goal resume")
        pending_cards = [
            card
            for _, card in bot.cards[pending_count:]
            if "正在同步 thread、goal 与当前会话设置" in card["elements"][0]["content"]
        ]
        self.assertTrue(pending_cards)
        handler._runtime_call(lambda: None)
        _, resume_card = bot.cards[-1]
        self.assertIn("状态：`active`", resume_card["elements"][0]["content"])
        self.assertEqual(state["goal_status"], "active")

        handler.handle_message("ou_user", "c1", "/goal clear")
        _, clear_card = bot.cards[-1]
        self.assertIn("已清除当前 thread goal。", clear_card["elements"][0]["content"])
        self.assertEqual(state["goal_objective"], "")
        self.assertEqual(state["goal_status"], "")

    def test_goal_card_action_can_pause_and_clear_goal(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._refresh_bound_thread_goal_projection("ou_user", "c1", "thread-1")

        pause_response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-goal", {"action": "goal_pause"})
        )
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "paused")
        self.assertEqual(pause_response["toast"], "已暂停 goal。")
        self.assertIn("状态：`paused`", pause_response["card"]["elements"][0]["content"])

        clear_response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-goal", {"action": "goal_clear"})
        )
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_objective"], "")
        self.assertEqual(clear_response["toast"], "已清除 goal。")
        self.assertIn("当前 thread 暂无 goal。", clear_response["card"]["elements"][0]["content"])

    def test_goal_set_detached_requires_confirm_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        handler.handle_message("ou_user", "c1", "/goal set ship goal support")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal")
        content = card["elements"][0]["content"]
        self.assertIn("当前会话处于 `detached`。", content)
        self.assertIn("目标：ship goal support", content)
        actions = self._first_action(card)["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["恢复推送并继续", "保持 detached"])
        self.assertNotIn("thread-1", handler._adapter.thread_goals)

    def test_goal_resume_detached_without_goal_fails_before_confirm_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        handler.handle_message("ou_user", "c1", "/goal resume")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal 操作失败")
        self.assertIn("当前 thread 没有可恢复的 goal。", card["elements"][0]["content"])

    def test_goal_resume_detached_with_goals_disabled_fails_before_confirm_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        handler.handle_message("ou_user", "c1", "/goal resume")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal 操作失败")
        self.assertIn("当前 backend 未启用 goal 功能。", card["elements"][0]["content"])

    def test_goal_resume_detached_confirm_can_keep_detached(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._refresh_bound_thread_goal_projection("ou_user", "c1", "thread-1")
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        confirm_response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-goal", {"action": "goal_resume"})
        )
        self.assertIn("当前会话处于 `detached`。", confirm_response["card"]["elements"][0]["content"])
        self.assertIn("状态：`active`", confirm_response["card"]["elements"][0]["content"])

        apply_response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-goal",
                {
                    "action": "goal_apply_confirm",
                    "status": "active",
                    "attach_binding": "",
                },
            )
        )
        self.assertIn("正在同步 thread、goal 与当前会话设置", apply_response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)
        _, final_card = handler.bot.cards[-1]
        self.assertIn("当前 thread goal。", final_card["elements"][0]["content"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "active")

    def test_goal_resume_cold_thread_injects_runtime_permissions_and_updates_loaded_settings(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = handler._get_runtime_state("ou_user", "c1")
        state["approval_policy"] = "on-request"
        state["permissions_profile_id"] = ":workspace"
        state["model"] = "gpt-5.4"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/goal resume")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": {"model_reasoning_effort": "high"},
                "model": "gpt-5.4",
                "model_provider": None,
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
            },
        )
        self.assertEqual(
            handler._adapter.update_thread_settings_calls[-1],
            {
                "thread_id": "thread-1",
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
                "model": "gpt-5.4",
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "active")
        self.assertIn("状态：`active`", bot.cards[-1][1]["elements"][0]["content"])

    def test_goal_resume_cold_active_goal_pauses_before_resume_then_reactivates(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = handler._get_runtime_state("ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/goal resume")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.set_thread_goal_calls[-2:],
            [
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "paused",
                    "token_budget": None,
                },
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "active",
                    "token_budget": None,
                },
            ],
        )
        self.assertEqual(
            handler._adapter.operation_log[-4:],
            [
                ("set_thread_goal", "thread-1", "paused"),
                ("resume_thread", "thread-1", "gpt-5.5"),
                ("update_thread_settings", "thread-1", "gpt-5.5"),
                ("set_thread_goal", "thread-1", "active"),
            ],
        )
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "active")

    def test_goal_resume_cold_active_goal_rolls_back_pause_on_failure(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._adapter.update_thread_settings = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sync failed"))

        handler.handle_message("ou_user", "c1", "/goal resume")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.set_thread_goal_calls[-2:],
            [
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "paused",
                    "token_budget": None,
                },
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "active",
                    "token_budget": None,
                },
            ],
        )
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "active")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "active")
        self.assertIn("sync failed", bot.cards[-1][1]["elements"][0]["content"])

    def test_goal_resume_fails_closed_when_goals_feature_disabled(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        pending_count = len(bot.cards)
        handler.handle_message("ou_user", "c1", "/goal resume")

        self.assertEqual(len(bot.cards), pending_count + 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Goal 操作失败")
        self.assertIn("当前 backend 未启用 goal 功能。", card["elements"][0]["content"])

    def test_goal_resume_card_action_acknowledges_immediately_then_attaches_in_background(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-goal",
                {
                    "action": "goal_apply_confirm",
                    "status": "active",
                    "attach_binding": "true",
                },
            )
        )

        self.assertIn("正在同步 thread、goal 与当前会话设置", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "active")

    def test_goal_apply_confirm_fast_ack_bypasses_busy_runtime_queue(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        blocker_started = threading.Event()
        blocker_release = threading.Event()
        handler._runtime_submit(
            lambda: (
                blocker_started.set(),
                blocker_release.wait(2),
            )
        )
        self.assertTrue(blocker_started.wait(1))

        response_holder: dict[str, dict] = {}

        def invoke() -> None:
            response_holder["response"] = self._unpack_card_response(
                handler.handle_card_action(
                    "ou_user",
                    "c1",
                    "msg-goal",
                    {
                        "action": "goal_apply_confirm",
                        "status": "active",
                        "attach_binding": "true",
                    },
                )
            )

        worker = threading.Thread(target=invoke)
        worker.start()
        worker.join(timeout=0.2)
        self.assertFalse(worker.is_alive())
        self.assertIn(
            "正在同步 thread、goal 与当前会话设置",
            response_holder["response"]["card"]["elements"][0]["content"],
        )
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")

        blocker_release.set()
        worker.join(timeout=1)
        handler._runtime_call(lambda: None)

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_status"], "active")

    def test_goal_set_detached_confirm_can_attach_before_apply(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        apply_response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-goal",
                {
                    "action": "goal_apply_confirm",
                    "objective": "ship goal support",
                    "attach_binding": "true",
                },
            )
        )
        self.assertEqual(apply_response["toast"], "已更新 goal 并恢复当前会话推送。")
        self.assertIn("当前会话已恢复接收该 thread 的飞书推送。", apply_response["card"]["elements"][0]["content"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["goal_objective"], "ship goal support")

    def test_detach_command_detaches_current_binding_and_keeps_other_binding_attached(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        unloaded = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="notLoaded",
        )
        handler._bind_thread("ou_user", "chat-a", thread)
        handler._bind_thread("ou_user2", "chat-b", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "chat-a", "/detach")

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertEqual(handler._get_runtime_state("ou_user", "chat-a")["current_thread_id"], "thread-1")
        self.assertEqual(handler._get_runtime_state("ou_user2", "chat-b")["current_thread_id"], "thread-1")
        self.assertEqual(handler._get_runtime_state("ou_user", "chat-a")["feishu_runtime_state"], "detached")
        self.assertEqual(handler._get_runtime_state("ou_user2", "chat-b")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._thread_subscribers("thread-1"), (("ou_user2", "chat-b"),))
        _, card = bot.cards[-1]
        self.assertIn("backend thread status：`idle`", card["elements"][0]["content"])

    def test_detached_binding_hydrates_without_resubscribe_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        unloaded = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="notLoaded",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        def _unsubscribe(thread_id: str) -> None:
            handler._adapter.unsubscribe_thread_calls.append(thread_id)
            handler._adapter.thread_snapshots[(thread_id, None)] = ThreadSnapshot(summary=unloaded)

        handler._adapter.unsubscribe_thread = _unsubscribe
        handler._detach_thread("thread-1")

        handler2, _ = self._make_handler(data_dir=data_dir)
        state2 = handler2._get_runtime_state("ou_user", "c1")
        self.assertEqual(state2["current_thread_id"], "thread-1")
        self.assertEqual(state2["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._thread_subscribers("thread-1"), ())

        handler2.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler2._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_attach_command_resumes_loaded_thread_to_restore_service_subscription(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        handler._detach_thread("thread-1")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertEqual(handler._thread_subscribers("thread-1"), ())

        handler.handle_message("ou_user", "c1", "/attach")

        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._thread_subscribers("thread-1"), (("ou_user", "c1"),))

    def test_persisted_attached_binding_hydrates_as_detached_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        handler2, _ = self._make_handler(data_dir=data_dir)
        state2 = handler2._get_runtime_state("ou_user", "c1")
        self.assertEqual(state2["current_thread_id"], "thread-1")
        self.assertEqual(state2["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._thread_subscribers("thread-1"), ())

        handler2.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler2._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_next_prompt_rejects_when_other_running_instance_still_reports_loaded(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        handler._detach_thread("thread-1")

        with patch(
            "bot.codex_handler.preview_thread_global_loaded_gate",
            return_value=SimpleNamespace(
                allowed=False,
                reason_code="prompt_denied_by_live_runtime_owner",
                reason_text=(
                    "当前 thread 仍由运行中的实例 `explorer` 保持为 loaded (`idle`)；"
                    "当前按 fail-close 拒绝跨实例继续。"
                ),
                blocking_instance="explorer",
                blocking_status="idle",
            ),
        ):
            handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(handler._thread_subscribers("thread-1"), ())
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertIn("拒绝跨实例继续", bot.replies[-1][1])

    def test_denied_prompt_keeps_detached_binding_detached_when_all_mode_group_owns_thread(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-a"] = "group"
        bot.chat_types["chat-b"] = "group"
        bot.group_modes["chat-b"] = "all"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        handler._bind_thread("ou_user", "chat-a", thread)
        handler._detach_thread("thread-1")
        handler._bind_thread("ou_user2", "chat-b", thread)

        handler.handle_message("ou_user", "chat-a", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(
            handler._get_runtime_state("ou_user", "chat-a")["feishu_runtime_state"],
            "detached",
        )
        self.assertEqual(handler._thread_subscribers("thread-1"), (("__group__", "chat-b"),))
        self.assertIn("其他群聊独占", bot.replies[-1][1])

    def test_denied_prompt_keeps_detached_binding_detached_when_interaction_lease_is_external(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        handler._bind_thread("ou_user", "c1", thread)
        handler._detach_thread("thread-1")
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:other", owner_pid=os.getpid()),
        )

        handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(handler._thread_subscribers("thread-1"), ())
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertEqual(InteractionLeaseStore(data_dir).load("thread-1").holder.kind, "fcodex")
        self.assertIn("当前线程正由另一终端执行", bot.replies[-1][1])

    def test_service_control_plane_releases_runtime_via_running_service(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        unloaded = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="notLoaded",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        def _unsubscribe(thread_id: str) -> None:
            handler._adapter.unsubscribe_thread_calls.append(thread_id)
            handler._adapter.thread_snapshots[(thread_id, None)] = ThreadSnapshot(summary=unloaded)

        handler._adapter.unsubscribe_thread = _unsubscribe

        status = control_request(data_dir, "service/status")
        result = control_request(data_dir, "thread/detach", {"thread_id": "thread-1"})

        self.assertEqual(status["binding_count"], 1)
        self.assertTrue(status["control_endpoint"].startswith("tcp://127.0.0.1:"))
        self.assertTrue(result["changed"])
        self.assertEqual(result["backend_thread_status"], "notLoaded")
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")

    def test_service_control_plane_thread_name_target_resolves_explicit_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        status = control_request(data_dir, "thread/status", {"thread_name": "demo"})

        self.assertEqual(status["thread_id"], "thread-1")
        self.assertEqual(status["thread_title"], "demo")

    def test_service_control_plane_thread_bindings_name_target_resolves_explicit_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        result = control_request(data_dir, "thread/bindings", {"thread_name": "demo"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["thread_title"], "demo")
        self.assertEqual(
            result["bindings"],
            [{"binding_id": "p2p:ou_user:c1", "feishu_runtime_state": "attached"}],
        )

    def test_service_control_plane_thread_status_thread_id_accepts_not_loaded_thread(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )

        status = control_request(data_dir, "thread/status", {"thread_id": "thread-1"})

        self.assertEqual(status["thread_id"], "thread-1")
        self.assertEqual(status["backend_thread_status"], "notLoaded")
        self.assertEqual(status["thread_title"], "demo")
        self.assertEqual(status["working_dir"], "/tmp/project")

    def test_service_control_plane_thread_bindings_thread_id_accepts_not_loaded_thread(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )

        result = control_request(data_dir, "thread/bindings", {"thread_id": "thread-1"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["thread_title"], "demo")
        self.assertEqual(result["bindings"], [{"binding_id": "p2p:ou_user:c1", "feishu_runtime_state": "attached"}])

    def test_service_control_plane_binding_list_uses_group_chat_display_name_cache(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        bot.chat_types["oc_group"] = "group"
        bot.chat_display_names["oc_group"] = "Project Group"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "oc_group", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        result = control_request(data_dir, "binding/list")

        self.assertEqual(result["chat_display_name_cache_miss_count"], 0)
        self.assertEqual(result["bindings"][0]["binding_id"], "group:oc_group")
        self.assertEqual(result["bindings"][0]["chat_display_name"], "Project Group")
        self.assertEqual(bot.refreshed_chat_display_names, [])

    def test_service_control_plane_binding_list_refreshes_group_chat_display_name_on_request(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        bot.chat_types["oc_group"] = "group"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "oc_group", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        bot.chat_display_names["oc_group"] = "Project Group"

        result = control_request(data_dir, "binding/list", {"refresh_names": True})

        self.assertEqual(result["chat_display_name_cache_miss_count"], 0)
        self.assertEqual(result["bindings"][0]["binding_id"], "group:oc_group")
        self.assertEqual(result["bindings"][0]["chat_display_name"], "Project Group")
        self.assertEqual(bot.refreshed_chat_display_names, ["oc_group"])

    def test_service_control_plane_binding_clear_removes_runtime_state_and_persistence(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        result = control_request(data_dir, "binding/clear", {"binding_id": "p2p:ou_user:c1"})

        self.assertTrue(result["cleared"])
        self.assertEqual(result["binding_id"], "p2p:ou_user:c1")
        self.assertNotIn(("ou_user", "c1"), self._binding_keys(handler))
        self.assertEqual(handler._thread_subscribers("thread-1"), ())
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertIsNone(handler._chat_binding_store.load(("ou_user", "c1")))

    def test_service_control_plane_binding_clear_all_removes_all_bindings(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread_a = ThreadSummary(
            thread_id="thread-a",
            cwd="/tmp/project-a",
            name="demo-a",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        thread_b = ThreadSummary(
            thread_id="thread-b",
            cwd="/tmp/project-b",
            name="demo-b",
            preview="world",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread_a)
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
        handler._bind_thread("ou_admin", "chat-group", thread_b, message_id="m-group")

        result = control_request(data_dir, "binding/clear-all")

        self.assertFalse(result["already_empty"])
        self.assertEqual(
            result["cleared_binding_ids"],
            ["group:chat-group", "p2p:ou_user:c1"],
        )
        self.assertEqual(self._binding_keys(handler), ())
        self.assertEqual(sorted(handler._adapter.unsubscribe_thread_calls), ["thread-a", "thread-b"])
        self.assertEqual(handler._chat_binding_store.load_all(), {})

    def test_service_control_plane_binding_submit_prompt_starts_synthetic_turn(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
                "synthetic_source": "schedule",
            },
        )

        self.assertTrue(result["started"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "继续分析")
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_binding_submit_prompt_rejects_missing_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_typo:chat-typo",
                "text": "继续分析",
            },
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "prompt_denied_binding_not_found")
        self.assertEqual(result["reason"], "未找到 binding：p2p:ou_typo:chat-typo")
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies, [])
        self.assertEqual(self._binding_keys(handler), ())

    def test_service_control_plane_binding_submit_prompt_announces_only_after_successful_start(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
                "synthetic_source": "schedule",
                "display_mode": "announce",
            },
        )

        self.assertTrue(result["started"])
        self.assertEqual(bot.replies, [("c1", "schedule触发，开始新一轮执行。")])

    def test_service_control_plane_binding_submit_prompt_queues_without_chat_reply(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
            },
        )

        self.assertFalse(result["started"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["queue_position"], 1)
        self.assertEqual(result["reason_code"], "")
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_group_binding_submit_prompt_queues_different_running_actor(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        bot.chat_types["chat-group"] = "group"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("__group__", "chat-group", thread)
        state = handler._get_runtime_state("__group__", "chat-group")
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["current_actor_open_id"] = "ou_actor_1"

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "group:chat-group",
                "text": "继续分析",
                "actor_open_id": "ou_actor_2",
            },
        )

        self.assertFalse(result["started"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["queue_position"], 1)
        self.assertEqual(result["reason_code"], "")
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_binding_submit_prompt_announce_does_not_reply_when_start_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._prompt_turn_entry.start_prompt_turn_result = lambda *_args, **_kwargs: SimpleNamespace(
            started=False,
            thread_id="thread-1",
            turn_id="",
            reason_code="execution_card_send_failed",
            reason_text="execution card failed",
        )

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
                "synthetic_source": "schedule",
                "display_mode": "announce",
            },
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "execution_card_send_failed")
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_binding_clear_rejects_when_binding_has_pending_request(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "title": "Codex 命令执行审批",
            "message_id": "msg-1",
            "questions": [],
            "answers": {},
            "chat_id": "c1",
            "sender_id": "ou_user",
            "actor_open_id": "ou_user",
            "status": "pending",
        })

        with self.assertRaisesRegex(ServiceControlError, "不能清除 binding"):
            control_request(data_dir, "binding/clear", {"binding_id": "p2p:ou_user:c1"})

        self.assertIn(("ou_user", "c1"), self._binding_keys(handler))

    def test_service_control_plane_detach_name_target_resolves_explicit_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        unloaded = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="notLoaded",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        def _unsubscribe(thread_id: str) -> None:
            handler._adapter.unsubscribe_thread_calls.append(thread_id)
            handler._adapter.thread_snapshots[(thread_id, None)] = ThreadSnapshot(summary=unloaded)

        handler._adapter.unsubscribe_thread = _unsubscribe

        result = control_request(data_dir, "thread/detach", {"thread_name": "demo"})

        self.assertTrue(result["changed"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["backend_thread_status"], "notLoaded")
        self.assertEqual(result["detached_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "detached")

    def test_service_control_plane_thread_name_target_rejects_ambiguous_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-a",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=2,
            source="appServer",
            status="idle",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-b",
            name="demo",
            preview="world",
            created_at=0,
            updated_at=1,
            source="appServer",
            status="idle",
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread_1)
        handler._adapter.thread_snapshots[("thread-2", None)] = ThreadSnapshot(summary=thread_2)

        with self.assertRaisesRegex(ServiceControlError, "匹配到多个同名线程"):
            control_request(data_dir, "thread/status", {"thread_name": "demo"})

    def test_service_control_plane_thread_target_requires_exactly_one_selector(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        handler.on_register(bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        with self.assertRaises(ServiceControlError):
            control_request(data_dir, "thread/status", {})
        with self.assertRaises(ServiceControlError):
            control_request(
                data_dir,
                "thread/status",
                {"thread_id": "thread-1", "thread_name": "demo"},
            )

    def test_archive_command_archives_current_thread_and_clears_binding(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/archive")

        self.assertEqual(handler._adapter.archive_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")
        self.assertIn("不是硬删除", bot.replies[-1][1])
        self.assertIn("已同步清理当前实例里仍指向该 thread 的 bindings：`1` 个。", bot.replies[-1][1])

    def test_archive_command_rejects_when_other_binding_has_pending_request(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._bind_thread("ou_other", "c2", thread)
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "thread_id": "thread-1",
            "sender_id": "ou_other",
            "chat_id": "c2",
            "status": "pending",
        })

        handler.handle_message("ou_user", "c1", "/archive")

        self.assertEqual(handler._adapter.archive_thread_calls, [])
        self.assertIn("待处理审批或补充输入", bot.replies[-1][1])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "thread-1")

    def test_archive_command_rejects_when_live_runtime_owner_is_other_instance(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._runtime_admin._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="explorer",
            owner_service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(),
        )

        handler.handle_message("ou_user", "c1", "/archive")

        self.assertEqual(handler._adapter.archive_thread_calls, [])
        self.assertIn("explorer", bot.replies[-1][1])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "thread-1")

    def test_status_hides_removed_new_thread_seed_profile_row(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        self.assertNotIn("新 thread seed profile", card["elements"][0]["content"])

    def test_new_thread_uses_current_runtime_overrides_without_profile_injection(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/new")

        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model"])

    def test_new_thread_after_model_auto_does_not_fallback_to_configured_model(self) -> None:
        handler, _ = self._make_handler({"model": "gpt-5.5"})

        handler.handle_message("ou_user", "c1", "/model auto")
        handler.handle_message("ou_user", "c1", "/new")

        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model"])
        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model_provider"])

    def test_new_thread_reports_bind_failure_instead_of_silently_dropping_command(self) -> None:
        handler, bot = self._make_handler()

        with patch.object(handler, "_bind_thread", side_effect=RuntimeError("bind failed")):
            handler.handle_message("ou_user", "c1", "/new")

        self.assertIn("新建线程失败：bind failed", bot.replies[-1][1])
        self.assertEqual(handler._adapter.unsubscribe_thread_calls[-1], "thread-created")

    def test_new_thread_failure_rolls_back_existing_binding(self) -> None:
        handler, bot = self._make_handler()
        old_thread = ThreadSummary(
            thread_id="thread-old",
            cwd="/tmp/project",
            name="old",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", old_thread)

        with patch.object(handler, "_clear_plan_state", side_effect=RuntimeError("clear plan failed")):
            handler.handle_message("ou_user", "c1", "/new")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertIn("新建线程失败：clear plan failed", bot.replies[-1][1])
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(state["current_thread_title"], "old")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(handler._thread_subscribers("thread-old"), (("ou_user", "c1"),))
        self.assertEqual(handler._thread_subscribers("thread-created"), ())
        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-created"), ())
        self.assertEqual(handler._adapter.unsubscribe_thread_calls[-1], "thread-created")

    def test_new_thread_failure_without_existing_binding_clears_new_thread_binding(self) -> None:
        handler, bot = self._make_handler()

        with patch.object(handler, "_clear_plan_state", side_effect=RuntimeError("clear plan failed")):
            handler.handle_message("ou_user", "c1", "/new")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertIn("新建线程失败：clear plan failed", bot.replies[-1][1])
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["feishu_runtime_state"], "")
        self.assertEqual(handler._thread_subscribers("thread-created"), ())
        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-created"), ())

    def test_prompt_starts_without_project_profile_override(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model"])
        self.assertIsNone(handler._adapter.start_turn_calls[-1]["model"])

    def test_prompt_without_configured_default_working_dir_uses_home_directory(self) -> None:
        with patch("bot.codex_handler.default_working_dir", return_value=pathlib.Path("/home/tester")):
            handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["cwd"], "/home/tester")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["cwd"], "/home/tester")

    def test_prompt_reuses_reserved_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.reserved_execution_cards["m1"] = "reserved-card"

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_message_id"], "reserved-card")
        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(bot.patches[-1][0], "reserved-card")

    def test_prompt_failure_patches_reserved_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.reserved_execution_cards["m1"] = "reserved-card"
        handler._adapter.create_thread = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        self.assertNotIn("m1", bot.reserved_execution_cards)
        self.assertEqual(bot.patches[-1][0], "reserved-card")
        self.assertIn("Codex 启动失败", bot.patches[-1][1])
        self.assertIn("准备线程失败：boom", bot.patches[-1][1])

    def test_concurrent_prompts_are_serialized_through_runtime_loop(self) -> None:
        handler, bot = self._make_handler()
        original_create_thread = handler._adapter.create_thread
        started = threading.Event()
        release = threading.Event()
        create_thread_calls = 0

        def blocking_create_thread(**kwargs):
            nonlocal create_thread_calls
            create_thread_calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=1))
            return original_create_thread(**kwargs)

        handler._adapter.create_thread = blocking_create_thread
        first = threading.Thread(target=handler.handle_message, args=("ou_user", "c1", "first"))
        second = threading.Thread(target=handler.handle_message, args=("ou_user", "c1", "second"))

        first.start()
        self.assertTrue(started.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(create_thread_calls, 1)
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：1"))

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_file_attachment_is_staged_and_consumed_by_next_prompt(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"spec-content",
            file_name="spec.pdf",
            content_type="application/pdf",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "spec.pdf")

        self.assertIn("已保存到本地", bot.replies[-1][1])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        staged_files = sorted((workspace / "_feishu_attachments").iterdir())
        self.assertEqual(len(staged_files), 1)
        self.assertEqual(staged_files[0].read_bytes(), b"spec-content")

        handler.handle_message("ou_user", "c1", "请阅读这个文件", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual(input_items[0]["type"], "text")
        self.assertIn(str(staged_files[0]), input_items[0]["text"])
        self.assertIn("spec.pdf", input_items[0]["text"])
        self.assertEqual(handler._pending_attachment_store.list_all(), ())

    def test_image_attachment_turn_includes_local_image_input(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-image"] = {"chat_type": "p2p", "message_type": "image"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-image", "image", "img-key")] = SimpleNamespace(
            content=b"\x89PNG\r\n\x1a\npng",
            file_name="diagram.png",
            content_type="image/png",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-image", "image", "img-key", "")
        handler.handle_message("ou_user", "c1", "请解释这张图", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual([item["type"] for item in input_items], ["text", "localImage"])
        self.assertTrue(input_items[1]["path"].endswith(".png"))
        self.assertIn(input_items[1]["path"], input_items[0]["text"])

    def test_missing_staged_attachment_blocks_entire_attachment_batch(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file-1"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-file-2"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file-1", "file", "file-key-1")] = SimpleNamespace(
            content=b"one",
            file_name="one.txt",
            content_type="text/plain",
        )
        bot.downloaded_resources[("m-file-2", "file", "file-key-2")] = SimpleNamespace(
            content=b"two",
            file_name="two.txt",
            content_type="text/plain",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file-1", "file", "file-key-1", "one.txt")
        handler.handle_attachment_message("ou_user", "c1", "m-file-2", "file", "file-key-2", "two.txt")

        staged_files = sorted((workspace / "_feishu_attachments").iterdir())
        staged_files[0].unlink()

        handler.handle_message("ou_user", "c1", "请处理附件", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIn("重新发送需要处理的全部附件", bot.replies[-1][1])
        self.assertEqual(handler._pending_attachment_store.list_all(), ())

    def test_workspace_mismatch_blocks_attachment_batch(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace-1"
        workspace_2 = pathlib.Path(tempdir.name) / "workspace-2"
        workspace.mkdir()
        workspace_2.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"one",
            file_name="one.txt",
            content_type="text/plain",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "one.txt")

        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["working_dir"] = str(workspace_2)

        handler.handle_message("ou_user", "c1", "请处理附件", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIn("属于其他工作目录", bot.replies[-1][1])
        self.assertEqual(handler._pending_attachment_store.list_all(), ())

    def test_group_attachment_pending_is_isolated_by_sender(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["g-file"] = {"chat_type": "group", "message_type": "file", "sender_open_id": "ou_user"}
        bot.message_contexts["g-text-b"] = {"chat_type": "group", "message_type": "text", "sender_open_id": "ou_user2"}
        bot.message_contexts["g-text-a"] = {"chat_type": "group", "message_type": "text", "sender_open_id": "ou_user"}
        bot.downloaded_resources[("g-file", "file", "file-key")] = SimpleNamespace(
            content=b"group-file",
            file_name="group.txt",
            content_type="text/plain",
        )

        handler.handle_attachment_message("ou_user", "chat-group", "g-file", "file", "file-key", "group.txt")
        handler.handle_message("ou_user2", "chat-group", "普通提问", message_id="g-text-b")

        self.assertNotIn("group.txt", handler._adapter.start_turn_calls[-1]["text"])
        state = handler._get_runtime_state("ou_user", "chat-group", "g-text-a")
        with handler._lock:
            state["running"] = False
            state["current_turn_id"] = ""

        handler.handle_message("ou_user", "chat-group", "请一起看附件", message_id="g-text-a")

        self.assertIn("group.txt", handler._adapter.start_turn_calls[-1]["text"])

    def test_expired_attachment_blocks_follow_up_prompt(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler(
            {"default_working_dir": str(workspace), "attachment_ttl_seconds": 1}
        )
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"ttl",
            file_name="ttl.txt",
            content_type="text/plain",
        )

        with patch("bot.file_message_domain.time.time", return_value=10.0):
            handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "ttl.txt")
        with patch("bot.file_message_domain.time.time", return_value=20.0):
            handler.handle_message("ou_user", "c1", "还在吗", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIn("附件已过期", bot.replies[-1][1])
        attachment_dir = workspace / "_feishu_attachments"
        self.assertFalse(attachment_dir.exists() and any(attachment_dir.iterdir()))

    def test_unsupported_attachment_type_is_rejected_explicitly(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-folder"] = {"chat_type": "p2p", "message_type": "folder"}

        handler.handle_attachment_message("ou_user", "c1", "m-folder", "folder", "folder-key", "设计资料")

        self.assertIn("文件夹消息当前无法通过飞书 API 下载", bot.replies[-1][1])

    def test_merge_forward_attachment_type_is_rejected_with_specific_reason(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-forward"] = {"chat_type": "p2p", "message_type": "merge_forward"}

        handler.handle_attachment_message("ou_user", "c1", "m-forward", "merge_forward", "forward-key", "转发记录")

        self.assertIn("合并转发里的子附件当前无法通过飞书 API 下载", bot.replies[-1][1])

    def test_interactive_attachment_type_is_rejected_with_specific_reason(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-card"] = {"chat_type": "p2p", "message_type": "interactive"}

        handler.handle_attachment_message("ou_user", "c1", "m-card", "interactive", "card-key", "卡片资源")

        self.assertIn("卡片里的资源当前无法通过飞书 API 下载", bot.replies[-1][1])

    def test_permissions_command_applies_to_thread_creation_and_turn_start(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions danger-full-access")
        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["approval_policy"], "never")
        self.assertEqual(handler._adapter.create_thread_calls[-1]["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["approval_policy"], "never")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["permissions_profile_id"], ":danger-full-access")

    def test_model_command_applies_to_thread_creation_and_turn_start(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/model gpt-5.5")
        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["model"], "gpt-5.5")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["model"], "gpt-5.5")

    def test_resume_thread_id_disconnect_is_not_reported_as_not_found(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            cwd="/tmp/project",
            name="clfeishu",
            preview="分析本项目",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        def fake_resume_thread(thread_id: str, **kwargs):
            raise CodexRpcError("thread/resume", {"code": -32000, "message": "Codex websocket disconnected"})

        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = fake_resume_thread

        with self.assertRaisesRegex(RuntimeError, "无法通过 app-server 恢复这个 CLI 线程"):
            handler._resume_snapshot(thread.thread_id)

    def test_resume_thread_id_not_found_returns_value_error(self) -> None:
        handler, _ = self._make_handler()
        handler._adapter.list_threads_all = lambda **kwargs: []

        def fake_resume_thread(thread_id: str, **kwargs):
            raise CodexRpcError(
                "thread/resume",
                {"code": -32600, "message": f"no rollout found for thread id {thread_id}"},
            )

        handler._adapter.read_thread = lambda thread_id, include_turns=False: fake_resume_thread(thread_id)
        handler._adapter.resume_thread = fake_resume_thread

        with self.assertRaisesRegex(ValueError, "未找到匹配的线程"):
            handler._resume_snapshot("00000000-0000-0000-0000-000000000000")

    def test_resume_failure_keeps_existing_service_runtime_lease(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler.on_register(bot)
        handler._bind_thread("ou_user", "c1", thread)
        holder_ids_before = self._service_runtime_holder_ids(handler, "thread-1")

        def fake_resume_thread(thread_id: str, **kwargs):
            raise RuntimeError("resume failed")

        handler._adapter.resume_thread = fake_resume_thread

        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            handler._resume_snapshot_by_id(thread.thread_id, original_arg=thread.thread_id, summary=thread)

        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-1"), holder_ids_before)

    def test_resume_by_name_uses_exact_name_match(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        resumed: list[str] = []

        def fake_resume_thread(thread_id: str, **kwargs):
            resumed.append(thread_id)
            return ThreadSnapshot(summary=thread)

        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = fake_resume_thread

        snapshot = handler._resume_snapshot("demo")

        self.assertEqual(snapshot.summary.thread_id, "thread-1")
        self.assertEqual(resumed, ["thread-1"])

    def test_resume_by_name_lists_threads_across_all_providers(self) -> None:
        handler, _ = self._make_handler()
        captured_kwargs = {}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
            model_provider="provider2_api",
        )

        def fake_list_threads_all(**kwargs):
            captured_kwargs.update(kwargs)
            return [thread]

        handler._adapter.list_threads_all = fake_list_threads_all
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = lambda thread_id, **kwargs: ThreadSnapshot(summary=thread)

        handler._resume_snapshot("demo")

        self.assertEqual(captured_kwargs["model_providers"], [])

    def test_resume_by_name_multiple_matches_returns_error(self) -> None:
        handler, _ = self._make_handler()
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-a",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=2,
            source="vscode",
            status="notLoaded",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-b",
            name="demo",
            preview="world",
            created_at=0,
            updated_at=1,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread_1, thread_2]

        with self.assertRaisesRegex(ValueError, "匹配到多个同名线程"):
            handler._resume_snapshot("demo")

    def test_resume_command_for_not_loaded_thread_resumes_directly_and_syncs_runtime_settings(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            service_name="codex-tui",
        )

        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/resume demo")

        _, pending_card = bot.cards[0]
        self.assertEqual(pending_card["header"]["title"]["content"], "Codex 正在恢复线程")
        self.assertIn("正在恢复：`demo`", pending_card["elements"][0]["content"])
        handler._runtime_call(lambda: None)
        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": {"model_reasoning_effort": "high"},
                "model": "gpt-5.5",
                "model_provider": None,
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )
        self.assertEqual(
            handler._adapter.update_thread_settings_calls[-1],
            {
                "thread_id": "thread-1",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
            },
        )

    def test_resume_command_for_unloaded_active_goal_requires_confirm(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )

        handler.handle_message("ou_user", "c1", "/resume demo")

        _, confirm_card = bot.cards[-1]
        self.assertEqual(confirm_card["header"]["title"]["content"], "Codex 恢复线程确认")
        content = confirm_card["elements"][0]["content"]
        self.assertIn("persisted goal 当前是 `active`", content)
        actions = self._first_action(confirm_card)["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["按当前设置恢复并保持 paused", "直接恢复"])
        self.assertEqual(handler._adapter.resume_thread_calls, [])

    def test_resume_confirm_pause_active_goal_restores_thread_and_keeps_goal_paused(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = handler._get_runtime_state("ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/resume demo")
        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-resume",
            {
                "action": "resume_thread_confirm",
                "thread_id": "thread-1",
                "thread_title": "demo",
                "pause_active_goal_on_resume": "true",
                "origin": "command",
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": {"model_reasoning_effort": "high"},
                "model": "gpt-5.5",
                "model_provider": None,
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )
        self.assertEqual(
            handler._adapter.operation_log[-3:],
            [
                ("set_thread_goal", "thread-1", "paused"),
                ("resume_thread", "thread-1", "gpt-5.5"),
                ("update_thread_settings", "thread-1", "gpt-5.5"),
            ],
        )
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "paused")
        _, final_card = handler.bot.cards[-1]
        self.assertIn("persisted goal 仍保持 `paused`", final_card["elements"][0]["content"])

    def test_resume_confirm_direct_resume_skips_strict_pause_but_syncs_followup_settings(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        state = handler._get_runtime_state("ou_user", "c1")
        state["approval_policy"] = "never"
        state["permissions_profile_id"] = ":danger-full-access"
        state["model"] = "gpt-5.5"
        state["reasoning_effort"] = "high"

        handler.handle_message("ou_user", "c1", "/resume demo")
        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-resume",
            {
                "action": "resume_thread_confirm",
                "thread_id": "thread-1",
                "thread_title": "demo",
                "pause_active_goal_on_resume": "",
                "origin": "command",
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-1",
                "config_overrides": None,
                "model": None,
                "model_provider": None,
                "approval_policy": None,
                "permissions_profile_id": None,
            },
        )
        self.assertEqual(
            handler._adapter.update_thread_settings_calls[-1],
            {
                "thread_id": "thread-1",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(handler._adapter.set_thread_goal_calls, [])

    def test_resume_command_ignores_goal_confirm_when_goals_feature_disabled(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        handler.handle_message("ou_user", "c1", "/resume demo")

        self.assertTrue(bot.cards)
        self.assertNotEqual(bot.cards[0][1]["header"]["title"]["content"], "Codex 恢复线程确认")
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")

    def test_threads_card_resume_ignores_goal_confirm_when_goals_feature_disabled(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        def fake_get_thread_goal(thread_id: str):
            raise CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})

        handler._adapter.get_thread_goal = fake_get_thread_goal

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-1", "thread_title": "demo"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")
        self.assertIn("正在恢复线程", response["card"]["elements"][0]["content"])

    def test_resume_confirm_pause_active_goal_rolls_back_when_settings_sync_fails(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        handler._adapter.update_thread_settings = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sync failed"))

        handler.handle_message("ou_user", "c1", "/resume demo")
        self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-resume",
            {
                "action": "resume_thread_confirm",
                "thread_id": "thread-1",
                "thread_title": "demo",
                "pause_active_goal_on_resume": "true",
                "origin": "command",
            },
        ))
        handler._runtime_call(lambda: None)

        self.assertEqual(
            handler._adapter.set_thread_goal_calls[-2:],
            [
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "paused",
                    "token_budget": None,
                },
                {
                    "thread_id": "thread-1",
                    "objective": None,
                    "status": "active",
                    "token_budget": None,
                },
            ],
        )
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "active")
        self.assertIn("恢复线程后同步当前会话设置失败", bot.replies[-1][1])

    def test_threads_card_mentions_global_resume_scope(self) -> None:
        handler, bot = self._make_handler()
        captured_kwargs = {}

        def fake_list_threads_all(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        handler._adapter.list_threads_all = fake_list_threads_all

        handler.handle_message("ou_user", "c1", "/threads")

        self.assertEqual(captured_kwargs["model_providers"], [])
        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        content = card["elements"][0]["content"]
        self.assertIn("跨 provider 汇总", content)
        self.assertIn(f"`{_DISPLAY_RESUME_COMMAND}`", content)
        self.assertIn(f"`{_DISPLAY_LOCAL_RESUME_COMMAND}`", content)
        self.assertIn("`focusctl thread list --scope cwd`", content)

    def test_threads_card_uses_trisection_layout_for_row_actions(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler.handle_message("ou_user", "c1", "/threads")

        _, card = bot.cards[0]
        action_elements = self._action_elements(card)
        row_action = action_elements[0]
        self.assertEqual(row_action["layout"], "trisection")
        self.assertEqual(len(row_action["actions"]), 2)
        self.assertEqual(row_action["actions"][0]["text"]["content"], "恢复")
        self.assertEqual(row_action["actions"][1]["text"]["content"], "归档")
        bottom_action = action_elements[-1]
        self.assertTrue(any(btn["text"]["content"] == "收起" for btn in bottom_action["actions"]))

    def test_threads_card_marks_current_thread_in_button_text(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_thread_id"] = "thread-1"
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler.handle_message("ou_user", "c1", "/threads")

        _, card = bot.cards[0]
        self.assertNotIn("**当前**", card["elements"][2]["content"])
        row_action = self._action_elements(card)[0]
        self.assertEqual(row_action["actions"][0]["text"]["content"], "当前")
        self.assertEqual(row_action["actions"][0]["type"], "primary")

    def test_threads_command_rejects_extra_args(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/threads extra")

        self.assertEqual(bot.cards, [])
        self.assertIn("用法：`/threads`", bot.replies[-1][1])
        self.assertIn("不接受额外参数", bot.replies[-1][1])

    def test_named_instance_threads_command_shares_global_current_dir_threads(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir, instance_name="corp-b")
        runtime = handler._get_runtime_view("ou_user", "c1")
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd=runtime.working_dir,
            name="one",
            preview="hello",
            created_at=0,
            updated_at=2,
            source="cli",
            status="idle",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd=runtime.working_dir,
            name="two",
            preview="world",
            created_at=0,
            updated_at=1,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread_1, thread_2]

        handler.handle_message("ou_user", "c1", "/threads")

        _, card = bot.cards[-1]
        content = "\n".join(
            element["content"]
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)

    def test_named_instance_resume_accepts_global_thread_id(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir, instance_name="corp-b")
        thread = ThreadSummary(
            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.thread_snapshots[(thread.thread_id, None)] = ThreadSnapshot(summary=thread)

        snapshot = handler._resume_snapshot(thread.thread_id)

        self.assertEqual(snapshot.summary.thread_id, thread.thread_id)
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], thread.thread_id)

    def test_close_threads_card_action_returns_closed_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "close_threads_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程（已收起）")
        action = self._first_action(response["card"])
        self.assertEqual(action["actions"][0]["text"]["content"], "展开线程列表")

    def test_reopen_threads_card_action_returns_threads_card(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "reopen_threads_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

    def test_show_more_threads_action_expands_all_rows(self) -> None:
        handler, _ = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]
        handler._adapter.list_threads_all = lambda **kwargs: threads

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        ))

        content = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertIn("thread-3", content)
        bottom_action = self._action_elements(response["card"])[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))
        self.assertEqual(response["toast"], "已展开全部线程。")

    def test_expanded_threads_card_stays_expanded_after_archive(self) -> None:
        handler, _ = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]

        def _list_threads_all(**kwargs):
            return [thread for thread in threads if thread.thread_id != "thread-3"]

        handler._adapter.list_threads_all = lambda **kwargs: threads
        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )
        handler._adapter.list_threads_all = _list_threads_all
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(
            summary=next(thread for thread in threads if thread.thread_id == thread_id)
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "archive_thread", "thread_id": "thread-3"},
        ))

        content = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertNotIn("thread-3", content)
        bottom_action = self._action_elements(response["card"])[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))

    def test_expanded_threads_card_stays_expanded_after_rename(self) -> None:
        handler, _ = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]

        def _rename_thread(thread_id: str, name: str) -> None:
            for index, thread in enumerate(threads):
                if thread.thread_id == thread_id:
                    threads[index] = ThreadSummary(
                        thread_id=thread.thread_id,
                        cwd=thread.cwd,
                        name=name,
                        preview=thread.preview,
                        created_at=thread.created_at,
                        updated_at=thread.updated_at,
                        source=thread.source,
                        status=thread.status,
                        active_flags=list(thread.active_flags),
                        path=thread.path,
                        model_provider=thread.model_provider,
                        service_name=thread.service_name,
                    )
                    return
            raise AssertionError(f"unexpected thread_id: {thread_id}")

        handler._adapter.list_threads_all = lambda **kwargs: threads
        handler._adapter.rename_thread = _rename_thread

        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )
        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_rename_form", "thread_id": "thread-2"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {
                "action": "rename_thread",
                "thread_id": "thread-2",
                "_form_value": {"rename_title": "two-renamed"},
            },
        ))

        content = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertIn("thread-3", content)
        self.assertIn("two-renamed", content)
        bottom_action = self._action_elements(response["card"])[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))
        self.assertEqual(response["toast"], "已重命名。")

    def test_resume_thread_on_runtime_submit_refreshes_threads_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
            service_name="codex-tui",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler._runtime_submit(
            handler._resume_thread_on_runtime,
            "ou_user",
            "c1",
            "thread-1",
            original_arg="thread-1",
            summary=thread,
            message_id="msg-session",
            refresh_threads_message_id="msg-session",
        )
        handler._runtime_call(lambda: None)

        self.assertTrue(any(message_id == "msg-session" for message_id, _ in bot.patches))

    def test_resume_thread_on_runtime_rejects_if_binding_became_running_before_runtime_executes(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )
        started = threading.Event()
        release = threading.Event()

        def block_runtime() -> None:
            started.set()
            self.assertTrue(release.wait(timeout=1))

        handler._runtime_submit(block_runtime)
        self.assertTrue(started.wait(timeout=1))
        handler._runtime_submit(
            handler._resume_thread_on_runtime,
            "ou_user",
            "c1",
            "thread-1",
            summary=thread,
            pause_active_goal_on_resume=True,
            message_id="msg-session",
        )
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "msg-turn"
            state["current_turn_id"] = "turn-1"
            state["running"] = True
            state["awaiting_local_turn_started"] = False
        release.set()
        handler._runtime_call(lambda: None)

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.set_thread_goal_calls, [])
        self.assertEqual(handler._adapter.thread_goals["thread-1"].status, "active")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")
        self.assertIn("当前线程仍在执行，暂不切换。", bot.replies[-1][1])

    def test_expanded_threads_card_stays_expanded_after_resume_refresh(self) -> None:
        handler, bot = self._make_handler({"threads_initial_limit": 1})
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="one",
                preview="",
                created_at=0,
                updated_at=3,
                source="cli",
                status="notLoaded",
            ),
            ThreadSummary(
                thread_id="thread-2",
                cwd="/tmp/project",
                name="two",
                preview="",
                created_at=0,
                updated_at=2,
                source="cli",
                status="idle",
            ),
            ThreadSummary(
                thread_id="thread-3",
                cwd="/tmp/project",
                name="three",
                preview="",
                created_at=0,
                updated_at=1,
                source="cli",
                status="idle",
            ),
        ]
        def _read_thread(thread_id: str, include_turns: bool = False) -> ThreadSnapshot:
            del include_turns
            thread = next(item for item in threads if item.thread_id == thread_id)
            return ThreadSnapshot(summary=thread)

        handler._adapter.list_threads_all = lambda **kwargs: threads
        handler._adapter.read_thread = _read_thread
        handler._adapter.resume_thread = lambda thread_id, **kwargs: ThreadSnapshot(summary=_read_thread(thread_id).summary)

        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-1", "thread_title": "one"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")
        pending_content = response["card"]["elements"][0]["content"]
        self.assertIn("正在恢复线程", pending_content)
        self.assertNotIn("toast", response)
        handler._runtime_call(lambda: None)

        patched = json.loads(next(content for message_id, content in bot.patches if message_id == "msg-session"))
        content = "\n".join(
            element.get("content", "")
            for element in patched["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)
        self.assertIn("thread-2", content)
        self.assertIn("thread-3", content)
        bottom_action = self._action_elements(patched)[-1]
        self.assertFalse(any(btn["text"]["content"] == "更多" for btn in bottom_action["actions"]))

    def test_help_overview_is_layered(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台")
        content = card["elements"][0]["content"]
        self.assertIn("目录：", content)
        self.assertIn("线程：`未绑定`", content)
        self.assertIn("推送：`", content)
        self.assertIn("本轮：权限 `Full` | 模型 `Auto` | 推理 `Auto`", content)
        action_elements = self._action_elements(card)
        self.assertEqual(action_elements[0]["layout"], "bisected")
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["开始", "线程设置"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["本轮设置", "连接状态"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["群聊设置", "更多"],
        )

    def test_commands_lists_common_navigation_commands(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/commands")

        reply = bot.replies[-1][1]
        self.assertIn("常用命令列表", reply)
        self.assertIn("`/commands`", reply)
        self.assertIn("`/help [overview|start|thread-settings|turn|connection|group|more]`", reply)
        self.assertIn("`/status`", reply)
        self.assertIn("`/goal [show|text|set 〈objective〉|pause|resume|clear]`", reply)
        self.assertIn("`/compact`", reply)
        self.assertIn("`/detach`", reply)
        self.assertIn("`/attach [binding|thread|service]`", reply)
        self.assertIn(f"`{_DISPLAY_RESUME_COMMAND}`", reply)
        self.assertIn("`/group-mode [assistant|mention-only|all]`", reply)
        self.assertIn("`/reset-backend`", reply)
        self.assertIn("`/last text`", reply)
        self.assertIn("`/model [name|auto]`", reply)
        self.assertIn("`/effort [auto|value]`", reply)
        self.assertIn(f"`{_DISPLAY_INIT_COMMAND}`", reply)
        self.assertIn(f"`{_DISPLAY_DEBUG_CONTACT_COMMAND}`", reply)
        self.assertNotIn("`/cancel`", reply)

    def test_commands_rejects_extra_args(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/commands extra")

        self.assertIn("用法：`/commands`", bot.replies[-1][1])

    def test_compact_command_starts_current_thread_compaction(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-1"])
        self.assertEqual(bot.cards[-1][1]["header"]["title"]["content"], "Codex Compact 已开始")
        self.assertIn("`thread-1", bot.cards[-1][1]["elements"][0]["content"])

    def test_compact_command_card_failure_clears_running_state(self) -> None:
        handler, _ = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"
        handler._send_execution_card = lambda *args, **kwargs: ""

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertIn("执行卡片发送失败，未启动 compact", state["execution_transcript"].reply_text())

    def test_prompt_compact_prompt_queue_runs_fifo(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "first")
        handler.handle_message("ou_user", "c1", "/compact")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertEqual(bot.replies[-2], ("c1", "已排队，compact 将在当前执行结束后开始。队列位置：1"))
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：2"))

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)

        handler._handle_turn_started({"threadId": "thread-created", "turn": {"id": "compact-turn"}})
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "compact-turn", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_queued_compact_ignores_stale_idle_before_turn_started(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "first", message_id="m-1")
        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertEqual(bot.replies[-2], ("c1", "已排队，compact 将在当前执行结束后开始。队列位置：1"))
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：2"))

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_user", "c1", "m-compact")
        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertTrue(state["running"])
        self.assertEqual(state["current_prompt_message_id"], "m-compact")
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])

        handler._handle_thread_status_changed({"threadId": "thread-created", "status": {"type": "idle"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertTrue(state["running"])
        self.assertEqual(state["current_prompt_message_id"], "m-compact")
        self.assertEqual(state["current_turn_id"], "")

        handler._handle_turn_started({"threadId": "thread-created", "turn": {"id": "compact-turn"}})
        handler._handle_item_started(
            {
                "threadId": "thread-created",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-item"},
            }
        )
        self.assertIn("上下文压缩", state["execution_transcript"].process_text())

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "compact-turn", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_queued_compact_binds_from_context_compaction_item_started_when_turn_started_missing(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "first", message_id="m-1")
        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertEqual(bot.replies[-2], ("c1", "已排队，compact 将在当前执行结束后开始。队列位置：1"))
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：2"))

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_user", "c1", "m-compact")
        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(state["current_execution_kind"], "compact")
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])

        handler._handle_item_started(
            {
                "threadId": "thread-created",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-item"},
            }
        )

        self.assertEqual(state["current_turn_id"], "compact-turn")
        self.assertFalse(state["awaiting_local_turn_started"])
        self.assertIn("上下文压缩", state["execution_transcript"].process_text())

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "compact-turn", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_queued_compact_keeps_origin_context_after_message_context_expires(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
            "thread_id": "om_thread",
        }
        bot.message_contexts["m-compact"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
            "thread_id": "om_thread",
        }

        handler.handle_message("ou_admin", "chat-group", "first", message_id="m-1")
        handler.handle_message("ou_admin", "chat-group", "/compact", message_id="m-compact")
        bot.message_contexts.pop("m-compact", None)

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = handler._get_runtime_state("ou_admin", "chat-group", "m-compact")
        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(state["current_actor_open_id"], "ou_admin")
        self.assertEqual(bot.reply_ref_calls[-1][0], "m-compact")
        self.assertTrue(bot.reply_ref_calls[-1][3])

    def test_compact_then_prompt_queues_until_compact_completes(self) -> None:
        handler, _ = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        handler.handle_message("ou_user", "c1", "/compact")
        handler.handle_message("ou_user", "c1", "after compact", message_id="m-2")

        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-1"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 0)

        handler._finalize_execution_card_from_state("ou_user", "c1")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "after compact")

    def test_queued_prompt_uses_latest_model_setting_at_dequeue(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "first")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")
        handler.handle_message("ou_user", "c1", "/model gpt-5.5")

        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["model"], "gpt-5.5")

    def test_compact_command_surfaces_thread_not_loaded_hint(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            )
        )

        def _raise_not_loaded(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not loaded: thread-1"})

        handler._adapter.compact_thread = _raise_not_loaded

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("当前 thread 尚未加载到本实例 backend", bot.replies[-1][1])
        self.assertIn("`/attach`", bot.replies[-1][1])
        self.assertEqual(handler._adapter.read_thread_calls[-1], {"thread_id": "thread-1", "include_turns": False})

    def test_compact_command_thread_not_found_confirms_readable_thread_before_unloaded_hint(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            )
        )

        def _raise_not_found(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not found: thread-1"})

        handler._adapter.compact_thread = _raise_not_found

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("当前 thread 尚未加载到本实例 backend", bot.replies[-1][1])
        self.assertEqual(handler._adapter.read_thread_calls[-1], {"thread_id": "thread-1", "include_turns": False})

    def test_compact_command_surfaces_unconfirmed_state_when_read_returns_not_loaded(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )

        def _raise_not_found(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not found: thread-1"})

        handler._adapter.compact_thread = _raise_not_found

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("暂时无法确认它只是未加载", bot.replies[-1][1])
        self.assertIn("`/attach`", bot.replies[-1][1])

    def test_compact_command_surfaces_unconfirmed_state_when_read_is_unavailable(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = TimeoutError(
            "Codex request timed out: thread/read"
        )

        def _raise_not_found(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not found: thread-1"})

        handler._adapter.compact_thread = _raise_not_found

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("暂时无法确认它只是未加载", bot.replies[-1][1])
        self.assertIn("`/attach`", bot.replies[-1][1])

    def test_help_chat_page_mentions_status_preflight_and_cd(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help chat")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：连接状态")
        content = card["elements"][0]["content"]
        self.assertIn("查看当前状态、发送前检查", content)
        self.assertIn("附着当前实例", content)
        self.assertIn("切换线程或目录，请到“开始”", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["当前状态", "发送前检查"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["暂停推送", "附着当前实例"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["更多附着方式"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_chat_page_switches_toggle_to_attach_when_binding_detached(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        handler.handle_message("ou_user", "c1", "/help chat")

        _, card = bot.cards[-1]
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["恢复当前会话", "附着当前实例"],
        )
        self.assertEqual(action_elements[1]["actions"][0]["value"], {"action": "attach_runtime"})
        self.assertEqual(
            action_elements[1]["actions"][1]["value"],
            {"action": "attach_runtime", "scope": "service"},
        )

    def test_help_thread_page_mentions_resume_scope_and_local_resume(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help thread")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：开始")
        content = card["elements"][0]["content"]
        self.assertIn("同一线程允许多端订阅观察", content)
        self.assertIn("同一 live turn 只有一个交互 owner", content)
        self.assertIn(f"`{_DISPLAY_LOCAL_RESUME_COMMAND}`", content)
        self.assertIn("`focusctl thread list --scope cwd`", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["新建线程", "恢复线程"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["浏览线程", "切换目录"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["返回首页"],
        )

    def test_help_thread_settings_page_exposes_goal_entry(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help thread-settings")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：线程设置")
        content = card["elements"][0]["content"]
        self.assertIn("当前 goal 可通过 `/goal` 查看", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["查看 Goal", "压缩上下文"],
        )

    def test_help_runtime_mentions_permissions_as_recommended_entry(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help runtime")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：本轮设置")
        content = card["elements"][0]["content"]
        self.assertIn("推荐先用“权限基线”", content)
        self.assertIn("`/last text`", content)
        self.assertIn("回退到最近执行卡", content)
        self.assertIn("实例级 backend reset 在“更多 -> 高级操作”", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["权限基线", "模型"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["推理强度", "审批策略"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["最近文本"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_group_card_has_shortcuts(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help group")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：群聊设置")
        self.assertIn("未启用群里，非管理员不能使用机器人", card["elements"][0]["content"])
        self.assertIn("`all` 风险最高", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["群聊启用状态", "启用本群"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["停用本群", "群工作模式"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["返回首页"],
        )

    def test_help_identity_page_has_bootstrap_shortcuts(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help identity")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：更多")
        content = card["elements"][0]["content"]
        self.assertIn("`/whoami`", content)
        self.assertIn(f"`{_DISPLAY_INIT_COMMAND}`", content)
        self.assertNotIn("/debug-contact", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["身份信息", "机器人状态"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["初始化", "命令索引"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["高级操作"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_page_action_returns_runtime_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "runtime"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：本轮设置")
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["权限基线", "模型"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[1]["actions"]],
            ["推理强度", "审批策略"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[2]["actions"]],
            ["最近文本"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[3]["actions"]],
            ["返回首页"],
        )

    def test_reset_backend_command_returns_preview_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/reset-backend")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Backend Reset")
        self.assertIn("作用对象：当前实例 backend", card["elements"][0]["content"])

    def test_reset_backend_card_action_is_group_admin_only(self) -> None:
        handler, _ = self._make_handler()
        handler.bot.message_contexts["msg-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-group",
            {"action": "reset_backend", "force": True, "_operator_open_id": "ou_user"},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "仅管理员可操作群共享会话或群设置。")

    def test_help_page_action_returns_overview_dashboard(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "overview"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台")
        action_elements = self._action_elements(response["card"])
        self.assertEqual(action_elements[0]["layout"], "bisected")
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["开始", "线程设置"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["本轮设置", "连接状态"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["群聊设置", "更多"],
        )

    def test_help_navigation_actions_are_not_group_admin_only(self) -> None:
        handler, _ = self._make_handler()
        handler.bot.message_contexts["msg-help-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-help-group",
            {"action": "show_help_page", "page": "overview", "_operator_open_id": "ou_user"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台")

    def test_help_show_page_action_can_open_current_thread_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "thread-current"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：线程设置")
        self.assertIn("“开始”", response["card"]["elements"][0]["content"])
        action_elements = self._action_elements(response["card"])
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["查看 Goal", "压缩上下文"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["重命名", "归档当前"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["按目标归档"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_show_page_action_can_open_thread_resume_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "thread-resume-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：恢复线程")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertEqual(self._action_elements(response["card"])[0]["actions"][0]["text"]["content"], "返回上一页")

    def test_help_show_page_action_can_open_chat_cd_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "chat-cd-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：切换目录")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_CD_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_thread_rename_current_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "thread-rename-current-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：重命名")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_RENAME_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_identity_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "identity"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：更多")
        self.assertNotIn("/debug-contact", response["card"]["elements"][0]["content"])
        action_elements = self._action_elements(response["card"])
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["身份信息", "机器人状态"],
        )

    def test_help_show_page_action_can_open_identity_init_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "identity-init-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：初始化")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_INIT_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_attach_more_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "connection-status-attach-more"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：更多附着方式")
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["附着当前线程", "附着当前会话"],
        )
        self.assertEqual(
            self._action_elements(response["card"])[0]["actions"][0]["value"],
            {"action": "attach_runtime", "scope": "thread"},
        )
        self.assertEqual(
            self._action_elements(response["card"])[0]["actions"][1]["value"],
            {"action": "attach_runtime"},
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[1]["actions"]],
            ["返回上一页"],
        )

    def test_help_show_page_action_can_open_more_advanced_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "more-advanced"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：高级操作")
        self.assertIn("恢复或排障时重置当前实例 backend", response["card"]["elements"][0]["content"])
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["重置 backend", "联系人排障"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[1]["actions"]],
            ["返回上一页"],
        )

    def test_help_show_page_action_can_open_debug_contact_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "more-debug-contact-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：联系人排障")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_DEBUG_CONTACT_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_returns_warning_for_unknown_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "missing-page"},
        ))

        self.assertEqual(response["toast"], "未知帮助页面。")
        self.assertEqual(response["toast_type"], "warning")

    def test_help_unknown_topic_returns_warning_text(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help nonsense")

        self.assertIn("帮助主题支持", bot.replies[-1][1])

    def test_unknown_command_mentions_help_and_commands(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/missing")

        self.assertIn("发送 `/help` 或 `/commands` 查看可用命令。", bot.replies[-1][1])

    def test_help_execute_command_action_reuses_status_command(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "help_execute_command", "command": "/status", "title": "Codex 当前状态"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前状态")
        self.assertIn("当前线程：`thread-1", response["card"]["elements"][0]["content"])

    def test_help_execute_group_command_uses_sender_id_fallback_for_group_admin(self) -> None:
        handler, _ = self._make_handler()
        handler.bot.chat_types["chat-group"] = "group"

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_admin",
            "chat-group",
            "msg-help-card",
            {"action": "help_execute_command", "command": "/threads", "title": "Codex Threads"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

    def test_help_execute_whoami_action_uses_operator_identity_context(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-p2p",
            "msg-help",
            {
                "action": "help_execute_command",
                "command": "/whoami",
                "title": "Codex 身份信息",
                "_operator_open_id": "ou_user",
                "_operator_user_id": "u2",
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 身份信息")
        content = response["card"]["elements"][0]["content"]
        self.assertIn("user_id: `u2`", content)
        self.assertIn("open_id: `ou_user`", content)

    def test_help_submit_resume_command_reuses_resume_handler(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {
                "action": "help_submit_command",
                "command": "/resume",
                "field_name": "resume_target",
                "title": "Codex 恢复线程",
                "_form_value": {"resume_target": "demo"},
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")

    def test_help_submit_init_command_uses_operator_identity_context(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "open_id": "ou_bot_new",
            "source": "auto-discovered",
            "configured_open_id": "",
            "discovered_open_id": "ou_bot_new",
            "trigger_open_ids": "",
        }

        with patch("bot.codex_settings_domain.ensure_init_token", return_value="secret-1"), patch(
            "bot.codex_settings_domain.load_system_config_raw",
            return_value={
                "app_id": "cli_test_app",
                "app_secret": "secret",
                "admin_open_ids": ["ou_admin"],
            },
        ), patch("bot.codex_settings_domain.save_system_config") as save_config:
            response = self._unpack_card_response(handler.handle_card_action(
                "ou_user2",
                "chat-p2p",
                "msg-help-init",
                {
                    "action": "help_submit_command",
                    "command": "/init",
                    "field_name": "init_token",
                    "title": "Codex 初始化结果",
                    "_form_value": {"init_token": "secret-1"},
                    "_operator_open_id": "ou_user2",
                    "_operator_user_id": "u2",
                },
            ))

        saved = save_config.call_args.args[0]
        self.assertEqual(saved["admin_open_ids"], ["ou_admin", "ou_user2"])
        self.assertEqual(saved["bot_open_id"], "ou_bot_new")
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 初始化结果")
        content = response["card"]["elements"][0]["content"]
        self.assertIn("已加入 `Alice`", content)
        self.assertIn("`ou_bot_new`", content)

    def test_help_submit_cd_command_reuses_cd_handler(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {
                "action": "help_submit_command",
                "command": "/cd",
                "field_name": "cd_path",
                "title": "Codex 目录切换结果",
                "_form_value": {"cd_path": "/tmp"},
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 目录已切换")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")

    def test_help_submit_init_command_preserves_scope_guard(self) -> None:
        handler, _ = self._make_handler()
        handler.bot.message_contexts["msg-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-group",
            {
                "action": "help_submit_command",
                "command": "/init",
                "field_name": "init_token",
                "title": "Codex 初始化结果",
                "_form_value": {"init_token": "demo"},
            },
        ))

        self.assertEqual(response["toast"], f"请私聊机器人执行 `{_DISPLAY_INIT_COMMAND}`。")
        self.assertEqual(response["toast_type"], "warning")

    def test_new_command_reply_focuses_on_next_step(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/new")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 线程已新建")
        content = card["elements"][0]["content"]
        self.assertIn("线程：`", content)
        self.assertIn("目录：`", content)
        self.assertIn("直接发送普通文本开始第一轮对话。", content)

    def test_cd_command_success_uses_card_and_clears_binding(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        handler.handle_message("ou_user", "c1", "/cd /tmp")

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录已切换")
        self.assertIn("当前线程绑定已清空。", card["elements"][0]["content"])

    def test_cd_command_expands_home_directory(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        home = pathlib.Path(tempdir.name) / "home"
        project = home / "project"
        project.mkdir(parents=True)
        handler, bot = self._make_handler({"default_working_dir": str(home)})
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd=str(home),
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        with patch.dict(os.environ, {"HOME": str(home)}):
            handler.handle_message("ou_user", "c1", "/cd ~/project")

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["working_dir"], str(project))
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录已切换")
        self.assertIn("目录：`~/project`", card["elements"][0]["content"])

    def test_cd_command_invalidates_pending_attachments_in_current_scope(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace-1"
        workspace_2 = pathlib.Path(tempdir.name) / "workspace-2"
        workspace.mkdir()
        workspace_2.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"spec-content",
            file_name="spec.pdf",
            content_type="application/pdf",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "spec.pdf")
        staged_file = next((workspace / "_feishu_attachments").iterdir())

        handler.handle_message("ou_user", "c1", f"/cd {workspace_2}")

        self.assertEqual(handler._pending_attachment_store.list_all(), ())
        _, card = bot.cards[-1]
        self.assertIn("已使 1 个待消费附件失效。", card["elements"][0]["content"])

        handler.handle_message("ou_user", "c1", "请处理附件", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "请处理附件")
        self.assertNotIn(str(staged_file), handler._adapter.start_turn_calls[-1]["text"])

    def test_bind_thread_to_new_thread_clears_previous_execution_anchor(self) -> None:
        handler, _ = self._make_handler()
        old_thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="old",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        new_thread = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-2",
            name="new",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", old_thread)
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "card-live"
            state["last_execution_message_id"] = "card-old"
            state["current_turn_id"] = "turn-1"
            state["current_prompt_message_id"] = "prompt-1"
            state["execution_transcript"].set_reply_text("stale")

        handler._bind_thread("ou_user", "c1", new_thread)

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["current_thread_id"], "thread-2")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["last_execution_message_id"], "")
        self.assertEqual(state["current_prompt_message_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        with patch.object(handler, "_refresh_terminal_execution_card_from_state") as refresh:
            ok, message = handler._cancel_current_turn("ou_user", "c1")
        self.assertFalse(ok)
        self.assertEqual(message, "当前没有正在执行的 turn。")
        refresh.assert_not_called()

    def test_clear_thread_binding_clears_previous_execution_anchor(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "card-live"
            state["last_execution_message_id"] = "card-old"
            state["current_turn_id"] = "turn-1"
            state["current_prompt_message_id"] = "prompt-1"
            state["execution_transcript"].set_reply_text("stale")

        handler._clear_thread_binding("ou_user", "c1")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["last_execution_message_id"], "")
        self.assertEqual(state["current_prompt_message_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        with patch.object(handler, "_refresh_terminal_execution_card_from_state") as refresh:
            ok, message = handler._cancel_current_turn("ou_user", "c1")
        self.assertFalse(ok)
        self.assertEqual(message, "当前没有正在执行的 turn。")
        refresh.assert_not_called()

    def test_cd_command_failure_uses_warning_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/cd /definitely-not-exists")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录未切换")
        self.assertIn("目录不存在", card["elements"][0]["content"])

    def test_resume_success_merges_switch_summary_into_history_preview_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = lambda thread_id, **kwargs: ThreadSnapshot(
            summary=thread,
            turns=[
                {
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]},
                        {"type": "agentMessage", "text": "world"},
                    ]
                }
            ],
        )

        handler.handle_message("ou_user", "c1", "/resume demo")

        self.assertEqual(bot.cards[0][1]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "线程 thread-1… 最近对话")
        content = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("已切换到线程", content)
        self.assertIn("目录：`/tmp/project`", content)
        self.assertIn("👤 **你**", content)
        self.assertIn("🤖 **Codex**", content)

    def test_resume_card_action_for_not_loaded_thread_resumes_directly(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            service_name="codex-tui",
        )
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-1",
            {"action": "resume_thread", "thread_id": "thread-1", "thread_title": "demo"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")
        self.assertIn("正在恢复线程", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

    def test_resume_card_action_failure_refreshes_threads_card(self) -> None:
        handler, bot = self._make_handler({"threads_initial_limit": 1})
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="one",
            preview="",
            created_at=0,
            updated_at=3,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-missing", "thread_title": "missing"},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("恢复线程失败", response["toast"])
        self.assertNotIn("card", response)
        self.assertEqual(bot.replies, [])
        self.assertEqual(bot.patches, [])

    def test_attach_binding_card_action_returns_ack_card_then_sends_result_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-attach", {"action": "attach_runtime"})
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复飞书推送")
        self.assertIn("当前会话推送", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        _, final_card = bot.cards[-1]
        self.assertEqual(final_card["header"]["title"]["content"], "Codex 已附着飞书推送")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_attach_thread_card_action_returns_ack_card_then_sends_result_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-attach",
                {"action": "attach_runtime", "scope": "thread"},
            )
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复飞书推送")
        self.assertIn("当前线程推送", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        _, final_card = bot.cards[-1]
        self.assertEqual(final_card["header"]["title"]["content"], "Codex 已附着飞书推送")

    def test_attach_service_card_action_returns_ack_card_then_sends_result_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)
        state = handler._get_runtime_state("ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-attach",
                {"action": "attach_runtime", "scope": "service"},
            )
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复飞书推送")
        self.assertIn("当前实例推送", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        _, final_card = bot.cards[-1]
        self.assertEqual(final_card["header"]["title"]["content"], "Codex 已附着飞书推送")

    def test_show_rename_form_registers_pending_message(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-rename",
            {"action": "show_rename_form", "thread_id": "thread-1"},
        ))

        pending = self._pending_rename_form_snapshot(handler, "msg-rename")
        assert pending is not None
        self.assertEqual(pending["thread_id"], "thread-1")
        self.assertEqual(response["card"]["header"]["title"]["content"], "重命名线程")

    def test_form_value_only_callback_submits_rename(self) -> None:
        handler, _ = self._make_handler()
        renamed = {}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="old-title",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
        )
        self._register_pending_rename_form(handler, "msg-rename", thread_id="thread-1")
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        def fake_rename_thread(thread_id: str, name: str) -> None:
            renamed["thread_id"] = thread_id
            renamed["name"] = name

        handler._adapter.rename_thread = fake_rename_thread

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-rename",
            {"_form_value": {"rename_title": "new-title"}},
        ))

        self.assertEqual(renamed, {"thread_id": "thread-1", "name": "new-title"})
        self.assertIsNone(self._pending_rename_form_snapshot(handler, "msg-rename"))
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已重命名。")

    def test_form_value_only_help_cd_callback_reuses_cd_handler(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"_form_value": {"cd_path": "/tmp"}},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 目录已切换")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")

    def test_form_value_only_help_rename_current_callback_reuses_rename_handler(self) -> None:
        handler, _ = self._make_handler()
        renamed = {}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="old-title",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._bind_thread("ou_user", "c1", thread)

        def fake_rename_thread(thread_id: str, name: str) -> None:
            renamed["thread_id"] = thread_id
            renamed["name"] = name

        handler._adapter.rename_thread = fake_rename_thread

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"_form_value": {"help_rename_current_title": "new-title"}},
        ))

        self.assertEqual(renamed, {"thread_id": "thread-1", "name": "new-title"})
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 重命名结果")

    def test_form_value_only_callback_without_pending_rename_returns_warning(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-rename",
            {"_form_value": {"rename_title": "new-title"}},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "重命名表单已失效，请重新打开。")

    def test_approval_card_action_is_idempotent_while_processing(self) -> None:
        handler, _ = self._make_handler()
        responded = []
        nested = {}

        def fake_respond(request_id, *, result=None, error=None):
            responded.append((request_id, result, error))
            if len(responded) == 1:
                nested["response"] = self._unpack_card_response(handler._handle_approval_card_action(
                    {
                        "request_id": "req-1",
                        "action": "command_allow_once",
                    }
                ))

        handler._adapter.respond = fake_respond
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
            "title": "Codex 命令执行审批",
            "questions": [],
            "answers": {},
            "status": "pending",
        })

        response = self._unpack_card_response(handler._handle_approval_card_action(
            {
                "request_id": "req-1",
                "action": "command_allow_once",
            }
        ))

        self.assertEqual(len(responded), 1)
        self.assertEqual(responded[0][0], "rpc-1")
        self.assertEqual(responded[0][1], {"decision": "accept"})
        self.assertEqual(nested["response"]["toast_type"], "warning")
        self.assertEqual(nested["response"]["toast"], "该审批请求正在处理中，请稍候。")
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已允许本次")

    def test_custom_user_input_is_shown_when_other_is_allowed(self) -> None:
        card = build_ask_user_card(
            "req-1",
            [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}, {"label": "暂缓步骤", "description": ""}],
                    "isOther": True,
                }
            ],
        )

        self.assertTrue(any(element.get("tag") == "form" for element in card["elements"]))

    def test_custom_answer_is_rejected_when_question_is_option_only(self) -> None:
        handler, _ = self._make_handler()
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "questions": [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}],
                    "isOther": False,
                }
            ],
            "answers": {},
        })

        response = self._unpack_card_response(handler._handle_user_input_action(
            {
                "request_id": "req-1",
                "action": "answer_user_input_custom",
                "question_id": "q1",
                "_form_value": {"user_input_q1": "自定义"},
            }
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "该问题仅支持选择预设选项")

    def test_form_value_only_callback_submits_custom_user_input(self) -> None:
        handler, _ = self._make_handler()
        responded = {}

        def fake_respond(request_id, *, result=None, error=None):
            responded["request_id"] = request_id
            responded["result"] = result
            responded["error"] = error

        handler._adapter.respond = fake_respond
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "message_id": "msg-1",
            "questions": [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}],
                    "isOther": True,
                }
            ],
            "answers": {},
        })

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-1",
            {"_form_value": {"user_input_q1": "创建 c.txt"}},
        ))

        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已提交回答。")
        self.assertEqual(responded["request_id"], "rpc-1")
        self.assertEqual(
            responded["result"],
            {"answers": {"q1": {"answers": ["创建 c.txt"]}}},
        )

    def test_group_request_actor_can_submit_own_supplemental_input(self) -> None:
        handler, bot = self._make_handler()
        responded = {}
        bot.message_contexts["msg-group-input"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        def fake_respond(request_id, *, result=None, error=None):
            responded["request_id"] = request_id
            responded["result"] = result
            responded["error"] = error

        handler._adapter.respond = fake_respond
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "message_id": "msg-group-input",
            "questions": [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}],
                    "isOther": False,
                }
            ],
            "answers": {},
            "chat_id": "chat-group",
            "sender_id": "__group__",
            "actor_open_id": "ou_user",
        })

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-group-input",
            {
                "action": "answer_user_input_option",
                "request_id": "req-1",
                "question_id": "q1",
                "answer": "确认步骤",
            },
        ))

        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已提交回答。")
        self.assertEqual(responded["request_id"], "rpc-1")
        self.assertEqual(
            responded["result"],
            {"answers": {"q1": {"answers": ["确认步骤"]}}},
        )

    def test_user_input_action_is_idempotent_while_processing_final_submit(self) -> None:
        handler, _ = self._make_handler()
        responded = []
        nested = {}

        def fake_respond(request_id, *, result=None, error=None):
            responded.append((request_id, result, error))
            if len(responded) == 1:
                nested["response"] = self._unpack_card_response(handler._handle_user_input_action(
                    {
                        "request_id": "req-1",
                        "action": "answer_user_input_option",
                        "question_id": "q1",
                        "answer": "确认步骤",
                    }
                ))

        handler._adapter.respond = fake_respond
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "questions": [
                {
                    "id": "q1",
                    "header": "步骤确认",
                    "question": "请选择下一步。",
                    "options": [{"label": "确认步骤", "description": ""}],
                    "isOther": False,
                }
            ],
            "answers": {},
            "status": "pending",
        })

        response = self._unpack_card_response(handler._handle_user_input_action(
            {
                "request_id": "req-1",
                "action": "answer_user_input_option",
                "question_id": "q1",
                "answer": "确认步骤",
            }
        ))

        self.assertEqual(len(responded), 1)
        self.assertEqual(responded[0][0], "rpc-1")
        self.assertEqual(
            responded[0][1],
            {"answers": {"q1": {"answers": ["确认步骤"]}}},
        )
        self.assertEqual(nested["response"]["toast_type"], "warning")
        self.assertEqual(nested["response"]["toast"], "该输入请求正在提交，请稍候。")
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已提交回答。")

    def test_form_value_only_callback_without_pending_request_returns_warning(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "missing-msg",
            {"_form_value": {"user_input_q1": "创建 c.txt"}},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "表单已失效或未找到对应问题，请重新触发该请求。")


if __name__ == "__main__":
    unittest.main()
