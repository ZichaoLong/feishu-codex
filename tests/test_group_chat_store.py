import json
import pathlib
import tempfile
import unittest

from bot.stores.group_chat_store import GroupChatStore, GROUP_CHAT_STORE_SCHEMA_VERSION


class GroupChatStoreTests(unittest.TestCase):
    def _make_store(self) -> tuple[tempfile.TemporaryDirectory[str], GroupChatStore, pathlib.Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        return tempdir, GroupChatStore(data_dir), data_dir / "group_chat_state.json"

    def test_store_writes_explicit_schema_version_and_round_trips(self) -> None:
        _, store, state_path = self._make_store()

        store.set_group_mode("chat-1", "all")
        store.activate_chat("chat-1", activated_by="ou_admin", activated_at=1712476800123)
        store.set_last_boundary(
            "chat-1",
            seq=3,
            created_at=1712476800000,
            message_ids=["m-1", "m-2"],
            scope="thread:th-1",
        )

        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], GROUP_CHAT_STORE_SCHEMA_VERSION)
        self.assertEqual(raw["groups"]["chat-1"]["mode"], "all")
        self.assertTrue(raw["groups"]["chat-1"]["activated"])
        self.assertEqual(raw["groups"]["chat-1"]["activated_by"], "ou_admin")
        self.assertEqual(raw["groups"]["chat-1"]["activated_at"], 1712476800123)
        self.assertEqual(raw["groups"]["chat-1"]["boundaries"]["main"], {
            "seq": 0,
            "created_at": 0,
            "message_ids": [],
        })
        self.assertEqual(raw["groups"]["chat-1"]["boundaries"]["thread:th-1"], {
            "seq": 3,
            "created_at": 1712476800000,
            "message_ids": ["m-1", "m-2"],
        })

        snapshot = store.group_snapshot("chat-1")
        self.assertEqual(snapshot["mode"], "all")
        self.assertTrue(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "ou_admin")
        self.assertEqual(snapshot["boundaries"]["thread:th-1"]["seq"], 3)

    def test_store_rejects_missing_schema_version(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "groups": {
                        "chat-1": {
                            "mode": "assistant",
                            "boundaries": {
                                "main": {"seq": 0, "created_at": 0, "message_ids": []}
                            },
                            "last_log_seq": 0,
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "schema_version"):
            store.get_group_mode("chat-1")

    def test_clear_chat_removes_group_state_and_log(self) -> None:
        _, store, state_path = self._make_store()

        store.set_group_mode("chat-1", "all")
        store.append_message(
            "chat-1",
            {
                "message_id": "m-1",
                "created_at": 1,
                "sender_user_id": "u-1",
                "sender_principal_id": "ou-1",
                "sender_type": "user",
                "sender_name": "User",
                "msg_type": "text",
                "thread_id": "",
                "text": "hello",
            },
        )

        self.assertTrue(store.clear_chat("chat-1"))
        self.assertFalse(store.log_path("chat-1").exists())
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["groups"], {})

    def test_store_migrates_v1_group_state_as_deactivated(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "groups": {
                        "chat-1": {
                            "mode": "assistant",
                            "access_policy": "all-members",
                            "allowlist": ["ou_user"],
                            "boundaries": {
                                "main": {"seq": 0, "created_at": 0, "message_ids": []}
                            },
                            "last_log_seq": 0,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        snapshot = store.group_snapshot("chat-1")
        self.assertFalse(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "")
        self.assertEqual(snapshot["activated_at"], 0)

    def test_activated_group_state_survives_store_restart(self) -> None:
        tempdir, store, _state_path = self._make_store()

        store.set_group_mode("chat-1", "assistant")
        store.activate_chat("chat-1", activated_by="ou_admin", activated_at=1712476800123)
        store.set_last_boundary("chat-1", seq=7, created_at=1712476800999, message_ids=["m-7"])

        reloaded = GroupChatStore(pathlib.Path(tempdir.name))
        snapshot = reloaded.group_snapshot("chat-1")

        self.assertTrue(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "ou_admin")
        self.assertEqual(snapshot["activated_at"], 1712476800123)
        self.assertEqual(snapshot["mode"], "assistant")
        self.assertEqual(snapshot["boundaries"]["main"]["seq"], 7)
        self.assertEqual(snapshot["boundaries"]["main"]["message_ids"], ["m-7"])

    @staticmethod
    def _raw_group(**overrides: object) -> dict[str, object]:
        group: dict[str, object] = {
            "mode": "assistant",
            "activated": False,
            "activated_by": "",
            "activated_at": 0,
            "boundaries": {
                "main": {"seq": 0, "created_at": 0, "message_ids": []}
            },
            "last_log_seq": 0,
        }
        group.update(overrides)
        return group

    def test_activation_metadata_requires_exact_boolean_and_consistent_fields(self) -> None:
        _, store, state_path = self._make_store()
        cases = (
            {"activated": "false"},
            {"activated": 1, "activated_by": "ou-admin", "activated_at": 1},
            {"activated": True, "activated_by": "", "activated_at": 1},
            {"activated": True, "activated_by": "ou-admin", "activated_at": 0},
            {"activated": False, "activated_by": "ou-admin", "activated_at": 0},
            {"activated": False, "activated_by": " ", "activated_at": 0},
            {"activated": False, "activated_by": "", "activated_at": 1},
            {"activated": False, "activated_by": "", "activated_at": "0"},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(index=index):
                state_path.write_text(
                    json.dumps(
                        {
                            "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
                            "groups": {"chat-bad": self._raw_group(**overrides)},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "chat-bad"):
                    store.activation_snapshot("chat-bad")

    def test_schema_v2_requires_explicit_activation_metadata_fields(self) -> None:
        _, store, state_path = self._make_store()
        raw_group = self._raw_group()
        del raw_group["activated"]
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
                    "groups": {"chat-bad": raw_group},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "chat-bad"):
            store.activation_snapshot("chat-bad")

    def test_malformed_activation_isolated_to_exact_chat(self) -> None:
        _, store, state_path = self._make_store()
        malformed = self._raw_group(activated="false")
        valid = self._raw_group(
            mode="all",
            activated=True,
            activated_by="ou-admin",
            activated_at=1712476800123,
        )
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
                    "groups": {"chat-bad": malformed, "chat-good": valid},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.assertEqual(store.get_group_mode("chat-good"), "all")
        self.assertTrue(store.is_group_activated("chat-good"))
        with self.assertRaisesRegex(ValueError, "chat-bad"):
            store.activation_snapshot("chat-bad")

        store.set_group_mode("chat-good", "assistant")
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["groups"]["chat-bad"]["activated"], "false")

        self.assertTrue(store.clear_chat("chat-bad"))
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn("chat-bad", persisted["groups"])

    def test_activation_mutation_rejects_non_contract_metadata(self) -> None:
        _, store, _state_path = self._make_store()
        with self.assertRaises(ValueError):
            store.activate_chat("chat-1", activated_by=123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            store.activate_chat("chat-1", activated_by="ou-admin", activated_at=0)
        with self.assertRaises(ValueError):
            store.activate_chat("chat-1", activated_by="ou-admin", activated_at=True)  # type: ignore[arg-type]
