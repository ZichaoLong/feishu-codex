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
            group_command_admin_denial_text=lambda chat_id, message_id: (
                "admin only" if chat_id.startswith("group") else ""
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
            },
            action_routes={
                "group_only": ActionRoute(
                    handler=lambda sender_id, chat_id, message_id, action_value: (
                        action_calls.append((sender_id, chat_id, message_id, action_value)),
                        {},
                    )[1],
                    group_guard="group_admin",
                ),
            },
            prefixed_action_routes=[
                (
                    "command_",
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
        return controller, activated, prompts, replies, cards, command_calls, action_calls, prefixed_calls

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

    def test_group_action_guard_denies_non_admin_actor(self) -> None:
        controller, *_rest, action_calls, _prefixed_calls = self._make_controller()

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
        controller, *_rest, prefixed_calls = self._make_controller()

        response = controller.handle_card_action(
            "ou_user",
            "group-1",
            "msg-2",
            {"action": "command_allow_once", "_operator_open_id": "ou_admin"},
        )

        self.assertEqual(self._unpack_card_response(response), {})
        self.assertEqual(
            prefixed_calls,
            [("ou_user", "group-1", "msg-2", {"action": "command_allow_once", "_operator_open_id": "ou_admin"})],
        )


if __name__ == "__main__":
    unittest.main()
