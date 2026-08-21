from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.stores.web_next_turn_settings_store import (
    WebNextTurnSettings,
    WebNextTurnSettingsStore,
)


class WebNextTurnSettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = pathlib.Path(self.temp_dir.name)
        self.path = self.data_dir / "web_next_turn_settings.json"
        self.initial = WebNextTurnSettings(
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
            model="gpt-a",
            reasoning_effort="high",
        )

    def _store(
        self,
        *,
        model: str = "gpt-a",
        reasoning_effort: str = "high",
    ) -> WebNextTurnSettingsStore:
        return WebNextTurnSettingsStore(
            self.data_dir,
            initial=WebNextTurnSettings(
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
                model=model,
                reasoning_effort=reasoning_effort,
            ),
        )

    def _write_raw(self, value: object) -> bytes:
        original = json.dumps(value, sort_keys=True).encode()
        self.path.write_bytes(original)
        return original

    def test_absent_file_uses_config_seed_without_materializing_it(self) -> None:
        self.assertEqual(self._store().load(), self.initial)
        self.assertFalse(self.path.exists())

        changed_config = self._store(model="gpt-b", reasoning_effort="medium")

        self.assertEqual(changed_config.load().model, "gpt-b")
        self.assertEqual(changed_config.load().reasoning_effort, "medium")
        self.assertFalse(self.path.exists())

    def test_first_explicit_update_persists_and_becomes_restart_authority(self) -> None:
        updated = self._store().update(
            {
                "model": "gpt-b",
                "reasoning_effort": "medium",
            }
        )

        self.assertEqual(updated.generation, 2)
        self.assertEqual(self._store(model="gpt-config-new").load(), updated)
        self.assertTrue(self.path.exists())

    def test_partial_update_preserves_other_fields_and_advances_once(self) -> None:
        store = self._store()

        updated = store.update({"approval_policy": "on-request"})

        self.assertEqual(
            updated,
            WebNextTurnSettings(
                approval_policy="on-request",
                permissions_profile_id=":danger-full-access",
                model="gpt-a",
                reasoning_effort="high",
                generation=2,
            ),
        )
        self.assertEqual(store.update({}), updated)
        self.assertEqual(store.load().generation, 2)

    def test_validator_observes_the_lock_merged_candidate_before_write(self) -> None:
        store = self._store()
        store.update({"model": "gpt-b", "reasoning_effort": "medium"})
        seen: list[WebNextTurnSettings] = []

        updated = store.update(
            {"approval_policy": "on-request"},
            validate_merged=seen.append,
        )

        self.assertEqual(seen, [updated])
        self.assertEqual(updated.model, "gpt-b")
        self.assertEqual(updated.reasoning_effort, "medium")
        self.assertEqual(updated.approval_policy, "on-request")
        self.assertEqual(updated.generation, 3)

    def test_persisted_file_is_private(self) -> None:
        self._store().update({"model": "gpt-b"})

        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_envelopes_and_records_fail_closed_without_rewrite(self) -> None:
        canonical = {
            "approval_policy": "never",
            "permissions_profile_id": ":danger-full-access",
            "model": "gpt-a",
            "reasoning_effort": "high",
            "generation": 1,
        }
        invalid_values = {
            "top-level-list": [],
            "unversioned": {"settings": canonical},
            "future-schema": {"schema_version": 2, "records": {"settings": canonical}},
            "extra-root-key": {
                "schema_version": 1,
                "records": {"settings": canonical},
                "extra": True,
            },
            "records-list": {"schema_version": 1, "records": []},
            "empty-records": {"schema_version": 1, "records": {}},
            "wrong-record-key": {
                "schema_version": 1,
                "records": {"defaults": canonical},
            },
            "extra-record": {
                "schema_version": 1,
                "records": {"settings": canonical, "other": canonical},
            },
            "record-list": {"schema_version": 1, "records": {"settings": []}},
            "missing-field": {
                "schema_version": 1,
                "records": {"settings": {k: v for k, v in canonical.items() if k != "model"}},
            },
            "extra-field": {
                "schema_version": 1,
                "records": {"settings": {**canonical, "extra": "value"}},
            },
        }
        for name, value in invalid_values.items():
            with self.subTest(name=name):
                self.path.unlink(missing_ok=True)
                original = self._write_raw(value)

                with self.assertRaisesRegex(RuntimeError, "invalid web_next_turn_settings"):
                    self._store().load()
                with self.assertRaisesRegex(RuntimeError, "invalid web_next_turn_settings"):
                    self._store().update({"model": "gpt-b"})

                self.assertEqual(self.path.read_bytes(), original)

    def test_duplicate_json_key_fails_closed_without_rewrite(self) -> None:
        original = (
            b'{"schema_version":1,"records":{"settings":'
            b'{"approval_policy":"never","permissions_profile_id":'
            b'":danger-full-access","model":"gpt-a","model":"gpt-b",'
            b'"reasoning_effort":"high","generation":1}}}'
        )
        self.path.write_bytes(original)

        with self.assertRaisesRegex(RuntimeError, "duplicate JSON key"):
            self._store().load()

        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_field_types_values_and_generation_fail_closed(self) -> None:
        canonical = {
            "approval_policy": "never",
            "permissions_profile_id": ":danger-full-access",
            "model": "gpt-a",
            "reasoning_effort": "high",
            "generation": 1,
        }
        invalid_fields = {
            "approval-empty": {"approval_policy": ""},
            "approval-non-string": {"approval_policy": 1},
            "approval-alias": {"approval_policy": "on-failure"},
            "permissions-empty": {"permissions_profile_id": ""},
            "permissions-non-string": {"permissions_profile_id": False},
            "permissions-alias": {"permissions_profile_id": "workspace-write"},
            "model-non-string": {"model": None},
            "model-whitespace": {"model": " gpt-a "},
            "effort-non-string": {"reasoning_effort": []},
            "effort-noncanonical": {"reasoning_effort": "HIGH"},
            "generation-zero": {"generation": 0},
            "generation-bool": {"generation": True},
            "generation-string": {"generation": "1"},
            "generation-too-large": {"generation": 9_007_199_254_740_992},
        }
        for name, changes in invalid_fields.items():
            with self.subTest(name=name):
                self.path.unlink(missing_ok=True)
                record = {**canonical, **changes}
                original = self._write_raw(
                    {"schema_version": 1, "records": {"settings": record}}
                )

                with self.assertRaisesRegex(RuntimeError, "invalid web_next_turn_settings"):
                    self._store().load()

                self.assertEqual(self.path.read_bytes(), original)

    def test_update_rejects_empty_or_non_string_security_settings(self) -> None:
        store = self._store()

        for field, value in (
            ("approval_policy", ""),
            ("approval_policy", 1),
            ("permissions_profile_id", ""),
            ("permissions_profile_id", False),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    store.update({field: value})

        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_preserves_previous_record(self) -> None:
        store = self._store()
        previous = store.update({"model": "gpt-b"})
        original = self.path.read_bytes()

        with patch("bot.atomic_file.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                store.update({"model": "gpt-c"})

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(store.load(), previous)


if __name__ == "__main__":
    unittest.main()
