import pathlib
import tempfile
import unittest

from scripts import check_import_cycles


class ImportCycleGuardTests(unittest.TestCase):
    def _package(self, files: dict[str, str]) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path]:
        temporary = tempfile.TemporaryDirectory()
        package_root = pathlib.Path(temporary.name) / "sample"
        for relative, content in {"__init__.py": "", **files}.items():
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return temporary, package_root

    def test_function_local_lazy_import_participates_in_cycle_detection(self) -> None:
        temporary, package_root = self._package(
            {
                "first.py": "def load():\n    from sample import second\n",
                "second.py": "def load():\n    import sample.first\n",
            }
        )
        with temporary:
            report = check_import_cycles.check_package(
                package_root,
                package_name="sample",
            )

        self.assertEqual(
            report.cycles,
            (
                check_import_cycles.ImportCycle(
                    modules=("sample.first", "sample.second")
                ),
            ),
        )

    def test_relative_imports_across_package_modules_are_resolved(self) -> None:
        temporary, package_root = self._package(
            {
                "nested/__init__.py": "",
                "nested/first.py": "from . import second\n",
                "nested/second.py": "from .first import value\n",
            }
        )
        with temporary:
            report = check_import_cycles.check_package(
                package_root,
                package_name="sample",
            )

        self.assertEqual(
            report.cycles,
            (
                check_import_cycles.ImportCycle(
                    modules=("sample.nested.first", "sample.nested.second")
                ),
            ),
        )

    def test_acyclic_imports_have_no_cycle_component(self) -> None:
        temporary, package_root = self._package(
            {
                "first.py": "from sample import second\n",
                "second.py": "VALUE = 1\n",
            }
        )
        with temporary:
            report = check_import_cycles.check_package(
                package_root,
                package_name="sample",
            )

        self.assertEqual(report.cycles, ())
        self.assertEqual(report.module_count, 3)

    def test_focus_package_import_graph_is_acyclic(self) -> None:
        report = check_import_cycles.check()

        self.assertEqual(report.cycles, ())


if __name__ == "__main__":
    unittest.main()
