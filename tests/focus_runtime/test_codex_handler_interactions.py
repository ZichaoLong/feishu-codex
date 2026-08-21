
from tests.focus_runtime.codex_handler_fakes import _bind_pending_interaction_action as _bind
from bot.cards import build_ask_user_card
from bot.jsonrpc_id import jsonrpc_id_key

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerInteractionTests(CodexHandlerHarness):
    def test_approval_card_action_is_idempotent_while_processing(self) -> None:
        handler, _ = self._make_handler()
        responded = []
        nested = {}

        def fake_respond(request_id, *, result=None, error=None, connection_generation):
            responded.append((request_id, result, error))
            if len(responded) == 1:
                nested["response"] = self._unpack_card_response(handler._feishu_surface.handle_approval_card_action(
                    _bind(handler, request_key, {
                        "request_id": request_key,
                        "action": "interaction_approval",
                        "response_action": "approve_once",
                    })
                ))

        handler._adapter.respond = fake_respond
        request_key = self._store_canonical_pending_request(handler, {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
            "title": "Codex 命令执行审批",
            "questions": [],
            "answers": {},
        })

        response = self._unpack_card_response(handler._runtime_call(
            handler._feishu_surface.handle_approval_card_action,
            _bind(handler, request_key, {
                "request_id": request_key,
                "action": "interaction_approval",
                "response_action": "approve_once",
            }),
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

        response = self._unpack_card_response(handler._feishu_surface.handle_user_input_action(
            _bind(handler, jsonrpc_id_key("req-1"), {
                "request_id": jsonrpc_id_key("req-1"),
                "action": "answer_user_input_custom",
                "question_id": "q1",
                "_form_value": {"user_input_q1": "自定义"},
            })
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "该问题仅支持选择预设选项")

    def test_form_value_only_callback_submits_custom_user_input(self) -> None:
        handler, _ = self._make_handler()
        responded = {}

        def fake_respond(request_id, *, result=None, error=None, connection_generation):
            responded["request_id"] = request_id
            responded["result"] = result
            responded["error"] = error

        handler._adapter.respond = fake_respond
        request_key = self._store_canonical_pending_request(handler, {
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
            _bind(handler, request_key, {"_form_value": {"user_input_q1": "创建 c.txt"}}),
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
        # An ordinary member can only have originated this pending group turn
        # after an administrator explicitly activated the group.
        bot.activate_group_chat("chat-group", activated_by="ou_admin")

        def fake_respond(request_id, *, result=None, error=None, connection_generation):
            responded["request_id"] = request_id
            responded["result"] = result
            responded["error"] = error

        handler._adapter.respond = fake_respond
        request_key = self._store_canonical_pending_request(handler, {
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
            _bind(handler, request_key, {
                "action": "answer_user_input_option",
                "request_id": request_key,
                "question_id": "q1",
                "answer": "确认步骤",
            }),
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

        def fake_respond(request_id, *, result=None, error=None, connection_generation):
            responded.append((request_id, result, error))
            if len(responded) == 1:
                nested["response"] = self._unpack_card_response(handler._feishu_surface.handle_user_input_action(
                    _bind(handler, request_key, {
                        "request_id": request_key,
                        "action": "answer_user_input_option",
                        "question_id": "q1",
                        "answer": "确认步骤",
                    })
                ))

        handler._adapter.respond = fake_respond
        request_key = self._store_canonical_pending_request(handler, {
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
        })

        response = self._unpack_card_response(handler._runtime_call(
            handler._feishu_surface.handle_user_input_action,
            _bind(handler, request_key, {
                "request_id": request_key,
                "action": "answer_user_input_option",
                "question_id": "q1",
                "answer": "确认步骤",
            }),
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
