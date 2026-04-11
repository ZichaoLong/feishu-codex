import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.cards import build_ask_user_card, build_execution_card
from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary, ThreadSnapshot, ThreadSummary
from bot.codex_handler import CodexHandler
from bot.codex_protocol.client import CodexRpcError


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
        raise NotImplementedError

    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        return RuntimeConfigSummary(
            current_profile=self.last_profile,
            current_model_provider=f"{self.last_profile}_api",
            profiles=[
                RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
            ],
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

    def archive_thread(self, thread_id: str) -> None:
        self.archive_thread_calls.append(thread_id)

    def start_turn(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
    ):
        self.start_turn_calls.append(
            {
                "thread_id": thread_id,
                "text": text,
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
        self.reply_parents: list[tuple[str, str, str]] = []
        self.card_parents: list[tuple[str, dict, str]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.message_contexts: dict[str, dict] = {}
        self.group_modes: dict[str, str] = {}
        self.group_acls: dict[str, dict] = {}
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

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.replies.append((chat_id, text))
        if parent_message_id:
            self.reply_parents.append((chat_id, text, parent_message_id))

    def reply_card(self, chat_id: str, card: dict, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.cards.append((chat_id, card))
        if parent_message_id:
            self.card_parents.append((chat_id, card, parent_message_id))

    def reply_to_message(self, parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str:
        self.reply_refs.append((parent_id, msg_type, content))
        return "plan-card-1"

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str:
        self.sent_messages.append((chat_id, msg_type, content))
        return "plan-card-2"

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return True

    def make_card_response(self, card=None, toast=None, toast_type="info"):
        return {"card": card, "toast": toast, "toast_type": toast_type}

    def get_message_context(self, message_id: str) -> dict:
        return dict(self.message_contexts.get(message_id, {}))

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

    def get_group_acl_snapshot(self, chat_id: str) -> dict:
        acl = self.group_acls.setdefault(
            chat_id,
            {"access_policy": "admin-only", "allowlist": []},
        )
        return {"access_policy": acl["access_policy"], "allowlist": list(acl["allowlist"])}

    def set_group_access_policy(self, chat_id: str, policy: str) -> str:
        acl = self.group_acls.setdefault(
            chat_id,
            {"access_policy": "admin-only", "allowlist": []},
        )
        acl["access_policy"] = policy
        return policy

    def grant_group_members(self, chat_id: str, open_ids) -> list[str]:
        acl = self.group_acls.setdefault(
            chat_id,
            {"access_policy": "admin-only", "allowlist": []},
        )
        merged = sorted(set(acl["allowlist"]) | {item for item in open_ids if item})
        acl["allowlist"] = merged
        return merged

    def revoke_group_members(self, chat_id: str, open_ids) -> list[str]:
        acl = self.group_acls.setdefault(
            chat_id,
            {"access_policy": "admin-only", "allowlist": []},
        )
        remaining = sorted(set(acl["allowlist"]) - {item for item in open_ids if item})
        acl["allowlist"] = remaining
        return remaining

    def is_group_admin(self, *, open_id: str = "") -> bool:
        return self.is_admin(open_id=open_id)

    def is_group_user_allowed(self, chat_id: str, *, open_id: str = "") -> bool:
        if self.is_group_admin(open_id=open_id):
            return True
        acl = self.group_acls.setdefault(
            chat_id,
            {"access_policy": "admin-only", "allowlist": []},
        )
        if acl["access_policy"] == "all-members":
            return True
        if acl["access_policy"] == "allowlist":
            return open_id in set(acl["allowlist"])
        return False

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

    def _make_handler(self) -> tuple[CodexHandler, _FakeBot]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        config_patch = patch("bot.codex_handler.load_config_file", return_value={})
        adapter_patch = patch("bot.codex_handler.CodexAppServerAdapter", _FakeAdapter)
        config_patch.start()
        adapter_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(adapter_patch.stop)
        handler = CodexHandler(data_dir=data_dir)
        bot = _FakeBot(data_dir)
        handler.bot = bot
        return handler, bot

    def test_mode_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/mode plan")

        state = handler._get_state("ou_user", "c1")
        self.assertEqual(state["collaboration_mode"], "plan")
        self.assertIn("已切换协作模式：`plan`", bot.replies[-1][1])
        self.assertIn("只影响当前飞书会话的后续 turn", bot.replies[-1][1])

    def test_on_register_eagerly_starts_adapter(self) -> None:
        handler, bot = self._make_handler()

        handler.on_register(bot)

        self.assertIs(handler.bot, bot)
        self.assertEqual(handler._adapter.start_calls, 1)

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
        state = handler._get_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "old-card"
            state["full_reply_text"] = "收到"
            state["full_log_text"] = "old log"
            state["running"] = False

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-2"}})
        handler._handle_agent_message_delta({"threadId": "thread-1", "delta": "新的回复"})

        self.assertEqual(len(bot.sent_messages), 1)
        self.assertEqual(handler._get_state("ou_user", "c1")["current_message_id"], "plan-card-2")
        self.assertEqual(handler._get_state("ou_user", "c1")["full_reply_text"], "新的回复")

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
        state = handler._get_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "old-card"
            state["full_reply_text"] = "上一轮回复"
            state["full_log_text"] = "上一轮日志"
            state["running"] = False

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-2"}})

        self.assertTrue(any(message_id == "old-card" for message_id, _ in bot.patches))
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
        self.assertEqual(handler._get_state("ou_user", "c1")["current_message_id"], "plan-card-2")

    def test_prompt_start_response_sets_current_turn_id_immediately(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        state = handler._get_state("ou_user", "c1")
        self.assertEqual(state["current_turn_id"], "turn-1")

    def test_cancel_before_turn_started_is_applied_after_turn_started(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        state = handler._get_state("ou_user", "c1")
        with handler._lock:
            state["current_turn_id"] = ""

        ok, message = handler._cancel_current_turn("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(handler._adapter.interrupt_turn_calls, [])
        self.assertTrue(handler._get_state("ou_user", "c1")["pending_cancel"])

        handler._handle_turn_started({"threadId": "thread-created", "turn": {"id": "turn-1"}})

        self.assertEqual(
            handler._adapter.interrupt_turn_calls,
            [{"thread_id": "thread-created", "turn_id": "turn-1"}],
        )
        self.assertFalse(handler._get_state("ou_user", "c1")["pending_cancel"])

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
        self.assertIs(handler._get_state("ou_user", "chat-group"), handler._get_state("ou_user2", "chat-group"))

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
        state = handler._get_state("ou_user", "c1")
        with handler._lock:
            state["current_message_id"] = "existing-card"
            state["pending_local_turn_card"] = True
            state["running"] = True

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})

        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(handler._get_state("ou_user", "c1")["current_message_id"], "existing-card")

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

    def test_group_followup_reply_stays_on_trigger_message(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        handler._card_reply_limit = 5

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        handler._handle_agent_message_delta({"threadId": "thread-created", "delta": "123456789"})
        handler._handle_turn_completed({"threadId": "thread-created", "turn": {"status": "completed"}})

        self.assertEqual(bot.reply_parents[-1], ("chat-group", "123456789", "m-thread"))

    def test_takeover_notifies_previous_feishu_binding(self) -> None:
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

        self.assertEqual(bot.replies[-1][0], "chat-a")
        self.assertIn("已被另一飞书会话接管", bot.replies[-1][1])

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
        card = build_execution_card("", running=True)

        self.assertTrue(card["config"]["update_multi"])

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
            return_value={"app_id": "cli_test_app", "app_secret": "secret"},
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

    def test_groupmode_command_rejects_non_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/groupmode all", message_id="m-group")

        self.assertIn("群里的 `/` 命令仅管理员可用", bot.replies[-1][1])
        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")

    def test_acl_policy_command_updates_group_policy(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/acl policy all-members", message_id="m-group")

        self.assertEqual(bot.get_group_acl_snapshot("chat-group")["access_policy"], "all-members")
        self.assertIn("已切换群聊授权策略：`all-members`", bot.replies[-1][1])

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

    def test_group_acl_policy_card_action_updates_group_acl(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_acl_policy", "policy": "all-members", "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(handler.bot.get_group_acl_snapshot("chat-group")["access_policy"], "all-members")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("all-members", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊授权")
        markdown = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("/acl policy admin-only", markdown)
        self.assertIn("/acl policy allowlist", markdown)
        self.assertIn("/acl policy all-members", markdown)

    def test_acl_grant_uses_message_mentions(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-grant"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
            "mentions": [{"user_id": "u2", "open_id": "ou_user2", "name": "Alice"}],
        }

        handler.handle_message("ou_user", "chat-group", "/acl grant", message_id="m-grant")

        snapshot = bot.get_group_acl_snapshot("chat-group")
        self.assertEqual(snapshot["allowlist"], ["ou_user2"])
        self.assertIn("已授权：Alice", bot.replies[-1][1])

    def test_acl_card_shows_readable_allowlist_names(self) -> None:
        handler, bot = self._make_handler()
        bot.group_acls["chat-group"] = {
            "access_policy": "allowlist",
            "allowlist": ["ou_user2"],
        }
        bot.message_contexts["m-acl"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/acl", message_id="m-acl")

        _, card = bot.cards[-1]
        markdown = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("Alice", markdown)
        self.assertNotIn("ou_user2", markdown)

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

        self.assertIn(("__group__", "chat-group"), handler._states)
        self.assertNotIn(("ou_user", "chat-group"), handler._states)
        self.assertIs(handler._get_state("ou_user", "chat-group"), handler._get_state("ou_user2", "chat-group"))

    def test_sandbox_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/sandbox read-only")

        state = handler._get_state("ou_user", "c1")
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

        state = handler._get_state("ou_user", "c1")
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

        self.assertEqual(handler._get_state("ou_user", "c1")["collaboration_mode"], "plan")
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

        self.assertEqual(handler._get_state("ou_user", "c1")["sandbox"], "read-only")
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

        state = handler._get_state("ou_user", "c1")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["sandbox"], "danger-full-access")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("Full Access", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 权限预设")
        self.assertEqual(self._action_elements(response["card"])[1]["actions"][0]["text"]["content"], "返回帮助")

    def test_turn_plan_updated_sends_then_patches_plan_card(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_state("ou_user", "c1")
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
        state = handler._get_state("ou_user", "c1")
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

    def test_execution_card_shows_help_hint(self) -> None:
        card = build_execution_card("", "", running=True)

        self.assertIn("/help", card["body"]["elements"][0]["content"])

    def test_status_includes_user_facing_summary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/status")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 当前状态")
        content = card["elements"][0]["content"]
        self.assertIn("默认 profile：`（未设置）`", content)
        self.assertIn("当前 provider：`provider1_api`", content)
        self.assertIn("权限预设：`Default`", content)
        self.assertIn("审批策略：`on-request`", content)
        self.assertIn("沙箱策略：`workspace-write`", content)
        self.assertIn("直接发送普通文本，会在当前目录自动新建线程。", content)

    def test_profile_command_without_arg_shows_runtime_profiles(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/profile")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 默认 Profile")
        content = card["elements"][0]["content"]
        self.assertIn("当前默认 profile：`（未设置）`", content)
        self.assertIn("默认 profile 对应 provider：跟随 Codex 原生默认", content)
        self.assertIn("切换方式：`/profile <name>`，例如：`/profile provider1`", content)
        self.assertIn("**可用 profile**", content)
        self.assertIn("`provider1` -> `provider1_api`", content)
        self.assertIn("`provider2` -> `provider2_api`", content)
        self.assertIn("**说明**", content)
        self.assertIn("作用范围：只影响 feishu-codex 与新的默认 `fcodex` 启动；不改裸 `codex`。", content)
        self.assertIn("已打开的 `fcodex` TUI 不会热切换。", content)

    def test_profile_command_switches_local_default_profile(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/profile provider2")

        self.assertEqual(handler._adapter.set_active_profile_calls, [])
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 默认 Profile")
        content = card["elements"][0]["content"]
        self.assertIn("已切换默认 profile：`provider2`", content)
        self.assertIn("默认 profile 对应 provider：`provider2_api`", content)
        self.assertIn("再次切换：`/profile <name>`", content)
        self.assertIn("**说明**", content)
        self.assertIn("作用范围：只影响 feishu-codex 与新的默认 `fcodex` 启动；不改裸 `codex`。", content)
        self.assertIn("已打开的 `fcodex` TUI 不会热切换。", content)
        self.assertEqual(handler._profile_state.load_default_profile(), "provider2")

    def test_profile_command_prefers_profile_mapping_for_default_provider(self) -> None:
        handler, bot = self._make_handler()
        handler._profile_state.save_default_profile("provider2")
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
        self.assertIn("当前默认 profile：`provider2`", content)
        self.assertIn("默认 profile 对应 provider：`provider2_api`", content)
        self.assertNotIn("当前运行时 provider", content)

    def test_profile_command_with_unknown_name_shows_usage(self) -> None:
        handler, bot = self._make_handler()

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
        handler._favorites.toggle("ou_user", "thread-1")
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/rm")

        self.assertEqual(handler._adapter.archive_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_state("ou_user", "c1")["current_thread_id"], "")
        self.assertFalse(handler._favorites.is_starred("ou_user", "thread-1"))
        self.assertIn("不是硬删除", bot.replies[-1][1])

    def test_rm_command_clears_favorites_for_all_users(self) -> None:
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
        handler._favorites.toggle("ou_user", "thread-1")
        handler._favorites.toggle("ou_user2", "thread-1")
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/rm")

        self.assertFalse(handler._favorites.is_starred("ou_user", "thread-1"))
        self.assertFalse(handler._favorites.is_starred("ou_user2", "thread-1"))

    def test_profile_command_clears_stale_local_default_profile(self) -> None:
        handler, bot = self._make_handler()
        handler._profile_state.save_default_profile("provider9")

        handler.handle_message("ou_user", "c1", "/profile")

        _, card = bot.cards[-1]
        self.assertIn("已不存在，现已自动清空并回退到 Codex 原生默认", card["elements"][0]["content"])
        self.assertEqual(handler._profile_state.load_default_profile(), "")

    def test_status_mentions_stale_local_default_profile_cleanup(self) -> None:
        handler, bot = self._make_handler()
        handler._profile_state.save_default_profile("provider9")

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        self.assertIn("已自动回退到 Codex 原生默认", card["elements"][0]["content"])
        self.assertEqual(handler._profile_state.load_default_profile(), "")

    def test_new_thread_uses_local_default_profile(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        handler.handle_message("ou_user", "c1", "/new")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")

    def test_prompt_uses_local_default_profile(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["profile"], "provider2")

    def test_prompt_reuses_reserved_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.reserved_execution_cards["m1"] = "reserved-card"

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        self.assertEqual(handler._get_state("ou_user", "c1")["current_message_id"], "reserved-card")
        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(bot.patches[-1][0], "reserved-card")

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

    def test_resume_command_for_not_loaded_thread_shows_guard_card(self) -> None:
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

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "恢复线程前确认")

    def test_resume_guard_preview_action_returns_handled_card(self) -> None:
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
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-guard",
            {"action": "preview_thread_snapshot", "thread_id": "thread-1"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "恢复线程前确认（已处理）")
        self.assertIn("已选择“查看快照”", response["card"]["elements"][0]["content"])
        self.assertFalse(any(element.get("tag") == "action" for element in response["card"]["elements"]))

    def test_resume_guard_write_action_returns_handled_card(self) -> None:
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
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-guard",
            {"action": "resume_thread_write", "thread_id": "thread-1"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "恢复线程前确认（已处理）")
        self.assertIn("已选择“恢复并继续写入”", response["card"]["elements"][0]["content"])
        self.assertFalse(any(element.get("tag") == "action" for element in response["card"]["elements"]))

    def test_resume_guard_cancel_action_returns_handled_card(self) -> None:
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
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-guard",
            {"action": "cancel_resume_guard", "thread_id": "thread-1"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "恢复线程前确认（已处理）")
        self.assertIn("已取消本次恢复", response["card"]["elements"][0]["content"])
        self.assertFalse(any(element.get("tag") == "action" for element in response["card"]["elements"]))

    def test_resume_guard_cancel_action_from_sessions_returns_sessions_card(self) -> None:
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
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "cancel_resume_guard", "thread_id": "thread-1", "return_to_sessions": True},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

    def test_resume_guard_preview_action_from_sessions_returns_sessions_card(self) -> None:
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
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "preview_thread_snapshot", "thread_id": "thread-1", "return_to_sessions": True},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

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
        self.assertIn("跨 provider 汇总", card["elements"][0]["content"])
        self.assertIn("`/resume <thread_id|thread_name>`", card["elements"][0]["content"])
        self.assertIn("`/help local`", card["elements"][0]["content"])

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
        self.assertEqual(action_elements[0]["actions"][0]["text"]["content"], "收起")
        self.assertEqual(action_elements[1]["layout"], "trisection")
        self.assertEqual(action_elements[1]["actions"][2]["text"]["content"], "归档")

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

    def test_resume_thread_in_background_refreshes_sessions_card(self) -> None:
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

        handler._resume_thread_in_background(
            "ou_user",
            "c1",
            "thread-1",
            original_arg="thread-1",
            summary=thread,
            message_id="msg-session",
            refresh_session_message_id="msg-session",
        )

        self.assertTrue(any(message_id == "msg-session" for message_id, _ in bot.patches))

    def test_help_overview_is_layered(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助")
        content = card["elements"][0]["content"]
        self.assertIn("/new", content)
        self.assertIn("/session", content)
        self.assertIn("/resume <thread_id|thread_name>", content)
        self.assertIn("/cd <path>", content)
        self.assertIn("/status", content)
        self.assertNotIn("/cancel", content)
        self.assertIn("/whoareyou", content)
        self.assertIn("/help session", content)
        self.assertIn("/help settings", content)
        self.assertIn("/help local", content)
        self.assertIn("如需在本地继续同一线程，请使用 `fcodex`", content)
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
        self.assertIn("`/session` 只列当前目录的线程", content)
        self.assertIn("`/resume <thread_id|thread_name>` 会做全局精确匹配", content)
        self.assertIn("`/new` 立即新建并切换到新线程", content)
        self.assertEqual(self._first_action(card)["actions"][0]["text"]["content"], "返回帮助")

    def test_help_settings_mentions_permissions_as_recommended_entry(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help settings")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：设置")
        content = card["elements"][0]["content"]
        self.assertIn("`/profile` 查看或切换默认 profile", content)
        self.assertIn("推荐先用 `/permissions`", content)
        self.assertIn("model_provider", content)
        self.assertIn("只影响当前飞书会话的后续 turn", content)
        self.assertIn("/profile [name]", content)
        self.assertIn("/approval [untrusted|on-failure|on-request|never]", content)
        self.assertIn("/sandbox [read-only|workspace-write|danger-full-access]", content)
        self.assertIn("如果当前正在执行，新设置从下一轮生效。", content)
        action = self._first_action(card)
        self.assertEqual(action["layout"], "trisection")
        self.assertEqual([item["text"]["content"] for item in action["actions"]], ["/permissions", "/mode", "返回帮助"])

    def test_help_group_card_has_shortcuts(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help group")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：群聊")
        self.assertIn("`assistant` 会缓存群聊消息", card["elements"][0]["content"])
        action = self._first_action(card)
        self.assertEqual([item["text"]["content"] for item in action["actions"]], ["/groupmode", "返回帮助"])

    def test_help_local_explains_wrapper_and_tui_resume_difference(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help local")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 帮助：本地继续")
        content = card["elements"][0]["content"]
        self.assertIn("`fcodex` 是 `codex --remote` 的 wrapper", content)
        self.assertIn("跨 provider 找线程", content)
        self.assertIn("不等同于 `fcodex /resume`", content)

    def test_help_topic_action_returns_topic_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_topic", "topic": "settings"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助：设置")
        self.assertEqual(
            [item["text"]["content"] for item in self._first_action(response["card"])["actions"]],
            ["/permissions", "/mode", "返回帮助"],
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

    def test_help_back_action_returns_overview_dashboard(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_overview"},
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
            {"action": "show_help_overview", "_operator_open_id": "ou_user"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 帮助")

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

        self.assertEqual(handler._get_state("ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(handler._get_state("ou_user", "c1")["current_thread_id"], "")
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录已切换")
        self.assertIn("当前线程绑定已清空。", card["elements"][0]["content"])

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

    def test_resume_card_action_for_not_loaded_thread_returns_guard_card(self) -> None:
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
            {"action": "resume_thread", "thread_id": "thread-1"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "恢复线程前确认")

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

        self.assertEqual(handler._pending_rename_forms["msg-rename"]["thread_id"], "thread-1")
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
        handler._pending_rename_forms["msg-rename"] = {"thread_id": "thread-1"}
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
        self.assertNotIn("msg-rename", handler._pending_rename_forms)
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
        handler._pending_requests["req-1"] = {
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
        }

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
        handler._pending_requests["req-1"] = {
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
        }

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
