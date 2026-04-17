import json
import threading
import unittest

from bot.interaction_request_controller import InteractionRequestController


class InteractionRequestControllerTests(unittest.TestCase):
    @staticmethod
    def _unpack_card_response(response) -> dict:
        result: dict = {}
        if getattr(response, "card", None):
            result["card"] = response.card.data
        if getattr(response, "toast", None):
            result["toast"] = response.toast.content
            result["toast_type"] = response.toast.type
        return result

    def _make_controller(self):
        lock = threading.RLock()
        pending_requests: dict[str, dict] = {}
        state = {
            "current_prompt_message_id": "prompt-1",
            "current_prompt_reply_in_thread": True,
            "current_actor_open_id": "ou_actor",
        }
        sent_cards: list[tuple[str, dict, str, bool]] = []
        replies: list[tuple[str, str, str, bool]] = []
        responses: list[tuple[object, dict | None, dict | None]] = []
        patches: list[tuple[str, dict]] = []

        controller = InteractionRequestController(
            lock=lock,
            pending_requests=pending_requests,
            get_runtime_state=lambda sender_id, chat_id: state,
            interactive_binding_for_thread=lambda thread_id, adopt_sole_subscriber: (("ou_user", "chat-1"), False),
            send_interactive_card=lambda chat_id, card, prompt_message_id, prompt_reply_in_thread: sent_cards.append(
                (chat_id, card, prompt_message_id, prompt_reply_in_thread)
            )
            or "msg-card-1",
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: replies.append(
                (chat_id, text, message_id, reply_in_thread)
            ),
            respond=lambda request_id, *, result=None, error=None: responses.append((request_id, result, error)),
            patch_message=lambda message_id, content: patches.append((message_id, json.loads(content))) or True,
        )
        return controller, pending_requests, sent_cards, replies, responses, patches

    def test_handle_adapter_request_registers_pending_request_and_routes_to_prompt_anchor(self) -> None:
        controller, pending_requests, sent_cards, _, _, _ = self._make_controller()

        controller.handle_adapter_request(
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(len(sent_cards), 1)
        self.assertEqual(sent_cards[0][0], "chat-1")
        self.assertEqual(sent_cards[0][2], "prompt-1")
        self.assertTrue(sent_cards[0][3])
        self.assertIn("req-1", pending_requests)
        self.assertEqual(pending_requests["req-1"]["thread_id"], "thread-1")
        self.assertEqual(pending_requests["req-1"]["actor_open_id"], "ou_actor")
        self.assertEqual(pending_requests["req-1"]["message_id"], "msg-card-1")

    def test_handle_approval_card_action_responds_and_removes_pending_request(self) -> None:
        controller, pending_requests, _, _, responses, _ = self._make_controller()
        pending_requests["req-1"] = {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
            "title": "Codex 命令执行审批",
            "questions": [],
            "answers": {},
            "status": "pending",
        }

        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": "req-1",
                    "action": "command_allow_once",
                }
            )
        )

        self.assertEqual(responses, [("rpc-1", {"decision": "accept"}, None)])
        self.assertNotIn("req-1", pending_requests)
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已允许本次")

    def test_handle_user_input_action_updates_card_then_submits_final_answers(self) -> None:
        controller, pending_requests, _, _, responses, _ = self._make_controller()
        pending_requests["req-1"] = {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "questions": [
                {
                    "id": "q1",
                    "header": "第一题",
                    "question": "Q1",
                    "options": [{"label": "A", "description": ""}],
                    "isOther": False,
                },
                {
                    "id": "q2",
                    "header": "第二题",
                    "question": "Q2",
                    "options": [],
                    "isOther": True,
                },
            ],
            "answers": {},
            "status": "pending",
        }

        first = self._unpack_card_response(
            controller.handle_user_input_action(
                {
                    "request_id": "req-1",
                    "action": "answer_user_input_option",
                    "question_id": "q1",
                    "answer": "A",
                }
            )
        )
        self.assertEqual(first["toast"], "已记录，继续回答下一题。")
        self.assertIn("req-1", pending_requests)
        self.assertEqual(pending_requests["req-1"]["answers"], {"q1": "A"})

        second = self._unpack_card_response(
            controller.handle_user_input_action(
                {
                    "request_id": "req-1",
                    "action": "answer_user_input_custom",
                    "question_id": "q2",
                    "_form_value": {"user_input_q2": "custom"},
                }
            )
        )
        self.assertEqual(
            responses,
            [("rpc-1", {"answers": {"q1": {"answers": ["A"]}, "q2": {"answers": ["custom"]}}}, None)],
        )
        self.assertNotIn("req-1", pending_requests)
        self.assertEqual(second["toast"], "已提交回答。")

    def test_handle_server_request_resolved_patches_handled_elsewhere_card(self) -> None:
        controller, pending_requests, _, _, _, patches = self._make_controller()
        pending_requests["req-1"] = {
            "method": "item/tool/requestUserInput",
            "title": "Codex 用户输入",
            "message_id": "msg-card-1",
        }

        controller.handle_server_request_resolved({"requestId": "req-1"})

        self.assertNotIn("req-1", pending_requests)
        self.assertEqual(patches[0][0], "msg-card-1")
        self.assertIn("其他终端处理", patches[0][1]["elements"][0]["content"])

    def test_fail_close_chat_requests_auto_rejects_matching_chat_only(self) -> None:
        controller, pending_requests, _, _, responses, _ = self._make_controller()
        pending_requests["req-1"] = {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
            "chat_id": "chat-1",
        }
        pending_requests["req-2"] = {
            "rpc_request_id": "rpc-2",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread-2"},
            "chat_id": "chat-2",
        }

        closed = controller.fail_close_chat_requests("chat-1")

        self.assertEqual(closed, 1)
        self.assertNotIn("req-1", pending_requests)
        self.assertIn("req-2", pending_requests)
        self.assertEqual(responses, [("rpc-1", {"decision": "abort"}, None)])
