import json
import pathlib
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from bot.stores.web_attachment_store import (
    WebAttachmentStore,
    WebAttachmentSubmissionClaimError,
)


_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)


class WebAttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = pathlib.Path(self.temp_dir.name)
        self.data_dir = root / "data"
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = WebAttachmentStore(
            self.data_dir,
            ttl_seconds=60,
            max_bytes=128,
            max_count=2,
        )

    def stage(
        self,
        *,
        client_id="tab-1",
        scope_key="draft:/workspace",
        content=_PNG_1X1,
        now=100,
    ):
        return self.store.stage(
            client_id=client_id,
            scope_key=scope_key,
            cwd=str(self.workspace),
            display_name="../demo.png",
            media_type="image/png",
            content=content,
            now=now,
        )

    def test_pending_attachment_is_isolated_by_client_and_draft_scope(self):
        record = self.stage()

        with self.assertRaisesRegex(ValueError, "different browser draft"):
            self.store.resolve_pending(
                client_id="tab-2",
                scope_key="draft:/workspace",
                attachment_ids=[record.attachment_id],
                now=101,
            )
        with self.assertRaisesRegex(ValueError, "different browser draft"):
            self.store.resolve_pending(
                client_id="tab-1",
                scope_key="thread:other",
                attachment_ids=[record.attachment_id],
                now=101,
            )

        resolved = self.store.resolve_pending(
            client_id="tab-1",
            scope_key="draft:/workspace",
            attachment_ids=[record.attachment_id],
            now=101,
        )
        self.assertEqual(resolved, (record,))
        self.assertEqual(pathlib.Path(record.local_path).name, f"{record.attachment_id}-demo.png")

    def test_v1_index_is_retired_without_trusting_or_deleting_workspace_file(self):
        legacy_id = str(uuid.uuid4())
        legacy_file = self.workspace / ".focus-attachments" / f"{legacy_id}-old.png"
        legacy_file.parent.mkdir()
        legacy_file.write_bytes(_PNG_1X1)
        self.data_dir.mkdir()
        cache_dir = self.data_dir / "web_attachment_cache"
        cache_dir.mkdir()
        orphan = cache_dir / "failed-upload.png"
        orphan.write_bytes(_PNG_1X1)
        index_path = self.data_dir / "web_attachments.json"
        index_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "attachments": {
                        legacy_id: {
                            "client_id": "old-tab",
                            "scope_key": "thread:old",
                            "cwd": str(self.workspace),
                            "display_name": "old.png",
                            "media_type": "image/png",
                            "size": len(_PNG_1X1),
                            "local_path": str(legacy_file),
                            "created_at": 1,
                            "expires_at": 0,
                            "submitted": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        current = self.stage(now=100)

        self.assertTrue(legacy_file.is_file())
        self.assertFalse(orphan.exists())
        persisted = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 2)
        self.assertEqual(set(persisted["attachments"]), {current.attachment_id})

    def test_unknown_or_malformed_legacy_schema_still_fails_closed(self):
        self.data_dir.mkdir()
        index_path = self.data_dir / "web_attachments.json"
        for payload in (
            {"schema_version": 999, "attachments": {}},
            {"schema_version": 1, "attachments": []},
            {"schema_version": 1, "attachments": {}, "unexpected": True},
            {
                "schema_version": 1,
                "attachments": {
                    str(uuid.uuid4()): {"local_path": str(self.workspace / "old.png")}
                },
            },
        ):
            with self.subTest(payload=payload):
                index_path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "schema"):
                    self.stage(now=100)
                self.assertEqual(json.loads(index_path.read_text(encoding="utf-8")), payload)
                self.assertFalse((self.data_dir / "web_attachment_cache").exists())

    def test_stage_metadata_failure_removes_new_private_cache_file(self):
        with patch.object(
            self.store,
            "_write_all",
            side_effect=OSError("metadata write failed"),
        ):
            with self.assertRaisesRegex(OSError, "metadata write failed"):
                self.stage(now=100)

        cache_dir = self.data_dir / "web_attachment_cache"
        self.assertEqual(list(cache_dir.iterdir()), [])

    def test_size_count_and_duplicate_limits_fail_closed(self):
        first = self.stage()
        second = self.stage()

        with self.assertRaisesRegex(ValueError, "at most 2"):
            self.stage()
        with self.assertRaisesRegex(ValueError, "128-byte"):
            self.stage(client_id="tab-2", content=b"x" * 129)
        with self.assertRaisesRegex(ValueError, "unique"):
            self.store.resolve_pending(
                client_id="tab-1",
                scope_key="draft:/workspace",
                attachment_ids=[first.attachment_id, first.attachment_id],
                now=101,
            )
        self.assertTrue(pathlib.Path(second.local_path).is_file())

    def test_submitted_attachment_is_durable_and_rollback_restores_expiry(self):
        record = self.stage()
        self.store.mark_submitted([record.attachment_id], submitted=True, now=101)

        downloaded = self.store.download(attachment_id=record.attachment_id, now=102)
        self.assertTrue(downloaded.record.submitted)
        self.assertEqual(downloaded.content, _PNG_1X1)
        self.assertGreater(downloaded.record.expires_at, 102)
        self.assertEqual(
            self.store.resolve_pending(
                client_id="tab-1",
                scope_key="draft:/workspace",
                attachment_ids=[],
                now=10_000,
            ),
            (),
        )
        self.assertTrue(pathlib.Path(record.local_path).is_file())

        self.store.mark_submitted([record.attachment_id], submitted=False, now=10_001)
        self.store.resolve_pending(
            client_id="tab-1",
            scope_key="draft:/workspace",
            attachment_ids=[],
            now=10_000_000_000,
        )
        with self.assertRaises(KeyError):
            self.store.download(attachment_id=record.attachment_id)
        self.assertFalse(pathlib.Path(record.local_path).exists())

    def test_private_cache_directory_symlink_is_rejected(self):
        outside = pathlib.Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        self.data_dir.mkdir()
        (self.data_dir / "web_attachment_cache").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(ValueError, "private directory"):
            self.stage()

    def test_cache_entry_symlink_replacement_is_rejected(self):
        record = self.stage()
        outside = pathlib.Path(self.temp_dir.name) / "outside.png"
        outside.write_bytes(b"outside")
        cache_path = pathlib.Path(record.local_path)
        cache_path.unlink()
        cache_path.symlink_to(outside)

        with self.assertRaises((KeyError, ValueError)):
            self.store.download(attachment_id=record.attachment_id, now=101)
        self.assertFalse(cache_path.exists())
        self.assertTrue(outside.exists())

    def test_observed_media_registration_is_stable_and_rejects_active_content(self):
        image = self.workspace / "result.png"
        image.write_bytes(_PNG_1X1)

        first = self.store.register_observed_media(
            cwd=str(self.workspace),
            local_path="result.png",
            now=100,
        )
        second = self.store.register_observed_media(
            cwd=str(self.workspace),
            local_path=str(image),
            now=200,
        )

        self.assertEqual(first.attachment_id, second.attachment_id)
        self.assertTrue(first.submitted)
        self.assertEqual(
            self.store.attachment_id_for_path(first.local_path, now=201),
            first.attachment_id,
        )
        self.assertNotEqual(pathlib.Path(first.local_path), image)
        self.assertTrue(pathlib.Path(first.local_path).is_relative_to(self.data_dir))
        self.assertEqual(pathlib.Path(first.local_path).read_bytes(), _PNG_1X1)

        changed_bytes = _PNG_1X1 + b"\x00"
        image.write_bytes(changed_bytes)
        changed = self.store.register_observed_media(
            cwd=str(self.workspace),
            local_path=str(image),
            now=202,
        )
        self.assertNotEqual(changed.attachment_id, first.attachment_id)
        self.assertEqual(
            self.store.download(
                attachment_id=changed.attachment_id,
                now=203,
            ).content,
            changed_bytes,
        )

        svg = self.workspace / "active.svg"
        svg.write_text("<svg/>", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not safe"):
            self.store.register_observed_media(
                cwd=str(self.workspace),
                local_path=str(svg),
            )

    def test_observed_media_cannot_escape_workspace(self):
        outside = pathlib.Path(self.temp_dir.name) / "outside.png"
        outside.write_bytes(b"png")

        with self.assertRaisesRegex(ValueError, "inside the thread workspace"):
            self.store.register_observed_media(
                cwd=str(self.workspace),
                local_path=str(outside),
            )

        (self.workspace / "linked.png").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "inside the thread workspace"):
            self.store.register_observed_media(
                cwd=str(self.workspace),
                local_path="linked.png",
            )

    def test_delete_scope_removes_only_staged_files_owned_by_that_thread(self):
        first = self.stage(scope_key="thread:one")
        second = self.stage(scope_key="thread:two")
        self.store.mark_submitted([first.attachment_id], submitted=True, now=101)
        self.store.mark_submitted([second.attachment_id], submitted=True, now=101)

        removed = self.store.delete_scope("thread:one")

        self.assertEqual(removed, [first.attachment_id])
        self.assertFalse(pathlib.Path(first.local_path).exists())
        self.assertTrue(pathlib.Path(second.local_path).exists())

    def test_submission_claim_atomically_precedes_concurrent_scope_delete(self):
        pending = self.stage(scope_key="thread:one")
        cache_path = pathlib.Path(pending.local_path)
        claim_commit_started = threading.Event()
        delete_attempted = threading.Event()
        deletion_outcomes: list[object] = []
        original_write_all = self.store._write_all

        def pause_claim_commit(records):
            claimed = records.get(pending.attachment_id)
            if claimed is not None and claimed.submitted:
                claim_commit_started.set()
                if not delete_attempted.wait(timeout=2):
                    raise AssertionError("delete did not contend with submission claim")
            return original_write_all(records)

        def delete_scope() -> None:
            try:
                if not claim_commit_started.wait(timeout=2):
                    raise AssertionError("submission claim did not reach commit")
                delete_attempted.set()
                deletion_outcomes.append(self.store.delete_scope("thread:one"))
            except BaseException as exc:  # pragma: no cover - asserted below
                deletion_outcomes.append(exc)

        deletion = threading.Thread(target=delete_scope)
        deletion.start()
        with patch.object(self.store, "_write_all", side_effect=pause_claim_commit):
            claimed_records, receipt = self.store.claim_pending_submission(
                client_id="tab-1",
                scope_key="thread:one",
                attachment_ids=[pending.attachment_id],
                now=101,
            )
            deletion.join(timeout=2)

        self.assertFalse(deletion.is_alive())
        self.assertTrue(claimed_records[0].submitted)
        self.assertEqual(deletion_outcomes, [[pending.attachment_id]])
        with self.assertRaises(KeyError):
            self.store.download(attachment_id=pending.attachment_id, now=102)
        self.assertTrue(cache_path.is_file())

        restored = self.store.rollback_submission_claim(receipt, now=103)

        self.assertEqual(restored, ())
        self.assertFalse(cache_path.exists())

    def test_scope_delete_atomically_precedes_concurrent_submission_claim(self):
        pending = self.stage(scope_key="thread:one")
        delete_commit_started = threading.Event()
        claim_attempted = threading.Event()
        claim_outcomes: list[object] = []
        original_write_all = self.store._write_all

        def pause_delete_commit(records):
            if pending.attachment_id not in records:
                delete_commit_started.set()
                if not claim_attempted.wait(timeout=2):
                    raise AssertionError("submission claim did not contend with delete")
            return original_write_all(records)

        def claim_submission() -> None:
            try:
                if not delete_commit_started.wait(timeout=2):
                    raise AssertionError("scope delete did not reach commit")
                claim_attempted.set()
                claim_outcomes.append(
                    self.store.claim_pending_submission(
                        client_id="tab-1",
                        scope_key="thread:one",
                        attachment_ids=[pending.attachment_id],
                        now=101,
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                claim_outcomes.append(exc)

        claiming = threading.Thread(target=claim_submission)
        claiming.start()
        with patch.object(self.store, "_write_all", side_effect=pause_delete_commit):
            removed = self.store.delete_scope("thread:one")
            claiming.join(timeout=2)

        self.assertFalse(claiming.is_alive())
        self.assertEqual(removed, [pending.attachment_id])
        self.assertEqual(len(claim_outcomes), 1)
        self.assertIsInstance(claim_outcomes[0], ValueError)
        self.assertIn("missing or expired", str(claim_outcomes[0]))
        self.assertFalse(pathlib.Path(pending.local_path).exists())

    def test_submission_claim_rejects_foreign_cloned_and_repeated_release(self):
        pending = self.stage(scope_key="thread:one")
        _claimed, receipt = self.store.claim_pending_submission(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pending.attachment_id],
            now=101,
        )
        clone = replace(receipt)

        other_store = WebAttachmentStore(
            pathlib.Path(self.temp_dir.name) / "other-data",
            ttl_seconds=60,
            max_bytes=128,
            max_count=2,
        )
        foreign_record = other_store.stage(
            client_id="tab-2",
            scope_key="thread:two",
            cwd=str(self.workspace),
            display_name="other.png",
            media_type="image/png",
            content=_PNG_1X1,
            now=100,
        )
        _foreign_claimed, foreign_receipt = other_store.claim_pending_submission(
            client_id="tab-2",
            scope_key="thread:two",
            attachment_ids=[foreign_record.attachment_id],
            now=101,
        )

        with self.assertRaisesRegex(WebAttachmentSubmissionClaimError, "another store"):
            self.store.release_submission_claim(foreign_receipt)
        with self.assertRaisesRegex(WebAttachmentSubmissionClaimError, "forged|exact"):
            self.store.release_submission_claim(clone)

        self.store.release_submission_claim(receipt)
        self.assertTrue(
            self.store.download(
                attachment_id=pending.attachment_id,
                now=102,
            ).record.submitted
        )
        with self.assertRaisesRegex(
            WebAttachmentSubmissionClaimError, "already released"
        ):
            self.store.release_submission_claim(receipt)

        other_store.release_submission_claim(foreign_receipt)

    def test_deferred_unlink_does_not_delay_unclaimed_scope_files(self):
        pinned = self.stage(scope_key="thread:one")
        unpinned = self.stage(scope_key="thread:one")
        _claimed, receipt = self.store.claim_pending_submission(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pinned.attachment_id],
            now=101,
        )

        self.store.delete_scope("thread:one")

        self.assertTrue(pathlib.Path(pinned.local_path).is_file())
        self.assertFalse(pathlib.Path(unpinned.local_path).exists())
        self.store.release_submission_claim(receipt)
        self.assertFalse(pathlib.Path(pinned.local_path).exists())

    def test_known_no_effect_rollback_restores_exact_current_claim(self):
        pending = self.stage(scope_key="thread:one")
        claimed, receipt = self.store.claim_pending_submission(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pending.attachment_id],
            now=101,
        )
        self.assertTrue(claimed[0].submitted)

        restored_ids = self.store.rollback_submission_claim(receipt, now=102)
        restored = self.store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pending.attachment_id],
            now=103,
        )

        self.assertEqual(restored_ids, (pending.attachment_id,))
        self.assertFalse(restored[0].submitted)
        self.assertTrue(pathlib.Path(pending.local_path).is_file())

    def test_known_no_effect_rollback_never_restores_partial_claim_after_cleanup(self):
        first = self.stage(scope_key="thread:one")
        second = self.stage(scope_key="thread:one")
        _claimed, receipt = self.store.claim_pending_submission(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[first.attachment_id, second.attachment_id],
            now=101,
        )
        self.store._retained_max_count = 2
        newest = self.stage(scope_key="thread:other", now=102)

        self.store.mark_submitted(
            [newest.attachment_id],
            submitted=True,
            now=103,
        )
        before_rollback = self.store._read_all()
        surviving_claim_ids = {
            attachment_id
            for attachment_id in (first.attachment_id, second.attachment_id)
            if attachment_id in before_rollback
        }
        self.assertEqual(len(surviving_claim_ids), 1)

        restored_ids = self.store.rollback_submission_claim(receipt, now=104)

        self.assertEqual(restored_ids, ())
        after_rollback = self.store._read_all()
        surviving_id = surviving_claim_ids.pop()
        self.assertTrue(after_rollback[surviving_id].submitted)
        self.assertTrue(after_rollback[newest.attachment_id].submitted)
        removed = first if first.attachment_id not in after_rollback else second
        self.assertFalse(pathlib.Path(removed.local_path).exists())

    def test_known_no_effect_rollback_write_failure_releases_exact_claim(self):
        pending = self.stage(scope_key="thread:one")
        _claimed, receipt = self.store.claim_pending_submission(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pending.attachment_id],
            now=101,
        )

        with patch.object(
            self.store,
            "_write_all",
            side_effect=OSError("metadata write failed"),
        ):
            with self.assertRaisesRegex(OSError, "metadata write failed"):
                self.store.rollback_submission_claim(receipt, now=102)

        with self.assertRaisesRegex(
            WebAttachmentSubmissionClaimError, "already released"
        ):
            self.store.release_submission_claim(receipt)
        self.assertTrue(
            self.store.download(
                attachment_id=pending.attachment_id,
                now=103,
            ).record.submitted
        )
        self.store.delete_scope("thread:one")
        self.assertFalse(pathlib.Path(pending.local_path).exists())

    def test_known_no_effect_rollback_validation_failure_releases_file_pin(self):
        pending = self.stage(scope_key="thread:one")
        _claimed, receipt = self.store.claim_pending_submission(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pending.attachment_id],
            now=101,
        )

        with patch.object(
            self.store,
            "_validate_submission_cache_record",
            side_effect=ValueError("cache validation failed"),
        ):
            with self.assertRaisesRegex(ValueError, "cache validation failed"):
                self.store.rollback_submission_claim(receipt, now=102)

        with self.assertRaisesRegex(
            WebAttachmentSubmissionClaimError, "already released"
        ):
            self.store.release_submission_claim(receipt)
        self.store.delete_scope("thread:one")
        self.assertFalse(pathlib.Path(pending.local_path).exists())

    def test_delete_pending_scope_preserves_submitted_and_other_browser_files(self):
        pending = self.stage(scope_key="thread:one")
        submitted = self.stage(scope_key="thread:one")
        other_browser = self.stage(client_id="tab-2", scope_key="thread:one")
        self.store.mark_submitted([submitted.attachment_id], submitted=True, now=101)

        removed = self.store.delete_pending_scope(
            client_id="tab-1",
            scope_key="thread:one",
        )

        self.assertEqual(removed, [pending.attachment_id])
        self.assertFalse(pathlib.Path(pending.local_path).exists())
        self.assertTrue(pathlib.Path(submitted.local_path).exists())
        self.assertTrue(pathlib.Path(other_browser.local_path).exists())

    def test_delete_pending_scope_write_failure_preserves_metadata_and_bytes(self):
        pending = self.stage(scope_key="draft:/work")

        with patch.object(
            self.store,
            "_write_all",
            side_effect=OSError("metadata write failed"),
        ):
            with self.assertRaisesRegex(OSError, "metadata write failed"):
                self.store.delete_pending_scope(
                    client_id="tab-1",
                    scope_key="draft:/work",
                )

        self.assertTrue(pathlib.Path(pending.local_path).exists())
        resolved = self.store.resolve_pending(
            client_id="tab-1",
            scope_key="draft:/work",
            attachment_ids=[pending.attachment_id],
            now=101,
        )
        self.assertEqual(resolved, (pending,))

    def test_rebind_pending_scope_atomically_preserves_bytes_and_ownership(self):
        pending = self.stage(scope_key="thread:one")
        submitted = self.stage(scope_key="thread:one")
        other_browser = self.stage(client_id="tab-2", scope_key="thread:one")
        self.store.mark_submitted([submitted.attachment_id], submitted=True, now=101)

        moved = self.store.rebind_pending_scope(
            client_id="tab-1",
            source_scope_key="thread:one",
            target_scope_key="draft:/workspace",
            cwd=str(self.workspace),
        )

        self.assertEqual(moved, [pending.attachment_id])
        rebound = self.store.resolve_pending(
            client_id="tab-1",
            scope_key="draft:/workspace",
            attachment_ids=[pending.attachment_id],
            now=101,
        )
        self.assertEqual(rebound[0].scope_key, "draft:/workspace")
        self.assertEqual(rebound[0].cwd, str(self.workspace.resolve()))
        self.assertTrue(pathlib.Path(pending.local_path).is_file())
        self.assertTrue(self.store.download(attachment_id=submitted.attachment_id, now=102).record.submitted)
        with self.assertRaisesRegex(ValueError, "different browser draft"):
            self.store.resolve_pending(
                client_id="tab-2",
                scope_key="draft:/workspace",
                attachment_ids=[other_browser.attachment_id],
                now=101,
            )

    def test_rebind_pending_scope_write_failure_preserves_old_scope(self):
        pending = self.stage(scope_key="thread:one")

        with patch.object(
            self.store,
            "_write_all",
            side_effect=OSError("metadata write failed"),
        ):
            with self.assertRaisesRegex(OSError, "metadata write failed"):
                self.store.rebind_pending_scope(
                    client_id="tab-1",
                    source_scope_key="thread:one",
                    target_scope_key="draft:/workspace",
                    cwd=str(self.workspace),
                )

        resolved = self.store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:one",
            attachment_ids=[pending.attachment_id],
            now=101,
        )
        self.assertEqual(resolved, (pending,))
        self.assertTrue(pathlib.Path(pending.local_path).is_file())

    def test_declared_native_media_must_match_verified_bytes(self):
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.store.stage(
                client_id="tab-1",
                scope_key="draft:/workspace",
                cwd=str(self.workspace),
                display_name="spoofed.png",
                media_type="image/png",
                content=b"not an image",
            )

        wav = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.store.stage(
                client_id="tab-1",
                scope_key="draft:/workspace",
                cwd=str(self.workspace),
                display_name="spoofed.wav",
                media_type="audio/wav",
                content=_PNG_1X1,
            )
        record = self.store.stage(
            client_id="tab-1",
            scope_key="draft:/workspace",
            cwd=str(self.workspace),
            display_name="sample.wav",
            media_type="audio/x-wav",
            content=wav,
        )
        self.assertEqual(record.media_type, "audio/wav")


if __name__ == "__main__":
    unittest.main()
