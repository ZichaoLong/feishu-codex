import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from bot.stores.interaction_lease_store import (
    InteractionLeaseHolder,
    InteractionLeaseStore,
    InteractionLeaseStoreUnavailable,
    make_feishu_interaction_holder,
    make_fcodex_interaction_holder,
    make_web_interaction_holder,
)


class InteractionLeaseStoreFailClosedTests(unittest.TestCase):
    def test_corrupted_json_fails_closed_without_read_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            path = data_dir / "interaction_leases.json"
            original = b"{broken"
            path.write_bytes(original)
            store = InteractionLeaseStore(data_dir)
            holder = make_fcodex_interaction_holder("fcodex:new", owner_pid=os.getpid())

            with self.assertRaises(InteractionLeaseStoreUnavailable):
                store.load("thread-1")
            with self.assertRaises(InteractionLeaseStoreUnavailable):
                store.acquire("thread-1", holder)

            self.assertEqual(path.read_bytes(), original)

    def test_future_schema_fails_closed_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            path = data_dir / "interaction_leases.json"
            original = b'{"schema_version": 999, "records": {}}\n'
            path.write_bytes(original)

            with self.assertRaises(InteractionLeaseStoreUnavailable):
                InteractionLeaseStore(data_dir).list()

            self.assertEqual(path.read_bytes(), original)

    def test_invalid_owner_pid_is_typed_unavailable_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            path = data_dir / "interaction_leases.json"
            payload = {
                "schema_version": 1,
                "records": {
                    "thread-1": {
                        "thread_id": "thread-1",
                        "holder": {
                            "kind": "fcodex",
                            "holder_id": "fcodex:old",
                            "owner_pid": "not-an-integer",
                            "sender_id": "",
                            "chat_id": "",
                        },
                        "updated_at": time.time(),
                    }
                },
            }
            original = json.dumps(payload, sort_keys=True).encode("utf-8")
            path.write_bytes(original)
            store = InteractionLeaseStore(data_dir)

            with self.assertRaises(InteractionLeaseStoreUnavailable):
                store.acquire(
                    "thread-1",
                    make_fcodex_interaction_holder("fcodex:new", owner_pid=os.getpid()),
                )

            self.assertEqual(path.read_bytes(), original)

    def test_pid_reuse_identity_prunes_old_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            path = data_dir / "interaction_leases.json"
            payload = {
                "schema_version": 1,
                "records": {
                    "thread-1": {
                        "thread_id": "thread-1",
                        "holder": {
                            "kind": "fcodex",
                            "holder_id": "fcodex:old",
                            "owner_pid": os.getpid(),
                            "sender_id": "",
                            "chat_id": "",
                            "owner_process_identity": "old-incarnation",
                        },
                        "updated_at": time.time(),
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = InteractionLeaseStore(data_dir)

            with patch(
                "bot.stores.interaction_lease_store.process_exists", return_value=True
            ):
                with patch(
                    "bot.stores.interaction_lease_store.process_identity",
                    return_value="new-incarnation",
                ):
                    self.assertIsNone(store.load("thread-1"))


class InteractionLeaseStoreTests(unittest.TestCase):
    def test_interaction_lease_store_acquire_and_release_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            holder = make_fcodex_interaction_holder(
                "fcodex:primary", owner_pid=os.getpid()
            )

            acquired = store.acquire("thread-1", holder)

            self.assertTrue(acquired.granted)
            self.assertTrue(acquired.acquired)
            self.assertEqual(store.load("thread-1").holder, holder)

            reacquired = store.acquire("thread-1", holder)

            self.assertTrue(reacquired.granted)
            self.assertFalse(reacquired.acquired)
            self.assertEqual(reacquired.lease.holder, holder)
            self.assertTrue(store.release("thread-1", holder))
            self.assertIsNone(store.load("thread-1"))

    def test_exact_release_rejects_same_holder_aba_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            holder = make_fcodex_interaction_holder(
                "fcodex:primary",
                owner_pid=os.getpid(),
            )
            with patch(
                "bot.stores.interaction_lease_store.time.time",
                return_value=100.0,
            ):
                original = store.acquire("thread-1", holder).lease
                replacement = store.force_acquire("thread-1", holder)

            self.assertIsNotNone(original)
            self.assertTrue(
                original and original.holder.same_holder(replacement.holder)
            )
            self.assertEqual(original and original.updated_at, replacement.updated_at)
            self.assertNotEqual(original and original.lease_id, replacement.lease_id)
            self.assertNotEqual(original, replacement)
            self.assertFalse(store.release_if_matches(original))
            self.assertEqual(store.load("thread-1"), replacement)
            self.assertTrue(store.release_if_matches(replacement))
            self.assertIsNone(store.load("thread-1"))

    def test_matching_turn_completion_releases_only_its_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InteractionLeaseStore(pathlib.Path(tmpdir))
            holder = make_fcodex_interaction_holder(
                "fcodex:primary",
                owner_pid=os.getpid(),
            )
            submission = store.acquire("thread-1", holder).lease
            assert submission is not None

            active = store.activate_turn(submission, "turn-1")

            self.assertIsNotNone(active)
            self.assertEqual(active and active.turn_id, "turn-1")
            self.assertFalse(store.release_turn("thread-1", "stale-turn"))
            self.assertEqual(store.load("thread-1"), active)
            self.assertTrue(store.release_turn("thread-1", "turn-1"))
            self.assertIsNone(store.load("thread-1"))

    def test_stale_submission_cannot_activate_same_holder_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InteractionLeaseStore(pathlib.Path(tmpdir))
            holder = make_fcodex_interaction_holder(
                "fcodex:primary",
                owner_pid=os.getpid(),
            )
            original = store.acquire("thread-1", holder).lease
            replacement = store.force_acquire("thread-1", holder)
            assert original is not None

            self.assertIsNone(store.activate_turn(original, "turn-old"))
            self.assertEqual(store.load("thread-1"), replacement)
            active = store.activate_turn(replacement, "turn-new")
            self.assertEqual(active and active.turn_id, "turn-new")

    def test_new_lease_generation_round_trips_through_v1_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            holder = make_fcodex_interaction_holder(
                "fcodex:primary",
                owner_pid=os.getpid(),
            )

            lease = store.acquire("thread-1", holder).lease
            payload = json.loads(
                (data_dir / "interaction_leases.json").read_text(encoding="utf-8")
            )

            self.assertIsNotNone(lease)
            self.assertEqual(
                payload["records"]["thread-1"]["lease_id"],
                lease and lease.lease_id,
            )
            self.assertEqual(InteractionLeaseStore(data_dir).load("thread-1"), lease)

    def test_legacy_v1_lease_gets_stable_deterministic_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            path = data_dir / "interaction_leases.json"
            payload = {
                "schema_version": 1,
                "records": {
                    "thread-1": {
                        "thread_id": "thread-1",
                        "holder": {
                            "kind": "web",
                            "holder_id": "web:legacy",
                            "owner_pid": 0,
                            "sender_id": "",
                            "chat_id": "",
                        },
                        "updated_at": 100.0,
                    }
                },
            }
            original = json.dumps(payload, sort_keys=True).encode("utf-8")
            path.write_bytes(original)
            store = InteractionLeaseStore(data_dir)

            first = store.load("thread-1")
            second = InteractionLeaseStore(data_dir).load("thread-1")

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertTrue(first and first.lease_id)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(store.release_if_matches(first))
            self.assertIsNone(store.load("thread-1"))

    def test_interaction_lease_store_prunes_stale_owner_before_acquire(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            stale_holder = make_fcodex_interaction_holder(
                "fcodex:stale", owner_pid=999999
            )
            current_holder = make_fcodex_interaction_holder(
                "fcodex:current", owner_pid=os.getpid()
            )
            store.force_acquire("thread-1", stale_holder)

            with patch(
                "bot.stores.interaction_lease_store.process_exists",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                acquired = store.acquire("thread-1", current_holder)

            self.assertTrue(acquired.granted)
            self.assertTrue(acquired.acquired)
            self.assertEqual(acquired.lease.holder, current_holder)
            self.assertEqual(store.load("thread-1").holder, current_holder)


class InteractionLeaseBackendStopRetirementTests(unittest.TestCase):
    def test_capture_and_retirement_cover_all_three_current_process_surfaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InteractionLeaseStore(pathlib.Path(tmpdir))
            owner_pid = os.getpid()
            for thread_id, holder in (
                (
                    "feishu-thread",
                    make_feishu_interaction_holder(
                        "sender-1",
                        "chat-1",
                        owner_pid=owner_pid,
                    ),
                ),
                (
                    "fcodex-thread",
                    make_fcodex_interaction_holder(
                        "participant-1",
                        connection_id="connection-1",
                        owner_pid=owner_pid,
                    ),
                ),
                (
                    "web-thread",
                    make_web_interaction_holder(
                        "client-1",
                        owner_pid=owner_pid,
                    ),
                ),
            ):
                store.force_acquire(thread_id, holder)

            capture = store.capture_current_process_for_backend_stop()

            self.assertEqual(capture.owner_pid, owner_pid)
            self.assertTrue(capture.owner_process_identity)
            self.assertEqual(
                tuple(lease.thread_id for lease in capture.leases),
                ("fcodex-thread", "feishu-thread", "web-thread"),
            )
            self.assertEqual(
                {lease.holder.kind for lease in capture.leases},
                {"fcodex", "feishu", "web"},
            )

            receipt = store.retire_after_backend_stop(capture)

            self.assertEqual(
                receipt.retired_thread_ids,
                ("fcodex-thread", "feishu-thread", "web-thread"),
            )
            self.assertEqual(receipt.preserved_thread_ids, ())
            self.assertEqual(store.list(), [])

            retried = store.retire_after_backend_stop(capture)

            self.assertEqual(retried.retired_thread_ids, ())
            self.assertEqual(
                retried.preserved_thread_ids,
                ("fcodex-thread", "feishu-thread", "web-thread"),
            )

    def test_retirement_preserves_same_holder_aba_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InteractionLeaseStore(pathlib.Path(tmpdir))
            holder = make_fcodex_interaction_holder(
                "participant-1",
                owner_pid=os.getpid(),
            )
            original = store.force_acquire("thread-1", holder)
            capture = store.capture_current_process_for_backend_stop()
            successor = store.force_acquire("thread-1", holder)

            receipt = store.retire_after_backend_stop(capture)

            self.assertNotEqual(original.lease_id, successor.lease_id)
            self.assertEqual(receipt.retired_thread_ids, ())
            self.assertEqual(receipt.preserved_thread_ids, ("thread-1",))
            self.assertEqual(store.load("thread-1"), successor)

    def test_retirement_preserves_same_generation_with_changed_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            holder = make_fcodex_interaction_holder(
                "participant-1",
                owner_pid=os.getpid(),
            )
            original = store.force_acquire("thread-1", holder)
            capture = store.capture_current_process_for_backend_stop()
            path = data_dir / "interaction_leases.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"]["thread-1"]["updated_at"] = original.updated_at + 1.0
            path.write_text(json.dumps(payload), encoding="utf-8")

            successor = store.load("thread-1")
            receipt = store.retire_after_backend_stop(capture)

            self.assertIsNotNone(successor)
            self.assertEqual(successor and successor.lease_id, original.lease_id)
            self.assertEqual(successor and successor.holder, original.holder)
            self.assertEqual(successor and successor.turn_id, original.turn_id)
            self.assertNotEqual(successor and successor.updated_at, original.updated_at)
            self.assertEqual(receipt.retired_thread_ids, ())
            self.assertEqual(receipt.preserved_thread_ids, ("thread-1",))
            self.assertEqual(store.load("thread-1"), successor)

    def test_retirement_preserves_foreign_pid_pid_zero_and_identity_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            owner_pid = os.getpid()
            current_identity = "current-incarnation"
            foreign_pid = owner_pid + 10_000

            def identity_for(pid: int) -> str:
                if pid == owner_pid:
                    return current_identity
                if pid == foreign_pid:
                    return "foreign-incarnation"
                return ""

            with (
                patch(
                    "bot.stores.interaction_lease_store.process_exists",
                    return_value=True,
                ),
                patch(
                    "bot.stores.interaction_lease_store.process_identity",
                    side_effect=identity_for,
                ),
            ):
                store.force_acquire(
                    "foreign-thread",
                    InteractionLeaseHolder(
                        kind="web",
                        holder_id="web:foreign",
                        owner_pid=foreign_pid,
                        owner_process_identity="foreign-incarnation",
                    ),
                )
                store.force_acquire(
                    "pid-zero-thread",
                    InteractionLeaseHolder(
                        kind="web",
                        holder_id="web:pid-zero",
                        owner_pid=0,
                    ),
                )
                store.force_acquire(
                    "current-thread",
                    InteractionLeaseHolder(
                        kind="web",
                        holder_id="web:current",
                        owner_pid=owner_pid,
                        owner_process_identity=current_identity,
                    ),
                )
                # Insert the stale-incarnation record last so ordinary store
                # cleanup cannot act before the backend-stop API takes over.
                store.force_acquire(
                    "identity-mismatch-thread",
                    InteractionLeaseHolder(
                        kind="web",
                        holder_id="web:old-incarnation",
                        owner_pid=owner_pid,
                        owner_process_identity="old-incarnation",
                    ),
                )

                capture = store.capture_current_process_for_backend_stop()
                receipt = store.retire_after_backend_stop(capture)

            self.assertEqual(
                tuple(lease.thread_id for lease in capture.leases),
                ("current-thread",),
            )
            self.assertEqual(receipt.retired_thread_ids, ("current-thread",))
            records = json.loads(
                (data_dir / "interaction_leases.json").read_text(encoding="utf-8")
            )["records"]
            self.assertEqual(
                set(records),
                {
                    "foreign-thread",
                    "identity-mismatch-thread",
                    "pid-zero-thread",
                },
            )

    def test_capture_fails_closed_for_current_pid_without_process_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            store.force_acquire(
                "thread-1",
                make_web_interaction_holder("client-1", owner_pid=os.getpid()),
            )
            path = data_dir / "interaction_leases.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"]["thread-1"]["holder"]["owner_process_identity"] = ""
            path.write_text(json.dumps(payload), encoding="utf-8")
            original = path.read_bytes()

            with self.assertRaises(InteractionLeaseStoreUnavailable):
                store.capture_current_process_for_backend_stop()

            self.assertEqual(path.read_bytes(), original)

    def test_failed_exact_cas_with_unchanged_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = InteractionLeaseStore(pathlib.Path(tmpdir))
            expected = store.force_acquire(
                "thread-1",
                make_web_interaction_holder("client-1", owner_pid=os.getpid()),
            )
            capture = store.capture_current_process_for_backend_stop()

            with patch.object(
                store,
                "_release_if_matches_without_pruning",
                return_value=False,
            ):
                with self.assertRaises(InteractionLeaseStoreUnavailable):
                    store.retire_after_backend_stop(capture)

            self.assertEqual(store.load("thread-1"), expected)

    def test_retirement_does_not_repair_store_that_became_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            store = InteractionLeaseStore(data_dir)
            store.force_acquire(
                "thread-1",
                make_web_interaction_holder("client-1", owner_pid=os.getpid()),
            )
            capture = store.capture_current_process_for_backend_stop()
            path = data_dir / "interaction_leases.json"
            broken = b"{broken-after-capture"
            path.write_bytes(broken)

            with self.assertRaises(InteractionLeaseStoreUnavailable):
                store.retire_after_backend_stop(capture)

            self.assertEqual(path.read_bytes(), broken)


if __name__ == "__main__":
    unittest.main()
