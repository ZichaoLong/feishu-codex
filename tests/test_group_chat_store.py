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
        store.set_access_policy("chat-1", "allowlist")
        store.grant_members("chat-1", ["ou_user", "ou_user2"])
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
        self.assertEqual(raw["groups"]["chat-1"]["access_policy"], "allowlist")
        self.assertEqual(raw["groups"]["chat-1"]["allowlist"], ["ou_user", "ou_user2"])
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
        self.assertEqual(snapshot["access_policy"], "allowlist")
        self.assertEqual(snapshot["allowlist"], ["ou_user", "ou_user2"])
        self.assertEqual(snapshot["boundaries"]["thread:th-1"]["seq"], 3)

    def test_store_rejects_missing_schema_version(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "groups": {
                        "chat-1": {
                            "mode": "assistant",
                            "access_policy": "admin-only",
                            "allowlist": [],
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
