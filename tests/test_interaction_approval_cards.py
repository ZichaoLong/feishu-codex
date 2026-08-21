from __future__ import annotations

import hashlib
import inspect
import json
import unittest

from bot.interaction_approval_cards import (
    build_approval_handled_card,
    build_command_approval_card,
    build_file_change_approval_card,
    build_permissions_approval_card,
)


def _approval_actions() -> list[dict]:
    return [
        {"id": "approve_once", "label": "允许本次", "style": "primary"},
        {"id": "approve_session", "label": "允许本会话"},
        {"id": "reject", "label": "拒绝", "style": "danger"},
    ]


def _representative_cards() -> list[dict]:
    actions = _approval_actions()
    return [
        build_command_approval_card(
            "req-command",
            command="python -m pytest -q",
            cwd="/tmp/project",
            reason="需要执行验证",
            actions=actions,
            context_lines=(
                "**来源**: shared approval",
                "**范围**: current turn",
            ),
        ),
        build_file_change_approval_card(
            "req-file",
            grant_root="/tmp/project",
            reason="需要修改文件",
            actions=actions,
        ),
        build_permissions_approval_card(
            "req-permissions",
            permissions={
                "fileSystem": {
                    "read": ["/tmp/a"],
                    "write": ["/tmp/b"],
                },
                "network": {"enabled": True},
            },
            reason="需要额外权限",
            actions=actions,
        ),
        build_approval_handled_card("审批完成", "批准", "已同步"),
    ]


class InteractionApprovalCardTests(unittest.TestCase):
    def test_representative_cards_keep_the_activation_structure(self) -> None:
        payload = json.dumps(
            _representative_cards(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        self.assertEqual(len(payload), 2_982)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "df5dadabc26c77e30e930a125cb5fa99c5ef820f261733e773eeed2571f6d990",
        )

    def test_request_builders_require_canonical_actions(self) -> None:
        builders = (
            build_command_approval_card,
            build_file_change_approval_card,
            build_permissions_approval_card,
        )

        for builder in builders:
            with self.subTest(builder=builder.__name__):
                actions = inspect.signature(builder).parameters["actions"]
                self.assertIs(actions.default, inspect.Parameter.empty)

        for card in _representative_cards()[:3]:
            values = [button["value"] for button in card["elements"][2]["actions"]]
            self.assertEqual(
                values,
                [
                    {
                        "action": "interaction_approval",
                        "request_id": values[0]["request_id"],
                        "response_action": action_id,
                    }
                    for action_id in ("approve_once", "approve_session", "reject")
                ],
            )


if __name__ == "__main__":
    unittest.main()
