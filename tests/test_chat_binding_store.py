import json
import pathlib
import tempfile
import unittest

from bot.stores.chat_binding_store import CHAT_BINDING_STORE_SCHEMA_VERSION, ChatBindingStore


class ChatBindingStoreTests(unittest.TestCase):
    def _make_store(self) -> tuple[tempfile.TemporaryDirectory[str], ChatBindingStore, pathlib.Path]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        return tempdir, ChatBindingStore(data_dir), data_dir / "chat_bindings.json"

    def test_store_round_trips_group_and_p2p_bindings(self) -> None:
        _, store, state_path = self._make_store()

        store.save(
            ("ou_user", "oc_p2p"),
            {
                "working_dir": "/tmp/p2p",
                "current_thread_id": "thread-p2p",
                "current_thread_title": "p2p title",
                "feishu_runtime_state": "attached",
                "current_thread_write_owner_thread_id": "thread-p2p",
                "approval_policy": "on-request",
                "sandbox": "workspace-write",
                "collaboration_mode": "default",
            },
        )
        store.save(
            ("__group__", "oc_group"),
            {
                "working_dir": "/tmp/group",
                "current_thread_id": "thread-group",
                "current_thread_title": "",
                "feishu_runtime_state": "released",
                "current_thread_write_owner_thread_id": "",
                "approval_policy": "never",
                "sandbox": "danger-full-access",
                "collaboration_mode": "plan",
            },
        )

        raw = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], CHAT_BINDING_STORE_SCHEMA_VERSION)
        self.assertEqual(raw["p2p_bindings"]["oc_p2p"]["ou_user"]["current_thread_id"], "thread-p2p")
        self.assertEqual(
            raw["p2p_bindings"]["oc_p2p"]["ou_user"]["current_thread_write_owner_thread_id"],
            "thread-p2p",
        )
        self.assertEqual(raw["group_bindings"]["oc_group"]["current_thread_id"], "thread-group")

        self.assertEqual(store.load(("ou_user", "oc_p2p"))["current_thread_title"], "p2p title")
        self.assertEqual(store.load(("__group__", "oc_group"))["collaboration_mode"], "plan")
        self.assertEqual(
            store.load(("ou_user", "oc_p2p"))["current_thread_write_owner_thread_id"],
            "thread-p2p",
        )

    def test_load_all_returns_all_normalized_bindings(self) -> None:
        _, store, _ = self._make_store()

        store.save(
            ("ou_user", "oc_p2p"),
            {
                "working_dir": "/tmp/p2p",
                "current_thread_id": "thread-p2p",
                "current_thread_title": "",
                "feishu_runtime_state": "attached",
                "current_thread_write_owner_thread_id": "thread-p2p",
                "approval_policy": "on-request",
                "sandbox": "workspace-write",
                "collaboration_mode": "default",
            },
        )
        store.save(
            ("__group__", "oc_group"),
            {
                "working_dir": "/tmp/group",
                "current_thread_id": "thread-group",
                "current_thread_title": "group title",
                "feishu_runtime_state": "released",
                "current_thread_write_owner_thread_id": "",
                "approval_policy": "never",
                "sandbox": "danger-full-access",
                "collaboration_mode": "plan",
            },
        )

        loaded = store.load_all()

        self.assertEqual(
            loaded[("ou_user", "oc_p2p")]["current_thread_write_owner_thread_id"],
            "thread-p2p",
        )
        self.assertEqual(loaded[("__group__", "oc_group")]["current_thread_title"], "group title")

    def test_clear_all_removes_state_file(self) -> None:
        _, store, state_path = self._make_store()

        store.save(
            ("ou_user", "oc_p2p"),
            {
                "working_dir": "/tmp/p2p",
                "current_thread_id": "thread-p2p",
                "current_thread_title": "",
                "feishu_runtime_state": "attached",
                "current_thread_write_owner_thread_id": "",
                "approval_policy": "on-request",
                "sandbox": "workspace-write",
                "collaboration_mode": "default",
            },
        )
        self.assertTrue(state_path.exists())

        store.clear_all()

        self.assertFalse(state_path.exists())

    def test_store_rejects_missing_schema_version(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "p2p_bindings": {},
                    "group_bindings": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "schema_version"):
            store.load(("ou_user", "oc_p2p"))

    def test_store_rejects_stale_schema_version(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": CHAT_BINDING_STORE_SCHEMA_VERSION - 1,
                    "p2p_bindings": {},
                    "group_bindings": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError,
            f"schema_version must be {CHAT_BINDING_STORE_SCHEMA_VERSION}",
        ):
            store.load(("ou_user", "oc_p2p"))

    def test_store_rejects_bound_thread_without_runtime_state(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": CHAT_BINDING_STORE_SCHEMA_VERSION,
                    "p2p_bindings": {
                        "oc_p2p": {
                            "ou_user": {
                                "working_dir": "/tmp/p2p",
                                "current_thread_id": "thread-1",
                                "current_thread_title": "",
                                "feishu_runtime_state": "",
                                "current_thread_write_owner_thread_id": "",
                                "approval_policy": "on-request",
                                "sandbox": "workspace-write",
                                "collaboration_mode": "default",
                            }
                        }
                    },
                    "group_bindings": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "feishu_runtime_state must be attached or released"):
            store.load(("ou_user", "oc_p2p"))

    def test_store_rejects_released_binding_with_write_owner(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": CHAT_BINDING_STORE_SCHEMA_VERSION,
                    "p2p_bindings": {
                        "oc_p2p": {
                            "ou_user": {
                                "working_dir": "/tmp/p2p",
                                "current_thread_id": "thread-1",
                                "current_thread_title": "",
                                "feishu_runtime_state": "released",
                                "current_thread_write_owner_thread_id": "thread-1",
                                "approval_policy": "on-request",
                                "sandbox": "workspace-write",
                                "collaboration_mode": "default",
                            }
                        }
                    },
                    "group_bindings": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "released binding must not carry"):
            store.load(("ou_user", "oc_p2p"))

    def test_store_rejects_runtime_state_without_thread_id(self) -> None:
        _, store, state_path = self._make_store()
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": CHAT_BINDING_STORE_SCHEMA_VERSION,
                    "p2p_bindings": {
                        "oc_p2p": {
                            "ou_user": {
                                "working_dir": "/tmp/p2p",
                                "current_thread_id": "",
                                "current_thread_title": "",
                                "feishu_runtime_state": "released",
                                "current_thread_write_owner_thread_id": "",
                                "approval_policy": "on-request",
                                "sandbox": "workspace-write",
                                "collaboration_mode": "default",
                            }
                        }
                    },
                    "group_bindings": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "must be empty when current_thread_id is empty"):
            store.load(("ou_user", "oc_p2p"))
