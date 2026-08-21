import unittest

from bot.adapters.base import RuntimeModelSummary
from bot.input_media_contract import model_supports_input


class InputMediaContractTests(unittest.TestCase):
    def test_model_capability_distinguishes_unknown_text_only_and_image(self) -> None:
        models = [
            RuntimeModelSummary(model="unknown", input_modalities=None),
            RuntimeModelSummary(model="text", input_modalities=["text"]),
            RuntimeModelSummary(model="vision", input_modalities=["text", "image"]),
        ]

        self.assertIsNone(model_supports_input(models, "missing", "image"))
        self.assertIsNone(model_supports_input(models, "unknown", "image"))
        self.assertFalse(model_supports_input(models, "text", "image"))
        self.assertTrue(model_supports_input(models, "vision", "image"))


if __name__ == "__main__":
    unittest.main()
