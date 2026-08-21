import unittest

from bot.managed_python import isolated_python_module_command


class ManagedPythonTests(unittest.TestCase):
    def test_isolated_module_command_has_one_fixed_shape(self) -> None:
        self.assertEqual(
            isolated_python_module_command(
                "/opt/focus/.venv/bin/python",
                "bot.__main__",
                "--instance",
                "corp-a",
            ),
            (
                "/opt/focus/.venv/bin/python",
                "-I",
                "-m",
                "bot.__main__",
                "--instance",
                "corp-a",
            ),
        )

    def test_isolated_module_command_rejects_non_module_text(self) -> None:
        for module in ("", "bot/module", "bot.module;exit"):
            with self.subTest(module=module):
                with self.assertRaisesRegex(ValueError, "无效 Python module"):
                    isolated_python_module_command("python", module)


if __name__ == "__main__":
    unittest.main()
