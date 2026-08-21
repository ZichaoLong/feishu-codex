import unittest

from bot.cards import CommandResult
from bot.inbound_surface_controller import (
    ActionRoute,
    CommandRoute,
    InboundSurfaceController,
)


class InboundSurfaceControllerTests(unittest.TestCase):
    @staticmethod
    def _unpack_card_response(response) -> dict:
        if isinstance(response, dict):
            return response
        result: dict = {}
        if getattr(response, "card", None):
            result["card"] = response.card.data
        if getattr(response, "toast", None):
            result["toast"] = response.toast.content
            result["toast_type"] = response.toast.type
        return result

    def _make_controller(self):
        activated: list[tuple[str, str, str]] = []
        prompts: list[tuple[str, str, str, str]] = []
        replies: list[tuple[str, str, str]] = []
        cards: list[tuple[str, dict, str]] = []
        command_calls: list[tuple[str, str, str, str]] = []
        action_calls: list[tuple[str, str, str, dict]] = []
        prefixed_calls: list[tuple[str, str, str, dict]] = []
        help_fallback_calls: list[tuple[str, str, str, dict]] = []
        settings_fallback_calls: list[tuple[str, str, str, dict]] = []

        controller = InboundSurfaceController(
            keyword="CODEX",
            activate_binding_if_needed=lambda sender_id, chat_id, message_id: activated.append(
                (sender_id, chat_id, message_id)
            ),
            help_reply=lambda chat_id, message_id: CommandResult(text=f"help:{chat_id}:{message_id}"),
            handle_prompt=lambda sender_id, chat_id, text, message_id: prompts.append(
                (sender_id, chat_id, text, message_id)
            ),
            reply_text=lambda chat_id, text, **kwargs: replies.append(
                (chat_id, text, str(kwargs.get("message_id", "") or ""))
            ),
            reply_card=lambda chat_id, card, **kwargs: cards.append(
                (chat_id, card, str(kwargs.get("message_id", "") or ""))
            ),
            resolve_chat_type=lambda chat_id, message_id: "group" if chat_id.startswith("group") else "p2p",
            group_command_admin_denial_text=lambda chat_id, message_id, sender_id: (
                "admin only" if chat_id.startswith("group") and sender_id != "ou_admin" else ""
            ),
            is_group_chat=lambda chat_id, message_id: chat_id.startswith("group"),
            is_group_admin_actor=lambda chat_id, **kwargs: str(kwargs.get("operator_open_id", "") or "").strip()
            == "ou_admin",
            is_group_turn_actor=lambda chat_id, **kwargs: str(kwargs.get("operator_open_id", "") or "").strip()
            in {"ou_admin", "ou_actor"},
            is_group_request_actor_or_admin=lambda chat_id, **kwargs: str(
                kwargs.get("operator_open_id", "") or ""
            ).strip()
            in {"ou_admin", "ou_actor"},
            handle_rename_form_fallback=lambda *args: None,
            handle_help_form_fallback=lambda sender_id, chat_id, message_id, action_value: (
                help_fallback_calls.append((sender_id, chat_id, message_id, action_value)),
                None,
            )[1],
            handle_settings_form_fallback=lambda sender_id, chat_id, message_id, action_value: (
                settings_fallback_calls.append((sender_id, chat_id, message_id, action_value)),
                None,
            )[1],
            handle_user_input_form_fallback=lambda *args: None,
        )
        controller.install_routes(
            command_routes={
                "/status": CommandRoute(
                    handler=lambda sender_id, chat_id, arg, message_id: (
                        command_calls.append((sender_id, chat_id, arg, message_id)),
                        CommandResult(text="status ok"),
                    )[1]
                ),
                "/init": CommandRoute(
                    handler=lambda sender_id, chat_id, arg, message_id: CommandResult(text=f"init {arg}"),
                    scope="p2p",
                    scope_denied_text="private only",
                ),
                "/last": CommandRoute(
                    handler=lambda sender_id, chat_id, arg, message_id: CommandResult(text="latest text")
                    if arg == "text"
                    else CommandResult(text="用法：`/last text`")
                ),
            },
            action_routes={
                "group_only": ActionRoute(
                    handler=lambda sender_id, chat_id, message_id, action_value: (
                        action_calls.append((sender_id, chat_id, message_id, action_value)),
                        {},
                    )[1],
                    group_guard="group_admin",
                ),
                "card_echo": ActionRoute(
                    handler=lambda sender_id, chat_id, message_id, action_value: {
                        "card": {
                            "config": {"wide_screen_mode": True, "update_multi": True},
                            "header": {
                                "title": {"tag": "plain_text", "content": "Echo"},
                                "template": "blue",
                            },
                            "elements": [
                                {"tag": "markdown", "content": "echo"},
                                {
                                    "tag": "action",
                                    "actions": [
                                        {
                                            "tag": "button",
                                            "text": {"tag": "plain_text", "content": "Next"},
                                            "type": "default",
                                            "value": {"action": "noop"},
                                        }
                                    ],
                                },
                            ],
                        }
                    }
                ),
            },
            prefixed_action_routes=[
                (
                    "answer_user_input_",
                    ActionRoute(
                        handler=lambda sender_id, chat_id, message_id, action_value: (
                            prefixed_calls.append((sender_id, chat_id, message_id, action_value)),
                            {},
                        )[1],
                        group_guard="approval_admin",
                    ),
                )
            ],
        )
        return (
            controller,
            activated,
            prompts,
            replies,
            cards,
            command_calls,
            action_calls,
            prefixed_calls,
            help_fallback_calls,
            settings_fallback_calls,
        )

    def test_blank_message_dispatches_help_and_activates_binding(self) -> None:
        controller, activated, prompts, replies, *_ = self._make_controller()

        controller.handle_message("ou_user", "c1", "   ", message_id="m1")

        self.assertEqual(activated, [("ou_user", "c1", "m1")])
        self.assertEqual(prompts, [])
        self.assertEqual(replies, [("c1", "help:c1:m1", "m1")])

    def test_plain_message_routes_to_prompt_handler(self) -> None:
        controller, activated, prompts, replies, *_ = self._make_controller()

        controller.handle_message("ou_user", "c1", "hello", message_id="m2")

        self.assertEqual(activated, [("ou_user", "c1", "m2")])
        self.assertEqual(prompts, [("ou_user", "c1", "hello", "m2")])
        self.assertEqual(replies, [])

    def test_help_submit_command_preserves_scope_denial(self) -> None:
        controller, *_ = self._make_controller()

        response = self._unpack_card_response(
            controller.handle_help_submit_command_action(
                "ou_user",
                "group-1",
                "msg-help",
                {
                    "command": "/init",
                    "field_name": "init_token",
                    "_form_value": {"init_token": "demo"},
                },
            )
        )

        self.assertEqual(response["toast"], "private only")
        self.assertEqual(response["toast_type"], "warning")

    def test_help_execute_last_text_action_replies_with_plain_text_instead_of_card(self) -> None:
        controller, _, _, replies, cards, *_ = self._make_controller()

        response = self._unpack_card_response(
            controller.handle_help_execute_command_action(
                "ou_user",
                "c1",
                "msg-last",
                {
                    "action": "help_execute_command",
                    "command": "/last text",
                    "title": "Codex 最近结果文本",
                },
            )
        )

        self.assertEqual(replies, [("c1", "latest text", "msg-last")])
        self.assertEqual(cards, [])
        self.assertEqual(response["toast"], "已发送最近文本。")
        self.assertEqual(response["toast_type"], "success")

    def test_help_execute_text_command_wraps_result_card_with_return_help(self) -> None:
        controller, *_ = self._make_controller()

        response = self._unpack_card_response(
            controller.handle_help_execute_command_action(
                "ou_user",
                "c1",
                "msg-help",
                {
                    "action": "help_execute_command",
                    "command": "/status",
                    "title": "Codex 当前状态",
                },
            )
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前状态")
        action_rows = [
            element for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "action"
        ]
        self.assertEqual(action_rows[-1]["actions"][0]["text"]["content"], "返回帮助")
        self.assertEqual(
            action_rows[-1]["actions"][0]["value"],
            {"action": "show_help_page", "page": "overview", "help_origin": "overview"},
        )

    def test_help_origin_card_action_preserves_return_help_and_propagates_metadata(self) -> None:
        controller, *_ = self._make_controller()

        response = self._unpack_card_response(
            controller.handle_card_action(
                "ou_user",
                "c1",
                "msg-help",
                {
                    "action": "card_echo",
                    "help_origin": "overview",
                },
            )
        )

        action_rows = [
            element for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "action"
        ]
        self.assertEqual(action_rows[0]["actions"][0]["value"], {"action": "noop", "help_origin": "overview"})
        self.assertEqual(action_rows[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_group_action_guard_denies_non_admin_actor(self) -> None:
        controller, _, _, _, _, _, action_calls, _, _, _ = self._make_controller()

        response = self._unpack_card_response(
            controller.handle_card_action(
                "ou_user",
                "group-1",
                "msg-1",
                {"action": "group_only", "_operator_open_id": "ou_guest"},
            )
        )

        self.assertEqual(response["toast"], "仅管理员可操作群共享会话或群设置。")
        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(action_calls, [])

    def test_prefixed_action_routes_dispatch_to_matching_handler(self) -> None:
        controller, _, _, _, _, _, _, prefixed_calls, _, _ = self._make_controller()

        response = controller.handle_card_action(
            "ou_user",
            "group-1",
            "msg-2",
            {"action": "answer_user_input_q1", "_operator_open_id": "ou_admin"},
        )

        self.assertEqual(self._unpack_card_response(response), {})
        self.assertEqual(
            prefixed_calls,
            [
                (
                    "ou_user",
                    "group-1",
                    "msg-2",
                    {
                        "action": "answer_user_input_q1",
                        "_operator_open_id": "ou_admin",
                    },
                )
            ],
        )

    def test_form_value_only_callback_checks_help_fallback_before_stale_warning(self) -> None:
        controller, *_rest, help_fallback_calls, settings_fallback_calls = self._make_controller()

        response = self._unpack_card_response(
            controller.handle_card_action(
                "ou_user",
                "c1",
                "msg-help",
                {"_form_value": {"cd_path": "/tmp"}},
            )
        )

        self.assertEqual(
            help_fallback_calls,
            [("ou_user", "c1", "msg-help", {"_form_value": {"cd_path": "/tmp"}})],
        )
        self.assertEqual(
            settings_fallback_calls,
            [("ou_user", "c1", "msg-help", {"_form_value": {"cd_path": "/tmp"}})],
        )
        self.assertEqual(response["toast"], "表单已失效或未找到对应问题，请重新触发该请求。")
        self.assertEqual(response["toast_type"], "warning")

    def test_form_value_only_callback_checks_settings_fallback_before_stale_warning(self) -> None:
        controller, activated, prompts, replies, cards, command_calls, action_calls, prefixed_calls, help_fallback_calls, _ = self._make_controller()
        settings_fallback_calls: list[tuple[str, str, str, dict]] = []
        controller = InboundSurfaceController(
            keyword="CODEX",
            activate_binding_if_needed=lambda sender_id, chat_id, message_id: activated.append(
                (sender_id, chat_id, message_id)
            ),
            help_reply=lambda chat_id, message_id: CommandResult(text=f"help:{chat_id}:{message_id}"),
            handle_prompt=lambda sender_id, chat_id, text, message_id: prompts.append(
                (sender_id, chat_id, text, message_id)
            ),
            reply_text=lambda chat_id, text, **kwargs: replies.append(
                (chat_id, text, str(kwargs.get("message_id", "") or ""))
            ),
            reply_card=lambda chat_id, card, **kwargs: cards.append(
                (chat_id, card, str(kwargs.get("message_id", "") or ""))
            ),
            resolve_chat_type=lambda chat_id, message_id: "group" if chat_id.startswith("group") else "p2p",
            group_command_admin_denial_text=lambda chat_id, message_id, sender_id: (
                "admin only" if chat_id.startswith("group") and sender_id != "ou_admin" else ""
            ),
            is_group_chat=lambda chat_id, message_id: chat_id.startswith("group"),
            is_group_admin_actor=lambda chat_id, **kwargs: str(kwargs.get("operator_open_id", "") or "").strip()
            == "ou_admin",
            is_group_turn_actor=lambda chat_id, **kwargs: str(kwargs.get("operator_open_id", "") or "").strip()
            in {"ou_admin", "ou_actor"},
            is_group_request_actor_or_admin=lambda chat_id, **kwargs: str(
                kwargs.get("operator_open_id", "") or ""
            ).strip()
            in {"ou_admin", "ou_actor"},
            handle_rename_form_fallback=lambda *args: None,
            handle_help_form_fallback=lambda sender_id, chat_id, message_id, action_value: (
                help_fallback_calls.append((sender_id, chat_id, message_id, action_value)),
                None,
            )[1],
            handle_settings_form_fallback=lambda sender_id, chat_id, message_id, action_value: (
                settings_fallback_calls.append((sender_id, chat_id, message_id, action_value)),
                {},
            )[1],
            handle_user_input_form_fallback=lambda *args: None,
        )

        response = self._unpack_card_response(
            controller.handle_card_action(
                "ou_user",
                "c1",
                "msg-model",
                {"_form_value": {"model_override": "glm-4.5"}},
            )
        )

        self.assertEqual(
            settings_fallback_calls,
            [("ou_user", "c1", "msg-model", {"_form_value": {"model_override": "glm-4.5"}})],
        )
        self.assertEqual(response, {})


if __name__ == "__main__":
    unittest.main()
