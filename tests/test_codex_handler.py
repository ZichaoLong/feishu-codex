import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.cards import build_ask_user_card, build_execution_card
from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary, ThreadSnapshot, ThreadSummary
from bot.codex_handler import CodexHandler
from bot.codex_protocol.client import CodexRpcError
from bot.execution_transcript import ExecutionReplySegment
from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.service_instance_lease import ServiceInstanceLease, ServiceInstanceLeaseError
from bot.stores.interaction_lease_store import InteractionLeaseStore, make_fcodex_interaction_holder


class _FakeAdapter:
    def __init__(
        self,
        config,
        *,
        on_notification=None,
        on_request=None,
        app_server_runtime_store=None,
    ) -> None:
        self.config = config
        self.on_notification = on_notification
        self.on_request = on_request
        self.app_server_runtime_store = app_server_runtime_store
        self.start_calls = 0
        self.last_profile = "provider1"
        self.set_active_profile_calls: list[str] = []
        self.create_thread_calls: list[dict] = []
        self.resume_thread_calls: list[dict] = []
        self.start_turn_calls: list[dict] = []
        self.interrupt_turn_calls: list[dict] = []
        self.archive_thread_calls: list[str] = []
        self.unsubscribe_thread_calls: list[str] = []
        self.read_thread_calls: list[dict] = []
        self.thread_snapshots: dict[tuple[str, bool | None], ThreadSnapshot | Exception] = {}

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
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ):
        self.create_thread_calls.append(
            {
                "cwd": cwd,
                "profile": profile,
                "approval_policy": approval_policy,
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

    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        return RuntimeConfigSummary(
            current_profile=self.last_profile,
            current_model_provider=f"{self.last_profile}_api",
            profiles=[
                RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
            ],
        )

    def list_loaded_thread_ids(self) -> list[str]:
        return sorted(
            {
                thread_id
                for (thread_id, _include_turns), snapshot in self.thread_snapshots.items()
                if snapshot is not None and not isinstance(snapshot, Exception)
            }
        )

    def set_active_profile(self, profile: str) -> RuntimeConfigSummary:
        self.set_active_profile_calls.append(profile)
        self.last_profile = profile
        return self.read_runtime_config()

    def resume_thread(
        self,
        thread_id: str,
        *,
        profile: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
    ):
        self.resume_thread_calls.append({
            "thread_id": thread_id,
            "profile": profile,
            "model": model,
            "model_provider": model_provider,
        })
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

    def unsubscribe_thread(self, thread_id: str) -> None:
        self.unsubscribe_thread_calls.append(thread_id)

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
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
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
                "profile": profile,
                "approval_policy": approval_policy,
                "sandbox": sandbox,
                "reasoning_effort": reasoning_effort,
                "collaboration_mode": collaboration_mode,
            }
        )
        return {"turn": {"id": "turn-1"}}

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        self.interrupt_turn_calls.append({"thread_id": thread_id, "turn_id": turn_id})


class _FakeBot:
    def __init__(self, data_dir: pathlib.Path) -> None:
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
        self.reserved_execution_cards: dict[str, str] = {}
        self.admin_open_ids = {"ou_admin"}
        self.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "ou_bot",
            "discovered_open_id": "ou_bot",
            "trigger_open_ids": [],
        }
        self.runtime_bot_open_id = "ou_bot"
        self.downloaded_resources: dict[tuple[str, str, str], object] = {}

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.replies.append((chat_id, text))
        if parent_message_id:
            self.reply_parents.append((chat_id, text, parent_message_id))
            self.reply_parent_calls.append((chat_id, text, parent_message_id, reply_in_thread))

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
            return {"ou_admin": "Admin", "ou_user": "User", "ou_user2": "Alice"}.get(open_id, open_id[:8])
        if user_id:
            return user_id[:8]
        return "unknown"

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
        return handler._session_ui_domain.pending_rename_form_snapshot(message_id)

    @staticmethod
    def _register_pending_rename_form(handler: CodexHandler, message_id: str, *, thread_id: str) -> None:
        handler._session_ui_domain.register_pending_rename_form(message_id, thread_id=thread_id)

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
                "FC_GLOBAL_DATA_DIR": str(data_dir / "_global"),
                "FC_INSTANCE": instance_name,
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

    def test_mode_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/mode plan")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["collaboration_mode"], "plan")
        self.assertIn("已切换协作模式：`plan`", bot.replies[-1][1])
        self.assertIn("只影响当前飞书会话的后续 turn", bot.replies[-1][1])

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
        handler._handle_agent_message_delta({"threadId": "thread-1", "delta": "新的回复"})

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
            [{"thread_id": "thread-created", "profile": None, "model": None, "model_provider": None}],
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
            {"thread_id": "thread-created", "profile": None, "model": None, "model_provider": None},
        )
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_running_p2p_prompt_replies_wait_or_cancel(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler.handle_message("ou_user", "c1", "follow up", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1], ("c1", "当前线程仍在执行，请等待结束或先执行 `/cancel`。"))

    def test_running_group_prompt_replies_wait_or_cancel(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler.handle_message("ou_user2", "chat-group", "插播", message_id="m-2")

        self.assertEqual(
            bot.reply_parents[-1],
            ("chat-group", "当前线程仍在执行，请等待结束或先执行 `/cancel`。", "m-2"),
        )

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
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"status": "completed"}})
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
        handler1.handle_message("ou_user", "c1", "/permissions full-access")
        handler1.handle_message("ou_user", "c1", "/mode plan")
        handler1.handle_message("ou_user", "c1", "hello")

        handler2, _ = self._make_handler(data_dir=data_dir)
        state = handler2._get_runtime_state("ou_user", "c1")

        self.assertEqual(state["working_dir"], str(project_dir))
        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["current_thread_title"], "（无标题）")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["sandbox"], "danger-full-access")
        self.assertEqual(state["collaboration_mode"], "plan")
        self.assertFalse(state["running"])

        handler2.handle_message("ou_user", "c1", "follow up")

        self.assertEqual(len(handler2._adapter.create_thread_calls), 0)
        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-created")

    def test_p2p_stored_binding_rebuilds_thread_reverse_mapping_after_restart(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, _ = self._make_handler(data_dir=data_dir)
        handler1.handle_message("ou_user", "c1", "hello")

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        state = handler2._get_runtime_state("ou_user", "c1")

        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(handler2._thread_subscribers("thread-created"), (("ou_user", "c1"),))

        handler2.handle_message("ou_user", "c1", "follow up")
        handler2._handle_turn_started({"threadId": "thread-created", "turn": {"id": "turn-2"}})
        handler2._handle_agent_message_delta({"threadId": "thread-created", "delta": "恢复后事件正常路由"})
        handler2._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-2", "status": "completed"}})

        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-created")
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

    def test_group_stored_binding_rebuilds_thread_reverse_mapping_after_restart(self) -> None:
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
        self.assertEqual(handler2._thread_subscribers("thread-group"), (("__group__", "chat-group"),))

        bot2.message_contexts["m-prompt"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        handler2.handle_message("ou_user2", "chat-group", "继续", message_id="m-prompt")
        handler2._handle_turn_started({"threadId": "thread-group", "turn": {"id": "turn-2"}})
        handler2._handle_agent_message_delta({"threadId": "thread-group", "delta": "群重启后事件正常路由"})
        handler2._handle_turn_completed({"threadId": "thread-group", "turn": {"id": "turn-2", "status": "completed"}})

        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-group")
        self.assertTrue(bot2.patches)
        self.assertTrue(
            any("群重启后事件正常路由" in payload for _message_id, payload in bot2.patches)
        )

    def test_restart_keeps_interaction_owner_for_multi_subscriber_running_thread(self) -> None:
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

        self.assertEqual(handler2._thread_subscribers("thread-1"), (("ou_user", "chat-a"), ("ou_user", "chat-b")))
        interaction_owner = handler2._binding_runtime.interaction_owner_snapshot_locked(
            "thread-1",
            current_binding=("ou_user", "chat-a"),
        )
        self.assertEqual(interaction_owner["binding_id"], "p2p:ou_user:chat-a")
        self.assertEqual(interaction_owner["relation"], "current")

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
        handler2._handle_agent_message_delta({"threadId": "thread-1", "delta": "恢复后继续"})

        self.assertEqual(bot2.sent_messages[-1][0], "chat-a")
        self.assertEqual(
            handler2._get_runtime_state("ou_user", "chat-a")["execution_transcript"].reply_text(),
            "恢复后继续",
        )
        self.assertEqual(
            handler2._get_runtime_state("ou_user", "chat-b")["execution_transcript"].reply_text(),
            "恢复后继续",
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
        handler._handle_agent_message_delta({"threadId": "thread-created", "delta": "完整"})
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
        handler._terminal_result_card_limit = 200

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
        self.assertIn("watchdog final", card["elements"][-1]["content"])

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
        handler._terminal_result_card_limit = 200

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
        self.assertIn("123456789", card["elements"][-1]["content"])

    def test_group_terminal_result_card_stays_in_topic_after_message_context_is_gone(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        handler._terminal_result_card_limit = 200

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
        handler._handle_agent_message_delta({"threadId": "thread-1", "delta": "done"})
        handler._handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertNotIn(
            ("chat-b", "线程 `thread-1…` 的上一轮执行已结束；本会话现在可继续提问。"),
            bot.replies,
        )
        state_b = handler._get_runtime_state("ou_user", "chat-b")
        self.assertEqual(state_b["execution_transcript"].reply_text(), "")
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
        handler._handle_agent_message_delta({"threadId": "thread-created", "delta": "123456789"})
        target = handler._capture_terminal_reconcile_target("ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})
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
        self.assertIn("123456789", card["elements"][-1]["content"])

    def test_terminal_reconcile_sends_authoritative_result_card_from_snapshot_without_live_reply_delta(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_result_card_limit = 200

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
        self.assertIn("snapshot final answer", card["elements"][-1]["content"])
        self.assertEqual(bot.deletes, [])

    def test_mode_command_without_arg_shows_mode_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/mode")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 协作模式")
        content = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("更接近直接执行", content)
        self.assertIn("更容易先规划、提问，并展示计划卡片", content)
        action_elements = self._action_elements(card)
        self.assertEqual(action_elements[0]["layout"], "trisection")
        self.assertEqual(action_elements[1]["actions"][0]["text"]["content"], "返回帮助")

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
        handler._handle_agent_message_delta({"threadId": "thread-created", "delta": "第一段"})
        handler._handle_item_started(
            {
                "threadId": "thread-created",
                "item": {"type": "commandExecution", "command": "ls", "cwd": "/tmp/project"},
            }
        )
        handler._handle_item_completed(
            {
                "threadId": "thread-created",
                "item": {"type": "commandExecution", "status": "completed", "exitCode": 0},
            }
        )
        handler._handle_item_completed(
            {
                "threadId": "thread-created",
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

    def test_whoareyou_alias_returns_bot_identity(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "ou_bot",
            "discovered_open_id": "ou_bot",
            "trigger_open_ids": ["ou_alias_1", "ou_alias_2"],
        }

        handler.handle_message("ou_user", "chat-p2p", "/whoareyou")

        reply = bot.replies[-1][1]
        self.assertIn("机器人身份信息", reply)
        self.assertIn("app_id: `cli_test_app`", reply)
        self.assertIn("configured bot_open_id: `ou_bot`", reply)
        self.assertIn("discovered open_id: `ou_bot`", reply)
        self.assertIn("runtime mention matching: `enabled`", reply)
        self.assertIn("trigger_open_ids: `ou_alias_1, ou_alias_2`", reply)
        self.assertIn("system.yaml.bot_open_id", reply)

    def test_whoareyou_reports_missing_bot_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "",
            "discovered_open_id": "",
            "trigger_open_ids": [],
        }

        handler.handle_message("ou_user", "chat-p2p", "/whoareyou")

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

        self.assertIn("请私聊机器人执行 `/debug-contact <open_id>`", bot.replies[-1][1])

    def test_init_command_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/init abc", message_id="m-group")

        self.assertIn("请私聊机器人执行 `/init <token>`", bot.replies[-1][1])

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

    def test_groupmode_command_without_arg_shows_group_mode_card(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/groupmode", message_id="m-group")

        card = bot.cards[-1][1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊工作态")
        action_elements = self._action_elements(card)
        actions = action_elements[0]["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["assistant", "all", "mention-only"])
        self.assertEqual(actions[0]["type"], "primary")
        self.assertEqual(action_elements[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_groupmode_command_can_use_cached_chat_type_without_message_context(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-group"] = {"sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/groupmode", message_id="m-group")

        self.assertEqual(bot.cards[-1][1]["header"]["title"]["content"], "Codex 群聊工作态")

    def test_groupmode_command_updates_group_mode_for_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/groupmode assistant", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertIn("已切换群聊工作态：`assistant`", bot.replies[-1][1])

    def test_groupmode_command_rejects_all_when_thread_is_shared(self) -> None:
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

        handler.handle_message("ou_user", "chat-group", "/groupmode all", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertIn("`all` 模式", bot.replies[-1][1])
        self.assertIn("不能与其他飞书会话共享", bot.replies[-1][1])

    def test_groupmode_command_rejects_non_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/groupmode all", message_id="m-group")

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

    def test_groupmode_card_action_updates_group_mode(self) -> None:
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

    def test_groupmode_card_action_rejects_all_when_thread_is_shared(self) -> None:
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
        self.assertIn("不能与其他飞书会话共享", response["toast"])

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

        handler.handle_message("ou_user", "oc_group123", "/groupmode", message_id="m-group")

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
                {"action": "set_collaboration_mode", "mode": "plan", "_operator_open_id": "ou_admin"},
            )
        )

        self.assertEqual(handler._get_runtime_state("ou_user", "chat-group", "m-group")["collaboration_mode"], "plan")
        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertNotIn(("ou_user", "chat-group"), self._binding_keys(handler))
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("plan", response["toast"])

    def test_resolve_runtime_binding_reuses_existing_group_state(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        first = handler._resolve_runtime_binding("ou_user", "chat-group", "m-group")
        second = handler._resolve_runtime_binding("ou_user2", "chat-group")

        self.assertEqual(first.binding, ("__group__", "chat-group"))
        self.assertEqual(second.binding, ("__group__", "chat-group"))
        self.assertIs(first.state, second.state)

    def test_sandbox_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/sandbox read-only")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["sandbox"], "read-only")
        self.assertIn("已切换沙箱策略：`read-only`", bot.replies[-1][1])
        self.assertIn("只影响当前飞书会话的后续 turn", bot.replies[-1][1])

    def test_sandbox_command_without_arg_shows_sandbox_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/sandbox")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 沙箱策略")
        self.assertIn("它只决定文件和网络边界", card["elements"][0]["content"])
        self.assertIn("优先使用 `/permissions`", card["elements"][0]["content"])

    def test_permissions_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions full-access")

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["sandbox"], "danger-full-access")
        self.assertIn("Full Access", bot.replies[-1][1])
        self.assertIn("danger-full-access", bot.replies[-1][1])

    def test_permissions_command_without_arg_shows_permissions_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 权限预设")
        self.assertIn("推荐先用这个", card["elements"][0]["content"])
        self.assertIn("优先选 `default`", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(len(action_elements), 2)
        self.assertEqual(action_elements[0]["layout"], "trisection")
        self.assertEqual(action_elements[1]["actions"][0]["text"]["content"], "返回帮助")

    def test_approval_command_without_arg_shows_approval_boundary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/approval")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 审批策略")
        self.assertIn("只决定什么时候停下来等你确认", card["elements"][0]["content"])
        self.assertIn("优先使用 `/permissions`", card["elements"][0]["content"])

    def test_mode_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_collaboration_mode", "mode": "plan"},
        ))

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["collaboration_mode"], "plan")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("plan", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 协作模式")
        self.assertEqual(self._action_elements(response["card"])[1]["actions"][0]["text"]["content"], "返回帮助")

    def test_sandbox_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_sandbox_policy", "policy": "read-only"},
        ))

        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["sandbox"], "read-only")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("read-only", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 沙箱策略")

    def test_permissions_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_permissions_preset", "preset": "full-access"},
        ))

        state = handler._get_runtime_state("ou_user", "c1")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["sandbox"], "danger-full-access")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("Full Access", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 权限预设")
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

    def test_status_includes_user_facing_summary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/status")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 当前状态")
        content = card["elements"][0]["content"]
        self.assertIn("权限预设：`Default`", content)
        self.assertIn("审批策略：`on-request`", content)
        self.assertIn("沙箱策略：`workspace-write`", content)
        self.assertIn("Codex 协作模式：`default`", content)
        self.assertNotIn("新 thread seed profile", content)
        self.assertNotIn("当前 provider", content)
        self.assertNotIn("binding：", content)

    def test_status_shows_current_thread_profile_and_hides_runtime_debug_fields(self) -> None:
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
        handler._thread_resume_profile_store.save(
            "thread-1",
            profile="provider2",
            model="provider2-model",
            model_provider="provider2_api",
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("当前 profile：`provider2`", content)
        self.assertIn("权限预设：`Default`", content)
        self.assertIn("Codex 协作模式：`default`", content)
        self.assertNotIn("binding：", content)
        self.assertNotIn("feishu runtime：", content)
        self.assertNotIn("backend thread status：", content)
        self.assertNotIn("交互 owner：", content)
        self.assertNotIn("re-profile possible：", content)
        self.assertNotIn("unsubscribe：", content)
        self.assertNotIn("当前直接提问：", content)

    def test_unsubscribe_command_releases_all_bound_bindings_but_keeps_binding(self) -> None:
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

        def _unsubscribe(thread_id: str) -> None:
            handler._adapter.unsubscribe_thread_calls.append(thread_id)
            handler._adapter.thread_snapshots[(thread_id, None)] = ThreadSnapshot(summary=unloaded)

        handler._adapter.unsubscribe_thread = _unsubscribe

        handler.handle_message("ou_user", "chat-a", "/unsubscribe")

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "chat-a")["current_thread_id"], "thread-1")
        self.assertEqual(handler._get_runtime_state("ou_user2", "chat-b")["current_thread_id"], "thread-1")
        self.assertEqual(handler._get_runtime_state("ou_user", "chat-a")["feishu_runtime_state"], "released")
        self.assertEqual(handler._get_runtime_state("ou_user2", "chat-b")["feishu_runtime_state"], "released")
        self.assertEqual(handler._thread_subscribers("thread-1"), ())
        _, card = bot.cards[-1]
        self.assertIn("backend thread status：`notLoaded`", card["elements"][0]["content"])

    def test_released_binding_hydrates_without_resubscribe_and_next_prompt_reattaches(self) -> None:
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
        handler._unsubscribe_feishu_runtime_by_thread_id("thread-1")

        handler2, _ = self._make_handler(data_dir=data_dir)
        state2 = handler2._get_runtime_state("ou_user", "c1")
        self.assertEqual(state2["current_thread_id"], "thread-1")
        self.assertEqual(state2["feishu_runtime_state"], "released")
        self.assertEqual(handler2._thread_subscribers("thread-1"), ())

        handler2.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler2._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_denied_prompt_keeps_released_binding_released_when_all_mode_group_owns_thread(self) -> None:
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
        handler._unsubscribe_feishu_runtime_by_thread_id("thread-1")
        handler._bind_thread("ou_user2", "chat-b", thread)

        handler.handle_message("ou_user", "chat-a", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(
            handler._get_runtime_state("ou_user", "chat-a")["feishu_runtime_state"],
            "released",
        )
        self.assertEqual(handler._thread_subscribers("thread-1"), (("__group__", "chat-b"),))
        self.assertIn("其他群聊独占", bot.replies[-1][1])

    def test_denied_prompt_keeps_released_binding_released_when_interaction_lease_is_external(self) -> None:
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
        handler._unsubscribe_feishu_runtime_by_thread_id("thread-1")
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:other", owner_pid=os.getpid()),
        )

        handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(handler._thread_subscribers("thread-1"), ())
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "released")
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
        result = control_request(data_dir, "thread/unsubscribe", {"thread_id": "thread-1"})

        self.assertEqual(status["binding_count"], 1)
        self.assertTrue(status["control_endpoint"].startswith("tcp://127.0.0.1:"))
        self.assertTrue(result["changed"])
        self.assertEqual(result["backend_thread_status"], "notLoaded")
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "released")

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

    def test_service_control_plane_release_runtime_name_target_resolves_explicit_exact_name(self) -> None:
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

        result = control_request(data_dir, "thread/unsubscribe", {"thread_name": "demo"})

        self.assertTrue(result["changed"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["backend_thread_status"], "notLoaded")
        self.assertEqual(result["released_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["feishu_runtime_state"], "released")

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

    def test_profile_command_without_arg_shows_bound_thread_profiles(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/profile")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Thread Profile")
        content = card["elements"][0]["content"]
        self.assertIn("当前 thread：`thread-1", content)
        self.assertIn("当前 thread-wise profile：`（未设置）`", content)
        self.assertIn("当前 thread-wise provider：未设置 thread-wise profile", content)
        self.assertIn("切换方式：发送 `/profile <name>`，或直接点下面按钮。", content)
        self.assertIn("**可用 profile**", content)
        self.assertIn("`provider1` -> `provider1_api`", content)
        self.assertIn("`provider2` -> `provider2_api`", content)
        self.assertIn("当前不能直接写入：当前 thread 尚未满足 verifiably globally unloaded", content)
        self.assertIn("**re-profile 诊断**", content)
        self.assertIn("当前实例：`default`", content)
        action = self._action_elements(card)[0]
        self.assertNotIn("layout", action)
        self.assertEqual(
            [item["text"]["content"] for item in action["actions"]],
            ["provider1", "provider2"],
        )
        self.assertEqual(
            [item["type"] for item in action["actions"]],
            ["default", "default"],
        )

    def test_profile_command_switches_bound_thread_profile(self) -> None:
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
        handler.handle_message("ou_user", "c1", "/unsubscribe")
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="hello",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="notLoaded",
            )
        )
        handler._adapter.list_loaded_thread_ids = lambda: []

        handler.handle_message("ou_user", "c1", "/profile provider2")

        self.assertEqual(handler._adapter.set_active_profile_calls, [])
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Thread Profile")
        content = card["elements"][0]["content"]
        self.assertIn("已切换当前 thread 的 profile：`provider2`", content)
        self.assertIn("当前 thread-wise provider：`provider2_api`", content)
        self.assertIn("当前已满足切换条件：thread globally unloaded。", content)
        action = self._action_elements(card)[0]
        self.assertEqual(
            [item["type"] for item in action["actions"]],
            ["default", "primary"],
        )
        saved = handler._thread_resume_profile_store.load("thread-1")
        assert saved is not None
        self.assertEqual(saved.profile, "provider2")
        self.assertEqual(saved.model_provider, "provider2_api")

    def test_profile_command_prefers_profile_mapping_for_thread_provider(self) -> None:
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
        handler._thread_resume_profile_store.save(
            "thread-1",
            profile="provider2",
            model="provider2-model",
            model_provider="",
        )
        handler._adapter.read_runtime_config = lambda **kwargs: RuntimeConfigSummary(
            current_profile="provider1",
            current_model_provider=None,
            profiles=[
                RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
            ],
        )

        handler.handle_message("ou_user", "c1", "/profile")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("当前 thread-wise profile：`provider2`", content)
        self.assertIn("当前 thread-wise provider：`provider2_api`", content)

    def test_profile_card_action_updates_thread_profile(self) -> None:
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
        handler.handle_message("ou_user", "c1", "/unsubscribe")
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="hello",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="notLoaded",
            )
        )
        handler._adapter.list_loaded_thread_ids = lambda: []

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_profile", "profile": "provider2"},
        ))

        saved = handler._thread_resume_profile_store.load("thread-1")
        assert saved is not None
        self.assertEqual(saved.profile, "provider2")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("provider2", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex Thread Profile")
        action = self._action_elements(response["card"])[0]
        self.assertNotIn("layout", action)
        self.assertEqual(
            [item["type"] for item in action["actions"]],
            ["default", "primary"],
        )

    def test_profile_command_with_unknown_name_shows_usage(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/profile provider9")

        self.assertIn("未找到 profile：`provider9`", bot.replies[-1][1])
        self.assertIn("用法：`/profile <name>`", bot.replies[-1][1])
        self.assertIn("先发 `/profile` 查看可用 profile。", bot.replies[-1][1])

    def test_rm_command_archives_current_thread_and_clears_binding(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/rm")

        self.assertEqual(handler._adapter.archive_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_runtime_state("ou_user", "c1")["current_thread_id"], "")
        self.assertIn("不是硬删除", bot.replies[-1][1])

    def test_profile_command_rejects_when_unbound(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/profile")

        self.assertIn("当前还没有绑定 thread", bot.replies[-1][1])

    def test_status_mentions_stale_local_default_profile_cleanup(self) -> None:
        handler, bot = self._make_handler()
        handler._profile_state.save_default_profile("provider9")

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        self.assertNotIn("新 thread seed profile", card["elements"][0]["content"])
        self.assertEqual(handler._profile_state.load_default_profile(), "")

    def test_new_thread_uses_local_default_profile(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        handler.handle_message("ou_user", "c1", "/new")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")
        saved = handler._thread_resume_profile_store.load("thread-created")
        assert saved is not None
        self.assertEqual(saved.profile, "provider2")

    def test_prompt_uses_local_default_profile(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["profile"], "provider2")
        saved = handler._thread_resume_profile_store.load("thread-created")
        assert saved is not None
        self.assertEqual(saved.profile, "provider2")

    def test_released_thread_resume_uses_thread_wise_profile(self) -> None:
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
        handler.handle_message("ou_user", "c1", "/unsubscribe")
        handler._thread_resume_profile_store.save(
            "thread-1",
            profile="provider2",
            model="provider2-model",
            model_provider="provider2_api",
        )
        handler._profile_state.save_default_profile("provider1")

        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["profile"], "provider2")
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["model_provider"], "provider2_api")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["profile"], "provider2")

    def test_prompt_keeps_last_known_profile_when_runtime_config_read_temporarily_fails(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        self.assertIsNotNone(handler._safe_read_runtime_config())
        handler._adapter.read_runtime_config = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("config down"))

        handler.handle_message("ou_user", "c1", "/new")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")

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
        self.assertEqual(bot.replies[-1], ("c1", "当前线程仍在执行，请等待结束或先执行 `/cancel`。"))

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

    def test_profile_command_fails_closed_when_loaded_state_cannot_be_verified(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_runtime_state("ou_user", "c1")
        with handler._lock:
            state["current_thread_id"] = "thread-1"
            state["current_thread_title"] = "demo"
            state["feishu_runtime_state"] = "released"
        handler._adapter.list_loaded_thread_ids = lambda: (_ for _ in ()).throw(RuntimeError("backend down"))

        handler.handle_message("ou_user", "c1", "/profile provider2")

        _, card = bot.cards[-1]
        content = card["elements"][0]["content"]
        self.assertIn("当前无法完整确认 backend 是否仍有运行中的 thread", content)
        reset_action = self._action_elements(card)[1]
        self.assertEqual(
            [item["text"]["content"] for item in reset_action["actions"]],
            ["强制应用并重置 backend"],
        )

    def test_prompt_after_switching_back_to_default_uses_default_collaboration_mode(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/mode plan")
        handler.handle_message("ou_user", "c1", "/mode default")
        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.start_turn_calls[-1]["collaboration_mode"], "default")

    def test_permissions_command_applies_to_thread_creation_and_turn_start(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions full-access")
        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["approval_policy"], "never")
        self.assertEqual(handler._adapter.create_thread_calls[-1]["sandbox"], "danger-full-access")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["approval_policy"], "never")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["sandbox"], "danger-full-access")

    def test_resume_thread_id_disconnect_is_not_reported_as_not_found(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            cwd="/tmp/project",
            name="feishu-cc",
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

    def test_resume_command_for_not_loaded_thread_resumes_directly(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/resume demo")

        _, pending_card = bot.cards[0]
        self.assertEqual(pending_card["header"]["title"]["content"], "Codex 正在恢复线程")
        self.assertIn("正在恢复：`demo`", pending_card["elements"][0]["content"])
        handler._runtime_call(lambda: None)
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")

    def test_session_card_mentions_global_resume_scope(self) -> None:
        handler, bot = self._make_handler()
        captured_kwargs = {}

        def fake_list_threads_all(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        handler._adapter.list_threads_all = fake_list_threads_all

        handler.handle_message("ou_user", "c1", "/session")

        self.assertEqual(captured_kwargs["model_providers"], [])
        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        content = card["elements"][0]["content"]
        self.assertIn("跨 provider 汇总", content)
        self.assertIn("`/resume <thread_id|thread_name>`", content)
        self.assertIn("`fcodex resume <thread_id|thread_name>`", content)
        self.assertIn("`feishu-codexctl thread list --scope cwd`", content)

    def test_session_card_uses_trisection_layout_for_row_actions(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/session")

        _, card = bot.cards[0]
        action_elements = self._action_elements(card)
        row_action = action_elements[0]
        self.assertEqual(row_action["layout"], "trisection")
        self.assertEqual(len(row_action["actions"]), 2)
        self.assertEqual(row_action["actions"][0]["text"]["content"], "恢复")
        self.assertEqual(row_action["actions"][1]["text"]["content"], "归档")
        bottom_action = action_elements[-1]
        self.assertTrue(any(btn["text"]["content"] == "收起" for btn in bottom_action["actions"]))

    def test_session_card_marks_current_thread_in_button_text(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/session")

        _, card = bot.cards[0]
        self.assertNotIn("**当前**", card["elements"][2]["content"])
        row_action = self._action_elements(card)[0]
        self.assertEqual(row_action["actions"][0]["text"]["content"], "当前")
        self.assertEqual(row_action["actions"][0]["type"], "primary")

    def test_session_command_rejects_extra_args(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/session extra")

        self.assertEqual(bot.cards, [])
        self.assertIn("用法：`/session`", bot.replies[-1][1])
        self.assertIn("不接受额外参数", bot.replies[-1][1])

    def test_named_instance_session_shares_global_current_dir_threads(self) -> None:
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

        handler.handle_message("ou_user", "c1", "/session")

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

    def test_close_sessions_card_action_returns_closed_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "close_sessions_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程（已收起）")
        action = self._first_action(response["card"])
        self.assertEqual(action["actions"][0]["text"]["content"], "展开会话列表")

    def test_reopen_sessions_card_action_returns_sessions_card(self) -> None:
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
            {"action": "reopen_sessions_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

    def test_show_more_sessions_action_expands_all_rows(self) -> None:
        handler, _ = self._make_handler({"session_recent_limit": 1})
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
            {"action": "show_more_sessions"},
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

    def test_expanded_sessions_card_stays_expanded_after_archive(self) -> None:
        handler, _ = self._make_handler({"session_recent_limit": 1})
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
            {"action": "show_more_sessions"},
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

    def test_expanded_sessions_card_stays_expanded_after_rename(self) -> None:
        handler, _ = self._make_handler({"session_recent_limit": 1})
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
            {"action": "show_more_sessions"},
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

    def test_resume_thread_on_runtime_submit_refreshes_sessions_card(self) -> None:
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
            refresh_session_message_id="msg-session",
        )
        handler._runtime_call(lambda: None)

        self.assertTrue(any(message_id == "msg-session" for message_id, _ in bot.patches))

    def test_expanded_sessions_card_stays_expanded_after_resume_refresh(self) -> None:
        handler, bot = self._make_handler({"session_recent_limit": 1})
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
            {"action": "show_more_sessions"},
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
        self.assertEqual(response["toast"], "正在恢复线程…")
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
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助")
        content = card["elements"][0]["content"]
        self.assertIn("`session`", content)
        self.assertIn("`settings`", content)
        self.assertIn("`group`", content)
        self.assertIn("`fcodex resume <thread_id|thread_name>`", content)
        self.assertIn("`feishu-codexctl thread list --scope cwd`", content)
        self.assertNotIn("/help local", content)
        self.assertIn("同一线程允许多端订阅观察", content)
        self.assertIn("同一 live turn 只有一个交互 owner", content)
        action = self._first_action(card)
        self.assertEqual(action["layout"], "trisection")
        self.assertEqual([item["text"]["content"] for item in action["actions"]], ["session", "settings", "group"])

    def test_help_session_mentions_resume_scope_and_new_semantics(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help session")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：线程")
        content = card["elements"][0]["content"]
        self.assertIn("`/session`", content)
        self.assertIn("`/resume <thread_id|thread_name>`", content)
        self.assertIn("`/new`", content)
        self.assertIn("`/cd <path>`", content)
        self.assertIn("`fcodex resume <thread_id|thread_name>`", content)
        self.assertIn("`feishu-codexctl thread list --scope cwd`", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["当前线程", "/session", "/new"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["恢复线程", "切换目录", "返回帮助"],
        )

    def test_help_settings_mentions_permissions_as_recommended_entry(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help settings")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：设置")
        content = card["elements"][0]["content"]
        self.assertIn("`/profile [name]`", content)
        self.assertIn("推荐先用 `/permissions`", content)
        self.assertIn("`/approval`", content)
        self.assertIn("`/sandbox`", content)
        self.assertIn("`/mode`", content)
        self.assertIn("身份与初始化", content)
        self.assertIn("如果当前正在执行，新设置从下一轮生效。", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["/profile", "/permissions", "/approval"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["/sandbox", "/mode", "身份与初始化"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["返回帮助"],
        )

    def test_help_group_card_has_shortcuts(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help group")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：群聊")
        self.assertIn("/group activate", card["elements"][0]["content"])
        self.assertIn("自己发起 turn 的审批", card["elements"][0]["content"])
        action = self._first_action(card)
        self.assertEqual([item["text"]["content"] for item in action["actions"]], ["/group", "/groupmode", "返回帮助"])

    def test_help_local_alias_resolves_to_redirect_page(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help local")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：本地命令")
        self.assertIn("`fcodex resume <thread_id|thread_name>`", card["elements"][0]["content"])
        self.assertIn("`feishu-codexctl thread list --scope cwd`", card["elements"][0]["content"])

    def test_help_page_action_returns_settings_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "settings"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：设置")
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["/profile", "/permissions", "/approval"],
        )

    def test_help_settings_shortcut_can_open_permissions_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_permissions_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 权限预设")

    def test_help_settings_shortcut_can_open_mode_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_mode_card"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 协作模式")

    def test_help_group_shortcut_can_open_groupmode_card(self) -> None:
        handler, _ = self._make_handler()
        handler.bot.message_contexts["msg-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-group",
            {"action": "show_group_mode_card", "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊工作态")

    def test_help_page_action_returns_overview_dashboard(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "overview"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助")
        action = self._first_action(response["card"])
        self.assertEqual(action["layout"], "trisection")
        self.assertEqual([item["text"]["content"] for item in action["actions"]], ["session", "settings", "group"])

    def test_help_navigation_actions_are_not_group_admin_only(self) -> None:
        handler, _ = self._make_handler()
        handler.bot.message_contexts["msg-help-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-help-group",
            {"action": "show_help_page", "page": "overview", "_operator_open_id": "ou_user"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助")

    def test_help_show_page_action_can_open_current_thread_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "session-current"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：当前线程")
        action_elements = self._action_elements(response["card"])
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["/status", "/preflight", "unsubscribe"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["/rm", "重命名", "返回 Session"],
        )

    def test_help_show_page_action_can_open_session_resume_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "session-resume-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：恢复线程")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertEqual(self._action_elements(response["card"])[0]["actions"][0]["text"]["content"], "返回上一页")

    def test_help_show_page_action_can_open_session_cd_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "session-cd-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：切换目录")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn("/cd <path>", response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_session_rename_current_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "session-rename-current-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：重命名当前线程")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn("/rename <title>", response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_settings_identity_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "settings-identity"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：身份与初始化")
        self.assertIn("/debug-contact <open_id>", response["card"]["elements"][0]["content"])
        action_elements = self._action_elements(response["card"])
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["/whoami", "/whoareyou", "初始化"],
        )

    def test_help_show_page_action_can_open_settings_init_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "settings-init-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：初始化")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn("/init <token>", response["card"]["elements"][0]["content"])

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

        self.assertIn("帮助主题仅支持", bot.replies[-1][1])

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

        self.assertEqual(response["toast"], "请私聊机器人执行 `/init <token>`。")
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
        self.assertIn("正在恢复线程", response["toast"])
        handler._runtime_call(lambda: None)

    def test_resume_card_action_failure_refreshes_sessions_card(self) -> None:
        handler, bot = self._make_handler({"session_recent_limit": 1})
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
            {"action": "show_more_sessions"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-missing", "thread_title": "missing"},
        ))

        self.assertEqual(response["toast"], "正在恢复线程…")
        handler._runtime_call(lambda: None)

        self.assertIn("恢复线程失败", bot.replies[-1][1])
        patched = json.loads(next(content for message_id, content in bot.patches if message_id == "msg-session"))
        content = "\n".join(
            element.get("content", "")
            for element in patched["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("thread-1", content)

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
