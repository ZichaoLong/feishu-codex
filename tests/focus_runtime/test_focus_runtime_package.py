from __future__ import annotations

import pathlib
import unittest

import bot.focus_runtime as focus_runtime_package
import bot.focus_runtime.runtime as focus_runtime_module


_RUNTIME_OWNER_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_PACKAGE_ROOT = _RUNTIME_OWNER_PATH.parent
_LEGACY_RUNTIME_PATH = _PACKAGE_ROOT.with_suffix(".py")


class FocusRuntimePackageTests(unittest.TestCase):
    def test_runtime_has_one_real_module_path_without_package_reexport(self) -> None:
        self.assertFalse(_LEGACY_RUNTIME_PATH.exists())
        self.assertTrue(_RUNTIME_OWNER_PATH.is_file())
        self.assertEqual((_PACKAGE_ROOT / "__init__.py").read_bytes(), b"")
        self.assertFalse(hasattr(focus_runtime_package, "FocusRuntime"))

    def test_runtime_preserves_its_operational_logger_category(self) -> None:
        self.assertEqual(focus_runtime_module.logger.name, "bot.focus_runtime")


if __name__ == "__main__":
    unittest.main()
