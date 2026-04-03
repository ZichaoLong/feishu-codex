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
    def __init__(self, config, *, on_notification=None, on_request=None) -> None:
        self.config = config
        self.on_notification = on_notification
        self.on_request = on_request
        self.start_calls = 0
        self.last_profile = "provider1"
        self.set_active_profile_calls: list[str] = []
        self.create_thread_calls: list[dict] = []
        self.resume_thread_calls: list[dict] = []
        self.start_turn_calls: list[dict] = []
        self.archive_thread_calls: list[str] = []

    def stop(self) -> None:
        return None

    def start(self) -> None:
        self.start_calls += 1

    def create_thread(self, *, cwd: str, profile: str | None = None):
        self.create_thread_calls.append({"cwd": cwd, "profile": profile})
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

    def resume_thread(self, thread_id: str, profile: str | None = None):
        self.resume_thread_calls.append({"thread_id": thread_id, "profile": profile})
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
                "reasoning_effort": reasoning_effort,
                "collaboration_mode": collaboration_mode,
            }
        )
        return {"turn": {"id": "turn-1"}}


class _FakeBot:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []
        self.reply_refs: list[tuple[str, str, str]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []

    def reply(self, chat_id: str, text: str) -> None:
        self.replies.append((chat_id, text))

    def reply_card(self, chat_id: str, card: dict) -> None:
        self.cards.append((chat_id, card))

    def reply_to_message(self, parent_id: str, msg_type: str, content: str) -> str:
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


class CodexHandlerTests(unittest.TestCase):
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
        bot = _FakeBot()
        handler.bot = bot
        return handler, bot

    def test_mode_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("u1", "c1", "/mode plan")

        state = handler._get_state("u1", "c1")
        self.assertEqual(state["collaboration_mode"], "plan")
        self.assertEqual(bot.replies[-1], ("c1", "协作模式已切换为：`plan`"))

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
        handler._bind_thread("u1", "c1", thread)
        state = handler._get_state("u1", "c1")
        with handler._lock:
            state["current_message_id"] = "old-card"
            state["full_reply_text"] = "收到"
            state["full_log_text"] = "old log"
            state["running"] = False

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-2"}})
        handler._handle_agent_message_delta({"threadId": "thread-1", "delta": "新的回复"})

        self.assertEqual(len(bot.sent_messages), 1)
        self.assertEqual(handler._get_state("u1", "c1")["current_message_id"], "plan-card-2")
        self.assertEqual(handler._get_state("u1", "c1")["full_reply_text"], "新的回复")

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
        handler._bind_thread("u1", "c1", thread)
        state = handler._get_state("u1", "c1")
        with handler._lock:
            state["current_message_id"] = "existing-card"
            state["pending_local_turn_card"] = True
            state["running"] = True

        handler._handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})

        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(handler._get_state("u1", "c1")["current_message_id"], "existing-card")

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

        handler._bind_thread("u1", "chat-a", thread)
        handler._bind_thread("u1", "chat-b", thread)

        self.assertEqual(bot.replies[-1][0], "chat-a")
        self.assertIn("已被另一飞书会话接管", bot.replies[-1][1])

    def test_mode_command_without_arg_shows_mode_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("u1", "c1", "/mode")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 协作模式")

    def test_mode_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = handler.handle_card_action(
            "u1",
            "c1",
            "m1",
            {"action": "set_collaboration_mode", "mode": "plan"},
        )

        self.assertEqual(handler._get_state("u1", "c1")["collaboration_mode"], "plan")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("plan", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 协作模式")

    def test_turn_plan_updated_sends_then_patches_plan_card(self) -> None:
        handler, bot = self._make_handler()
        state = handler._get_state("u1", "c1")
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
        handler._bind_thread("u1", "c1", thread)
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
        state = handler._get_state("u1", "c1")
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
        handler._bind_thread("u1", "c1", thread)
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

    def test_status_includes_backend_hint(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("u1", "c1", "/status")

        self.assertIn("后端：`managed` `ws://127.0.0.1:8765`", bot.replies[-1][1])
        self.assertIn("feishu-codex 默认 profile：`（未设置）`", bot.replies[-1][1])
        self.assertIn("当前运行时 provider：`provider1_api`", bot.replies[-1][1])
        self.assertIn("fcodex", bot.replies[-1][1])
        self.assertIn("飞书 `/session` 仅列当前目录线程", bot.replies[-1][1])
        self.assertIn("飞书 `/resume` 按后端全局精确匹配（可跨 provider）", bot.replies[-1][1])
        self.assertIn("`fcodex /session`、`fcodex /resume <thread_name>` 复用与飞书一致的共享发现逻辑", bot.replies[-1][1])
        self.assertIn("`fcodex resume <id>` 以及进入 TUI 后的 `/resume` 仍是 upstream 原样", bot.replies[-1][1])
        self.assertIn("`fcodex` shell wrapper 自命令只有 `fcodex /help`、`/profile`、`/rm`、`/session`、`/resume`", bot.replies[-1][1])

    def test_profile_command_without_arg_shows_runtime_profiles(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("u1", "c1", "/profile")

        reply = bot.replies[-1][1]
        self.assertIn("feishu-codex 默认 profile：`（未设置）`", reply)
        self.assertIn("`provider1` -> `provider1_api`", reply)
        self.assertIn("`provider2` -> `provider2_api`", reply)
        self.assertIn("不会改动裸 `codex` 全局配置", reply)

    def test_profile_command_switches_local_default_profile(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("u1", "c1", "/profile provider2")

        self.assertEqual(handler._adapter.set_active_profile_calls, [])
        reply = bot.replies[-1][1]
        self.assertIn("feishu-codex 默认 profile 已切换为：`provider2`", reply)
        self.assertIn("对应 provider：`provider2_api`", reply)
        self.assertEqual(handler._profile_state.load_default_profile(), "provider2")

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
        handler._bind_thread("u1", "c1", thread)
        handler._favorites.toggle("u1", "thread-1")
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        handler.handle_message("u1", "c1", "/rm")

        self.assertEqual(handler._adapter.archive_thread_calls, ["thread-1"])
        self.assertEqual(handler._get_state("u1", "c1")["current_thread_id"], "")
        self.assertFalse(handler._favorites.is_starred("u1", "thread-1"))
        self.assertIn("不是硬删除", bot.replies[-1][1])

    def test_profile_command_clears_stale_local_default_profile(self) -> None:
        handler, bot = self._make_handler()
        handler._profile_state.save_default_profile("provider9")

        handler.handle_message("u1", "c1", "/profile")

        reply = bot.replies[-1][1]
        self.assertIn("已不存在，现已自动清空并回退到 Codex 原生默认", reply)
        self.assertEqual(handler._profile_state.load_default_profile(), "")

    def test_status_mentions_stale_local_default_profile_cleanup(self) -> None:
        handler, bot = self._make_handler()
        handler._profile_state.save_default_profile("provider9")

        handler.handle_message("u1", "c1", "/status")

        reply = bot.replies[-1][1]
        self.assertIn("已自动回退到 Codex 原生默认", reply)
        self.assertEqual(handler._profile_state.load_default_profile(), "")

    def test_new_thread_uses_local_default_profile(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        handler.handle_message("u1", "c1", "/new")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")

    def test_prompt_uses_local_default_profile(self) -> None:
        handler, _ = self._make_handler()
        handler._profile_state.save_default_profile("provider2")

        handler.handle_message("u1", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["profile"], "provider2")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["profile"], "provider2")

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

        def fake_resume_thread(thread_id: str, profile: str | None = None):
            raise CodexRpcError("thread/resume", {"code": -32000, "message": "Codex websocket disconnected"})

        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = fake_resume_thread

        with self.assertRaisesRegex(RuntimeError, "无法通过 app-server 恢复这个 CLI 线程"):
            handler._resume_snapshot(thread.thread_id)

    def test_resume_thread_id_not_found_returns_value_error(self) -> None:
        handler, _ = self._make_handler()
        handler._adapter.list_threads_all = lambda **kwargs: []

        def fake_resume_thread(thread_id: str, profile: str | None = None):
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

        def fake_resume_thread(thread_id: str, profile: str | None = None):
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
        handler._adapter.resume_thread = lambda thread_id, profile=None: ThreadSnapshot(summary=thread)

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

        handler.handle_message("u1", "c1", "/resume demo")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "恢复线程前确认")

    def test_session_card_mentions_global_resume_scope(self) -> None:
        handler, bot = self._make_handler()
        captured_kwargs = {}

        def fake_list_threads_all(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        handler._adapter.list_threads_all = fake_list_threads_all

        handler.handle_message("u1", "c1", "/session")

        self.assertEqual(captured_kwargs["model_providers"], [])
        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertIn("跨 provider 汇总", card["elements"][0]["content"])
        self.assertIn("全局恢复请用 `/resume <thread_id|thread_name>`", card["elements"][0]["content"])
        self.assertIn("`fcodex /session`、`fcodex /resume <thread_name>` 与飞书复用同一套共享发现逻辑", card["elements"][0]["content"])
        self.assertIn("进入 TUI 后，`/resume` 仍保持 upstream 原样", card["elements"][0]["content"])

    def test_help_mentions_session_and_resume_scope_difference(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("u1", "c1", "/help")

        self.assertIn("飞书 `/session` 只列当前目录线程，但会跨 provider 汇总", bot.replies[-1][1])
        self.assertIn("飞书 `/resume` 按后端全局精确匹配", bot.replies[-1][1])
        self.assertIn("/profile", bot.replies[-1][1])
        self.assertIn("`fcodex` shell wrapper 自命令只有 `fcodex /help`、`/profile`、`/rm`、`/session`、`/resume`", bot.replies[-1][1])
        self.assertIn("不能与裸 `codex` 的 flags 或子命令混用", bot.replies[-1][1])
        self.assertIn("`fcodex resume <id>` 以及进入 TUI 后的 `/resume` 仍是 upstream 原样", bot.replies[-1][1])
        self.assertIn("`fcodex /session`、`fcodex /resume <thread_name>` 复用与飞书一致的共享发现逻辑", bot.replies[-1][1])
        self.assertIn("`fcodex /session` 或 `fcodex /session global`", bot.replies[-1][1])
        self.assertIn("docs/session-profile-semantics.md", bot.replies[-1][1])

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

        response = handler.handle_card_action(
            "u1",
            "c1",
            "msg-1",
            {"action": "resume_thread", "thread_id": "thread-1"},
        )

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

        response = handler.handle_card_action(
            "u1",
            "c1",
            "msg-rename",
            {"action": "show_rename_form", "thread_id": "thread-1"},
        )

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

        response = handler.handle_card_action(
            "u1",
            "c1",
            "msg-rename",
            {"_form_value": {"rename_title": "new-title"}},
        )

        self.assertEqual(renamed, {"thread_id": "thread-1", "name": "new-title"})
        self.assertNotIn("msg-rename", handler._pending_rename_forms)
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已重命名。")

    def test_form_value_only_callback_without_pending_rename_returns_warning(self) -> None:
        handler, _ = self._make_handler()

        response = handler.handle_card_action(
            "u1",
            "c1",
            "msg-rename",
            {"_form_value": {"rename_title": "new-title"}},
        )

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

        response = handler._handle_user_input_action(
            {
                "request_id": "req-1",
                "action": "answer_user_input_custom",
                "question_id": "q1",
                "_form_value": {"user_input_q1": "自定义"},
            }
        )

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

        response = handler.handle_card_action(
            "u1",
            "c1",
            "msg-1",
            {"_form_value": {"user_input_q1": "创建 c.txt"}},
        )

        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已提交回答。")
        self.assertEqual(responded["request_id"], "rpc-1")
        self.assertEqual(
            responded["result"],
            {"answers": {"q1": {"answers": ["创建 c.txt"]}}},
        )

    def test_form_value_only_callback_without_pending_request_returns_warning(self) -> None:
        handler, _ = self._make_handler()

        response = handler.handle_card_action(
            "u1",
            "c1",
            "missing-msg",
            {"_form_value": {"user_input_q1": "创建 c.txt"}},
        )

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "表单已失效或未找到对应问题，请重新触发该请求。")


if __name__ == "__main__":
    unittest.main()
