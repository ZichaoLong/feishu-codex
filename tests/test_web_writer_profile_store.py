import json
import pathlib
import tempfile
import unittest
from unittest import mock

from bot.stores.web_writer_profile_store import (
    WebWriterProfileStore,
    WebWriterSelectionClearReceipt,
)


class WebWriterProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = pathlib.Path(self.temp_dir.name)

    def test_update_persists_browser_writer_profile(self) -> None:
        store = WebWriterProfileStore(self.data_dir)

        updated = store.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir="/work/project",
        )
        reloaded = WebWriterProfileStore(self.data_dir).load("tab-1")

        self.assertEqual(reloaded, updated)
        self.assertGreater(updated.updated_at, 0)

    def test_clear_removes_only_target_profile(self) -> None:
        store = WebWriterProfileStore(self.data_dir)
        store.update("tab-1", working_dir="/work/one")
        store.update("tab-2", working_dir="/work/two")

        store.clear("tab-1")

        self.assertIsNone(store.load("tab-1"))
        self.assertEqual(store.load("tab-2").working_dir, "/work/two")

    def test_clear_selected_thread_commits_exact_scope_transition_receipts(self) -> None:
        store = WebWriterProfileStore(self.data_dir)
        store.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir="/work/one",
            scope_generation=4,
        )
        store.update(
            "tab-2",
            selected_thread_id="thread-2",
            working_dir="/work/two",
            scope_generation=7,
        )
        store.update(
            "tab-3",
            selected_thread_id="thread-1",
            working_dir="/work/three",
            scope_generation=9,
        )

        cleared = store.clear_selected_thread("thread-1")

        self.assertEqual(tuple(type(receipt) for receipt in cleared), (
            WebWriterSelectionClearReceipt,
            WebWriterSelectionClearReceipt,
        ))
        self.assertEqual(
            tuple(receipt.current.client_id for receipt in cleared),
            ("tab-1", "tab-3"),
        )
        for receipt, previous_generation in zip(cleared, (4, 9), strict=True):
            self.assertEqual(receipt.cleared_thread_id, "thread-1")
            self.assertEqual(receipt.previous.selected_thread_id, "thread-1")
            self.assertEqual(receipt.previous.scope_generation, previous_generation)
            self.assertEqual(receipt.current.selected_thread_id, "")
            self.assertEqual(
                receipt.current.scope_generation,
                previous_generation + 1,
            )

        reloaded = WebWriterProfileStore(self.data_dir)
        cleared_tab = reloaded.load("tab-1")
        self.assertEqual(cleared_tab.selected_thread_id, "")
        self.assertEqual(cleared_tab.scope_generation, 5)
        self.assertEqual(cleared_tab.working_dir, "/work/one")
        untouched = reloaded.load("tab-2")
        self.assertEqual(untouched.selected_thread_id, "thread-2")
        self.assertEqual(untouched.scope_generation, 7)

    def test_clear_selected_thread_mismatch_and_replay_do_not_advance(self) -> None:
        store = WebWriterProfileStore(self.data_dir)
        initial = store.update(
            "tab-1",
            selected_thread_id="thread-1",
            scope_generation=4,
        )

        mismatch = store.clear_selected_thread("thread-2")
        after_mismatch = store.load("tab-1")
        first = store.clear_selected_thread("thread-1")
        replay = store.clear_selected_thread("thread-1")
        after_replay = WebWriterProfileStore(self.data_dir).load("tab-1")

        self.assertEqual(mismatch, ())
        self.assertEqual(after_mismatch, initial)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].previous.scope_generation, 4)
        self.assertEqual(first[0].current.scope_generation, 5)
        self.assertEqual(replay, ())
        self.assertEqual(after_replay.selected_thread_id, "")
        self.assertEqual(after_replay.scope_generation, 5)
        self.assertEqual(after_replay.updated_at, first[0].current.updated_at)

    def test_clear_selected_thread_write_failure_commits_no_transition(self) -> None:
        store = WebWriterProfileStore(self.data_dir)
        store.update(
            "tab-1",
            selected_thread_id="thread-1",
            scope_generation=4,
        )
        store.update(
            "tab-2",
            selected_thread_id="thread-1",
            scope_generation=8,
        )

        with mock.patch.object(store, "_write_all", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.clear_selected_thread("thread-1")

        reloaded = WebWriterProfileStore(self.data_dir)
        self.assertEqual(reloaded.load("tab-1").selected_thread_id, "thread-1")
        self.assertEqual(reloaded.load("tab-1").scope_generation, 4)
        self.assertEqual(reloaded.load("tab-2").selected_thread_id, "thread-1")
        self.assertEqual(reloaded.load("tab-2").scope_generation, 8)

    def test_invalid_profile_file_fails_closed(self) -> None:
        (self.data_dir / "web_writer_profiles.json").write_text(
            '{"schema_version": 999, "profiles": {}}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "schema"):
            WebWriterProfileStore(self.data_dir).load("tab-1")

    def test_legacy_profiles_rewrite_immediately_as_navigation_only_v3(self) -> None:
        path = self.data_dir / "web_writer_profiles.json"
        for schema_version, expected_generation in ((1, 1), (2, 7)):
            with self.subTest(schema_version=schema_version):
                legacy_profile = {
                    "selected_thread_id": "thread-1",
                    "working_dir": "/work",
                    "approval_policy": "future-policy",
                    "permissions_profile_id": ":future-profile",
                    "model": "retired-model",
                    "reasoning_effort": "future-effort",
                    "updated_at": 1,
                }
                if schema_version == 2:
                    legacy_profile["scope_generation"] = 7
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": schema_version,
                            "profiles": {"tab-1": legacy_profile},
                        }
                    ),
                    encoding="utf-8",
                )

                migrated = WebWriterProfileStore(self.data_dir).load("tab-1")

                self.assertIsNotNone(migrated)
                assert migrated is not None
                self.assertEqual(migrated.selected_thread_id, "thread-1")
                self.assertEqual(migrated.working_dir, "/work")
                self.assertEqual(migrated.scope_generation, expected_generation)
                rewritten = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(rewritten["schema_version"], 3)
                self.assertEqual(
                    set(rewritten["profiles"]["tab-1"]),
                    {
                        "selected_thread_id",
                        "working_dir",
                        "scope_generation",
                        "updated_at",
                    },
                )

    def test_schema_v3_rejects_legacy_setting_fields(self) -> None:
        path = self.data_dir / "web_writer_profiles.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "profiles": {
                        "tab-1": {
                            "selected_thread_id": "",
                            "working_dir": "/work",
                            "scope_generation": 1,
                            "updated_at": 1,
                            "model": "retired-model",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "invalid web writer profile entry"):
            WebWriterProfileStore(self.data_dir).load("tab-1")

    def test_schema_v2_requires_exact_positive_integer_generation(self) -> None:
        base_profile = {
            "selected_thread_id": "thread-1",
            "working_dir": "/work",
            "approval_policy": "on-request",
            "permissions_profile_id": ":workspace",
            "model": "",
            "reasoning_effort": "",
            "updated_at": 1,
        }
        for label, generation, present in (
            ("missing", None, False),
            ("null", None, True),
            ("bool", True, True),
            ("float", 1.0, True),
            ("string", "1", True),
            ("zero", 0, True),
            ("negative", -1, True),
        ):
            with self.subTest(scope_generation=label):
                profile = dict(base_profile)
                if present:
                    profile["scope_generation"] = generation
                payload = {
                    "schema_version": 2,
                    "profiles": {"tab-1": profile},
                }
                (self.data_dir / "web_writer_profiles.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "invalid web writer profile entry",
                ):
                    WebWriterProfileStore(self.data_dir).load("tab-1")

    def test_update_rejects_non_integer_generation_instead_of_coercing(self) -> None:
        store = WebWriterProfileStore(self.data_dir)

        for generation in (True, 1.0, "1", 0, -1):
            with self.subTest(scope_generation=generation):
                with self.assertRaisesRegex(ValueError, "exact positive integer"):
                    store.update("tab-1", scope_generation=generation)


if __name__ == "__main__":
    unittest.main()
