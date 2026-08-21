from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from scripts import check_import_cycles, focus_capabilities, focus_nav


class FocusNavigationTests(unittest.TestCase):
    def _package(self, files: dict[str, str]) -> tuple[pathlib.Path, str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        package_name = "sample"
        package_root = pathlib.Path(temporary.name) / package_name
        for relative, content in {"__init__.py": "", **files}.items():
            path = package_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return package_root, package_name

    def _path_catalog(
        self,
    ) -> tuple[pathlib.Path, focus_capabilities.CapabilityCatalog]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = pathlib.Path(temporary.name)
        files = {
            "bot/shared.py": ("class Entry:\n    pass\n\nclass Owner:\n    pass\n"),
            "docs/contracts/sample.md": ("# Sample\n\n## Boundary\ntext\n\n## Next\n"),
            "tests/test_sample.py": (
                "class SampleTests:\n    def test_case(self):\n        assert True\n"
            ),
            "tests/group/test_nested.py": "def test_nested():\n    assert True\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        source_entry = focus_capabilities.SourceReference("bot/shared.py", "Entry")
        source_owner = focus_capabilities.SourceReference("bot/shared.py", "Owner")
        contract = focus_capabilities.ContractReference(
            "docs/contracts/sample.md", "Boundary"
        )
        file_test = focus_capabilities.VerificationReference(
            "pytest", ("tests/test_sample.py",)
        )
        node_test = focus_capabilities.VerificationReference(
            "pytest", ("tests/test_sample.py::SampleTests::test_case",)
        )
        directory_test = focus_capabilities.VerificationReference(
            "pytest", ("tests/group",)
        )
        catalog = focus_capabilities.CapabilityCatalog(
            capabilities=(
                focus_capabilities.Capability(
                    name="beta",
                    entries=(source_entry,),
                    owners=(source_owner,),
                    contracts=(contract,),
                    focused_tests=(directory_test,),
                    sentinels=(node_test,),
                    guards=("source-context",),
                ),
                focus_capabilities.Capability(
                    name="alpha",
                    entries=(source_entry,),
                    owners=(source_owner,),
                    contracts=(contract,),
                    focused_tests=(file_test,),
                    sentinels=(node_test,),
                    guards=("import-cycles",),
                ),
            )
        )
        return root, catalog

    def test_module_neighborhood_is_live_one_hop_graph_with_inversion(self) -> None:
        package_root, package_name = self._package(
            {
                "a.py": (
                    "from typing import TYPE_CHECKING\n"
                    "from sample import b\n"
                    "if TYPE_CHECKING:\n    import sample.c\n"
                    "def lazy():\n    import sample.d\n"
                ),
                "b.py": "from sample import e\n",
                "c.py": "",
                "d.py": "",
                "e.py": "",
            }
        )

        neighborhood = focus_nav.module_neighborhood(
            "sample.a", package_root=package_root, package_name=package_name
        )
        self.assertEqual(
            neighborhood.imports,
            ("sample", "sample.b", "sample.c", "sample.d"),
        )
        self.assertNotIn("sample.e", neighborhood.imports)

        imported = focus_nav.module_neighborhood(
            "sample.b", package_root=package_root, package_name=package_name
        )
        self.assertEqual(imported.importers, ("sample.a",))

    def test_relative_import_and_package_initializer_are_resolved(self) -> None:
        package_root, package_name = self._package(
            {
                "nested/__init__.py": "from . import leaf\n",
                "nested/leaf.py": "VALUE = 1\n",
            }
        )

        neighborhood = focus_nav.module_neighborhood(
            "sample/nested/__init__.py",
            package_root=package_root,
            package_name=package_name,
        )
        self.assertEqual(neighborhood.module, "sample.nested")
        self.assertIn("sample.nested.leaf", neighborhood.imports)

    def test_module_query_rejects_unknown_and_noncanonical_paths(self) -> None:
        graph = {"bot": frozenset(), "bot.valid": frozenset({"bot"})}
        invalid = (
            "",
            "bot/../bot/valid.py",
            "bot\\valid.py",
            "/bot/valid.py",
            "other/valid.py",
            "bot.missing",
        )
        for query in invalid:
            with self.subTest(query=query):
                with self.assertRaises(focus_nav.NavigationError):
                    focus_nav.module_name_from_query(query, graph=graph)

    def test_live_graph_parse_failure_is_not_hidden(self) -> None:
        package_root, package_name = self._package({"broken.py": "def nope(:\n"})

        with self.assertRaises(check_import_cycles.ImportGraphError):
            focus_nav.module_neighborhood(
                "sample.broken",
                package_root=package_root,
                package_name=package_name,
            )

    def test_all_reviewed_python_entry_and_owner_paths_map_to_live_modules(
        self,
    ) -> None:
        catalog = focus_capabilities.load_catalog()
        graph = check_import_cycles.build_import_graph(
            focus_nav.REPO_ROOT / "bot", package_name="bot"
        )

        for capability in catalog.capabilities:
            for reference in (*capability.entries, *capability.owners):
                if not reference.path.startswith("bot/"):
                    continue
                with self.subTest(capability=capability.name, path=reference.path):
                    module = focus_nav.module_name_from_query(
                        reference.path, graph=graph
                    )
                    self.assertIn(module, graph)

    def test_explicit_paths_reverse_map_roles_without_guessing(self) -> None:
        root, catalog = self._path_catalog()
        impacts = focus_nav.path_impacts(
            (
                "unmapped/deleted.py",
                "tests/test_sample.py",
                "bot/shared.py",
                "tests/group/test_nested.py",
            ),
            catalog=catalog,
            repo_root=root,
        )
        payload = focus_nav._paths_payload(impacts)

        self.assertEqual(
            [item["path"] for item in payload["paths"]],
            [
                "bot/shared.py",
                "tests/group/test_nested.py",
                "tests/test_sample.py",
                "unmapped/deleted.py",
            ],
        )
        source_matches = payload["paths"][0]["matches"]
        self.assertEqual(
            [(item["capability"], item["role"]) for item in source_matches],
            [
                ("alpha", "entry"),
                ("alpha", "owner"),
                ("beta", "entry"),
                ("beta", "owner"),
            ],
        )
        self.assertEqual(
            payload["paths"][1],
            {
                "path": "tests/group/test_nested.py",
                "matches": [],
                "unmapped": True,
            },
        )
        self.assertEqual(
            [item["role"] for item in payload["paths"][2]["matches"]],
            ["focused-test", "sentinel", "sentinel"],
        )
        self.assertTrue(payload["paths"][3]["unmapped"])
        self.assertEqual(payload["paths"][3]["matches"], [])
        self.assertNotIn("guard", json.dumps(payload, sort_keys=True))

    def test_path_locations_are_live_and_selector_specific(self) -> None:
        root, catalog = self._path_catalog()
        impacts = focus_nav.path_impacts(
            (
                "bot/shared.py",
                "docs/contracts/sample.md",
                "tests/test_sample.py",
            ),
            catalog=catalog,
            repo_root=root,
            include_locations=True,
        )
        payload = focus_nav._paths_payload(impacts)

        source_locations = {
            item["ref"]["symbol"]: item["location"]
            for item in payload["paths"][0]["matches"]
        }
        self.assertEqual(
            source_locations,
            {
                "Entry": {
                    "path": "bot/shared.py",
                    "start_line": 1,
                    "end_line": 2,
                },
                "Owner": {
                    "path": "bot/shared.py",
                    "start_line": 4,
                    "end_line": 5,
                },
            },
        )
        self.assertEqual(
            payload["paths"][1]["matches"][0]["location"],
            {
                "path": "docs/contracts/sample.md",
                "start_line": 3,
                "end_line": 5,
            },
        )
        test_locations = {
            item["ref"]["target"]: item["location"]
            for item in payload["paths"][2]["matches"]
        }
        self.assertEqual(
            test_locations["tests/test_sample.py::SampleTests::test_case"],
            {
                "path": "tests/test_sample.py",
                "start_line": 2,
                "end_line": 3,
            },
        )

        (root / "bot/shared.py").write_text(
            "class Renamed:\n    pass\n", encoding="utf-8"
        )
        with self.assertRaises(focus_capabilities.CapabilityMapError):
            focus_nav.path_impacts(
                ("bot/shared.py",),
                catalog=catalog,
                repo_root=root,
                include_locations=True,
            )

    def test_changed_paths_reject_ambiguous_input(self) -> None:
        _, catalog = self._path_catalog()
        invalid = (
            "",
            " bot/shared.py",
            "/bot/shared.py",
            "bot/../bot/shared.py",
            "bot\\shared.py",
            "bot/*.py",
            "tests/test_sample.py::SampleTests::test_case",
        )
        for path in invalid:
            with self.subTest(path=path):
                with self.assertRaises(focus_nav.NavigationError):
                    focus_nav.path_impacts((path,), catalog=catalog)
        with self.assertRaisesRegex(focus_nav.NavigationError, "duplicates"):
            focus_nav.path_impacts(("bot/shared.py", "bot/shared.py"), catalog=catalog)

    def test_duplicate_flattened_refs_fail_closed(self) -> None:
        root, catalog = self._path_catalog()
        alpha = catalog.require("alpha")
        duplicate = focus_capabilities.Capability(
            name=alpha.name,
            entries=alpha.entries,
            owners=alpha.owners,
            contracts=alpha.contracts,
            focused_tests=(
                *alpha.focused_tests,
                focus_capabilities.VerificationReference(
                    "pytest",
                    (
                        "tests/test_sample.py",
                        "tests/group/test_nested.py",
                    ),
                ),
            ),
            sentinels=alpha.sentinels,
            guards=alpha.guards,
        )

        with self.assertRaisesRegex(focus_nav.NavigationError, "duplicate reviewed"):
            focus_nav.path_impacts(
                ("tests/test_sample.py",),
                catalog=focus_capabilities.CapabilityCatalog((duplicate,)),
                repo_root=root,
            )

    def test_directory_target_location_fails_closed(self) -> None:
        root, catalog = self._path_catalog()
        impact = focus_nav.path_impacts(
            ("tests/group",), catalog=catalog, repo_root=root
        )[0]
        self.assertEqual(len(impact.matches), 1)
        self.assertEqual(
            focus_nav._reference_payload(impact.matches[0].reference),
            {"runner": "pytest", "target": "tests/group"},
        )

        with self.assertRaisesRegex(
            focus_capabilities.CapabilityMapError, "directory-level"
        ):
            focus_nav.path_impacts(
                ("tests/group",),
                catalog=catalog,
                repo_root=root,
                include_locations=True,
            )

    def test_paths_cli_emits_stable_json_and_preserves_show(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = focus_nav.main(
                [
                    "paths",
                    "web/src/focus/mutations/actions.ts",
                    "unmapped/deleted.py",
                    "--locations",
                    "--json",
                ]
            )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["paths"][0]["path"], "unmapped/deleted.py")
        self.assertTrue(payload["paths"][0]["unmapped"])
        self.assertEqual(
            [item["role"] for item in payload["paths"][1]["matches"]],
            ["entry", "owner"],
        )
        self.assertGreater(
            payload["paths"][1]["matches"][0]["location"]["start_line"], 0
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = focus_nav.main(["show", "focus-runtime", "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["name"], "focus-runtime")

    def test_paths_cli_reports_invalid_input_without_traceback(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = focus_nav.main(["paths", "bot/../bot/runtime.py"])

        self.assertEqual(result, 2)
        self.assertIn("Focus navigation failed:", errors.getvalue())

    def test_json_payloads_are_stable_navigation_shapes(self) -> None:
        capability = focus_capabilities.load_catalog().require("focus-runtime")
        payload = focus_capabilities.capability_payload(capability)
        encoded = json.dumps(payload, sort_keys=True)

        self.assertEqual(json.loads(encoded)["name"], "focus-runtime")
        self.assertEqual(
            focus_nav._module_payload(
                focus_nav.ModuleNeighborhood(
                    module="bot.sample",
                    imports=("bot.owner",),
                    importers=("bot.entry",),
                )
            ),
            {
                "module": "bot.sample",
                "imports": ["bot.owner"],
                "importers": ["bot.entry"],
            },
        )


if __name__ == "__main__":
    unittest.main()
