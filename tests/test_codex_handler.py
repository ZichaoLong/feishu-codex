import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.cards import build_ask_user_card
from bot.adapters.base import ThreadSummary
from bot.codex_handler import CodexHandler


class _FakeAdapter:
    def __init__(self, config, *, on_notification=None, on_request=None) -> None:
        self.config = config
        self.on_notification = on_notification
        self.on_request = on_request

    def stop(self) -> None:
        return None


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
