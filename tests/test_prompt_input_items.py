import unittest

from bot.prompt_input_items import replace_text_input_items


class PromptInputItemsTests(unittest.TestCase):
    def test_replace_text_preserves_non_text_items(self) -> None:
        items = [
            {"type": "text", "text": "old"},
            {"type": "input_image", "image_url": "file:///tmp/a.png"},
            {"type": "text", "text": "duplicate"},
        ]

        self.assertEqual(
            replace_text_input_items(items, "new"),
            [
                {"type": "text", "text": "new"},
                {"type": "input_image", "image_url": "file:///tmp/a.png"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
