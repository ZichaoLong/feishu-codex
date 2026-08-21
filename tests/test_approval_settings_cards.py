from __future__ import annotations

import hashlib
import json
import unittest

from bot.approval_settings_cards import (
    BINDING_SAFETY_BASELINE_SCOPE_TEXT,
    build_approval_policy_card,
    build_permissions_profile_card,
)


class ApprovalSettingsCardTests(unittest.TestCase):
    def test_representative_cards_keep_the_activation_structure(self) -> None:
        cards = [
            build_approval_policy_card("on-request", running=True),
            build_permissions_profile_card(":workspace", running=True),
        ]
        payload = json.dumps(
            cards,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

        self.assertEqual(len(payload), 3_139)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "63c15d1e488cfecb7a401c1d8608d4aa048aed4f4afda0a41a8ab650c0c4ae8b",
        )
        for card in cards:
            serialized = json.dumps(card, ensure_ascii=False)
            self.assertIn(BINDING_SAFETY_BASELINE_SCOPE_TEXT, serialized)
            self.assertIn("下一轮生效", serialized)
            self.assertNotIn('"plugin"', serialized)

    def test_permissions_card_keeps_the_help_return_action(self) -> None:
        card = build_permissions_profile_card(":workspace")

        self.assertEqual(
            card["elements"][-1],
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "返回帮助"},
                        "type": "default",
                        "value": {
                            "action": "show_help_page",
                            "page": "overview",
                        },
                    }
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
