"""Persistent staged attachments for one Focus Web instance."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import pathlib
import re
import stat
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace

from bot.input_media_contract import (
    detect_native_media_type,
    is_native_input_media_type,
    is_safe_image_media_type,
    normalize_native_media_type,
    validate_declared_native_media,
)


_SCHEMA_VERSION = 2
_RETIRED_SCHEMA_VERSION = 1
_RETIRED_V1_ENTRY_KEYS = frozenset(
    {
        "client_id",
        "scope_key",
        "cwd",
        "display_name",
        "media_type",
        "size",
        "local_path",
        "created_at",
        "expires_at",
        "submitted",
    }
)
_CACHE_DIRNAME = "web_attachment_cache"
_DEFAULT_RETAINED_TTL_SECONDS = 30 * 24 * 60 * 60
_DEFAULT_RETAINED_MAX_BYTES = 512 * 1024 * 1024
_DEFAULT_RETAINED_MAX_COUNT = 1024


@dataclass(frozen=True, slots=True)
class WebAttachmentRecord:
    attachment_id: str
    client_id: str
    scope_key: str
    cwd: str
    display_name: str
    media_type: str
    size: int
    local_path: str
    cache_name: str
    content_sha256: str
    file_device: int
    file_inode: int
    created_at: float
    last_accessed_at: float
    expires_at: float
    submitted: bool = False
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class WebAttachmentDownload:
    record: WebAttachmentRecord
    content: bytes


@dataclass(frozen=True, slots=True, eq=False)
class WebAttachmentSubmissionClaimReceipt:
    """Opaque process-local receipt for one exact attachment submission claim."""

    _store_token: object = field(repr=False)
    _receipt_token: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class _WebAttachmentSubmissionClaimState:
    receipt: WebAttachmentSubmissionClaimReceipt
    records: tuple[WebAttachmentRecord, ...]


class WebAttachmentSubmissionClaimError(RuntimeError):
    """A claim receipt is stale, forged, or owned by another attachment store."""


class WebAttachmentStore:
    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        ttl_seconds: float,
        max_bytes: int = 25 * 1024 * 1024,
        max_count: int = 8,
        retained_ttl_seconds: float = _DEFAULT_RETAINED_TTL_SECONDS,
        retained_max_bytes: int = _DEFAULT_RETAINED_MAX_BYTES,
        retained_max_count: int = _DEFAULT_RETAINED_MAX_COUNT,
    ) -> None:
        self._data_dir = pathlib.Path(data_dir).expanduser()
        self._ttl_seconds = max(float(ttl_seconds), 60.0)
        self._max_bytes = max(int(max_bytes), 1)
        self._max_count = max(int(max_count), 1)
        self._retained_ttl_seconds = max(float(retained_ttl_seconds), self._ttl_seconds)
        self._retained_max_bytes = max(int(retained_max_bytes), self._max_bytes)
        self._retained_max_count = max(int(retained_max_count), self._max_count)
        self._lock = threading.Lock()
        self._submission_claim_store_token = object()
        self._submission_claims: dict[object, _WebAttachmentSubmissionClaimState] = {}
        self._submission_claim_tokens_by_cache_name: dict[str, set[object]] = {}
        self._deferred_unlink_cache_names: set[str] = set()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def max_count(self) -> int:
        return self._max_count

    def stage(
        self,
        *,
        client_id: str,
        scope_key: str,
        cwd: str,
        display_name: str,
        media_type: str,
        content: bytes,
        now: float | None = None,
    ) -> WebAttachmentRecord:
        normalized_client_id = self._require_text(client_id, "client_id", max_length=128)
        normalized_scope = self._require_text(scope_key, "scope_key", max_length=4096)
        normalized_name = self._safe_name(display_name)
        normalized_media_type = self._normalized_media_type(media_type)
        payload = bytes(content)
        if not payload:
            raise ValueError("Attachment is empty.")
        if len(payload) > self._max_bytes:
            raise ValueError(f"Attachment exceeds the {self._max_bytes}-byte limit.")
        normalized_media_type = self.validate_native_input(
            normalized_media_type,
            payload,
        )
        workspace = pathlib.Path(self._require_text(cwd, "cwd", max_length=4096)).expanduser()
        if not workspace.is_dir():
            raise ValueError("Attachment workspace does not exist or is not a directory.")
        workspace = workspace.resolve()

        timestamp = time.time() if now is None else float(now)
        attachment_id = str(uuid.uuid4())
        cache_name = f"{attachment_id}-{normalized_name}"

        with self._lock:
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            pending_count = sum(
                1
                for item in records.values()
                if item.client_id == normalized_client_id
                and item.scope_key == normalized_scope
                and not item.submitted
            )
            if pending_count >= self._max_count:
                raise ValueError(f"A draft can contain at most {self._max_count} attachments.")
            try:
                file_metadata = self._write_private_cache_file(cache_name, payload)
                record = WebAttachmentRecord(
                    attachment_id=attachment_id,
                    client_id=normalized_client_id,
                    scope_key=normalized_scope,
                    cwd=str(workspace),
                    display_name=normalized_name,
                    media_type=normalized_media_type or "application/octet-stream",
                    size=len(payload),
                    local_path=str(self._cache_path(cache_name)),
                    cache_name=cache_name,
                    content_sha256=hashlib.sha256(payload).hexdigest(),
                    file_device=int(file_metadata.st_dev),
                    file_inode=int(file_metadata.st_ino),
                    created_at=timestamp,
                    last_accessed_at=timestamp,
                    expires_at=timestamp + self._ttl_seconds,
                )
                records[attachment_id] = record
                self._write_all(records)
            except Exception:
                self._unlink_cache_name(cache_name)
                raise
        return record

    def resolve_pending(
        self,
        *,
        client_id: str,
        scope_key: str,
        attachment_ids: list[str],
        now: float | None = None,
    ) -> tuple[WebAttachmentRecord, ...]:
        normalized_client_id = self._require_text(client_id, "client_id", max_length=128)
        normalized_scope = self._require_text(scope_key, "scope_key", max_length=4096)
        normalized_ids = [self._require_id(value) for value in attachment_ids]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("Attachment ids must be unique.")
        if len(normalized_ids) > self._max_count:
            raise ValueError(f"A prompt can contain at most {self._max_count} attachments.")
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            resolved: list[WebAttachmentRecord] = []
            for attachment_id in normalized_ids:
                record = records.get(attachment_id)
                if record is None:
                    raise ValueError(f"Attachment {attachment_id} is missing or expired.")
                if record.client_id != normalized_client_id or record.scope_key != normalized_scope:
                    raise ValueError("Attachment belongs to a different browser draft.")
                if record.submitted:
                    raise ValueError("Attachment has already been submitted.")
                try:
                    self._validate_cache_record(record)
                    if self.is_native_input_media_type(record.media_type):
                        self.validate_native_input(
                            record.media_type,
                            self._read_cache_record(record),
                        )
                except (KeyError, ValueError) as exc:
                    raise ValueError(
                        "Attachment staging is incomplete; upload the draft again."
                    ) from exc
                resolved.append(record)
            if records:
                self._write_all(records)
            return tuple(resolved)

    def register_observed_media(
        self,
        *,
        cwd: str,
        local_path: str,
        now: float | None = None,
    ) -> WebAttachmentRecord:
        """Copy an app-server media path into the private authenticated cache."""

        workspace = pathlib.Path(
            self._require_text(cwd, "cwd", max_length=4096)
        ).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError("Observed media workspace does not exist.")
        source_path = self._require_text(local_path, "local_path", max_length=8192)
        payload, source_name, normalized_source_path = self._read_workspace_file(
            workspace=workspace,
            local_path=source_path,
        )
        media_type = self._safe_preview_media_type(pathlib.Path(source_name))
        if not media_type:
            raise ValueError("Observed file type is not safe for inline preview.")
        media_type = self.validate_native_input(media_type, payload)
        digest = hashlib.sha256(payload).hexdigest()
        timestamp = time.time() if now is None else float(now)
        observed_scope = f"observed:{normalized_source_path}:{digest}"
        with self._lock:
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            existing = next(
                (
                    record
                    for record in records.values()
                    if record.submitted and record.scope_key == observed_scope
                ),
                None,
            )
            if existing is not None:
                try:
                    self._validate_cache_record(existing)
                except (KeyError, ValueError):
                    records.pop(existing.attachment_id, None)
                else:
                    touched = replace(existing, last_accessed_at=timestamp)
                    records[touched.attachment_id] = touched
                    self._write_all(records)
                    return touched

            attachment_id = str(uuid.uuid4())
            cache_name = f"{attachment_id}-{self._safe_name(source_name)}"
            file_metadata = self._write_private_cache_file(cache_name, payload)
            record = WebAttachmentRecord(
                attachment_id=attachment_id,
                client_id="focus-web-projection",
                scope_key=observed_scope,
                cwd=str(workspace),
                display_name=self._safe_name(source_name),
                media_type=media_type,
                size=len(payload),
                local_path=str(self._cache_path(cache_name)),
                cache_name=cache_name,
                content_sha256=digest,
                file_device=int(file_metadata.st_dev),
                file_inode=int(file_metadata.st_ino),
                created_at=timestamp,
                last_accessed_at=timestamp,
                expires_at=timestamp + self._retained_ttl_seconds,
                submitted=True,
                source_path=normalized_source_path,
            )
            try:
                records[attachment_id] = record
                records = self._cleanup_locked(records, now=timestamp)
                if attachment_id not in records:
                    raise ValueError("Observed media cache capacity was exhausted.")
                self._write_all(records)
            except Exception:
                self._unlink_cache_name(cache_name)
                raise
            return record

    def mark_submitted(
        self,
        attachment_ids: list[str],
        *,
        submitted: bool,
        scope_key: str | None = None,
        now: float | None = None,
    ) -> None:
        normalized_ids = [self._require_id(value) for value in attachment_ids]
        if not normalized_ids:
            return
        normalized_scope = (
            self._require_text(scope_key, "scope_key", max_length=4096)
            if scope_key is not None
            else None
        )
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            missing = [attachment_id for attachment_id in normalized_ids if attachment_id not in records]
            if missing:
                raise ValueError(f"Attachment metadata is missing: {', '.join(missing)}")
            for attachment_id in normalized_ids:
                record = records[attachment_id]
                self._validate_cache_record(record)
                records[attachment_id] = replace(
                    record,
                    submitted=bool(submitted),
                    scope_key=normalized_scope or record.scope_key,
                    last_accessed_at=timestamp,
                    expires_at=timestamp + (
                        self._retained_ttl_seconds if submitted else self._ttl_seconds
                    ),
                )
            self._write_all(self._cleanup_locked(records, now=timestamp))

    def claim_pending_submission(
        self,
        *,
        client_id: str,
        scope_key: str,
        attachment_ids: list[str],
        now: float | None = None,
    ) -> tuple[
        tuple[WebAttachmentRecord, ...],
        WebAttachmentSubmissionClaimReceipt,
    ]:
        """Atomically consume pending records and pin their exact cache bytes.

        The receipt owns only this local claim and physical-file lifetime. Scope
        deletion and bounded cleanup may retire claimed records from durable
        metadata immediately, but defer physical unlink until the exact claim
        settles. A receipt is not evidence that an upstream effect occurred.
        """

        normalized_client_id = self._require_text(
            client_id, "client_id", max_length=128
        )
        normalized_scope = self._require_text(scope_key, "scope_key", max_length=4096)
        normalized_ids = [self._require_id(value) for value in attachment_ids]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("Attachment ids must be unique.")
        if len(normalized_ids) > self._max_count:
            raise ValueError(
                f"A prompt can contain at most {self._max_count} attachments."
            )
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            claimed_records: list[WebAttachmentRecord] = []
            updated = dict(records)
            for attachment_id in normalized_ids:
                record = records.get(attachment_id)
                if record is None:
                    raise ValueError(
                        f"Attachment {attachment_id} is missing or expired."
                    )
                if (
                    record.client_id != normalized_client_id
                    or record.scope_key != normalized_scope
                ):
                    raise ValueError("Attachment belongs to a different browser draft.")
                if record.submitted:
                    raise ValueError("Attachment has already been submitted.")
                self._validate_submission_cache_record(record)
                claimed = replace(
                    record,
                    submitted=True,
                    last_accessed_at=timestamp,
                    expires_at=timestamp + self._retained_ttl_seconds,
                )
                updated[attachment_id] = claimed
                claimed_records.append(claimed)

            retained, retired_cache_names = self._cleanup_plan_locked(
                updated,
                now=timestamp,
            )
            if any(record.attachment_id not in retained for record in claimed_records):
                raise ValueError("Attachment cache capacity was exhausted.")

            receipt_token = object()
            receipt = WebAttachmentSubmissionClaimReceipt(
                _store_token=self._submission_claim_store_token,
                _receipt_token=receipt_token,
            )
            state = _WebAttachmentSubmissionClaimState(
                receipt=receipt,
                records=tuple(claimed_records),
            )
            self._submission_claims[receipt_token] = state
            for record in claimed_records:
                self._submission_claim_tokens_by_cache_name.setdefault(
                    record.cache_name,
                    set(),
                ).add(receipt_token)
            try:
                self._write_all(retained)
            except BaseException:
                self._release_submission_claim_locked(receipt)
                raise
            for cache_name in retired_cache_names:
                self._retire_cache_name_locked(cache_name)
            return state.records, receipt

    def release_submission_claim(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt,
    ) -> None:
        """Keep submitted metadata and release one exact claim's file pins."""

        with self._lock:
            self._release_submission_claim_locked(receipt)

    def rollback_submission_claim(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt,
        *,
        now: float | None = None,
    ) -> tuple[str, ...]:
        """Restore still-current records after an authoritative no-effect result.

        A scope deletion commits logical retirement independently of this
        claim. Missing or re-scoped records are therefore skipped and never
        recreated from the process-local receipt.
        """

        timestamp = time.time() if now is None else float(now)
        with self._lock:
            state = self._require_submission_claim_locked(receipt)
            try:
                records = self._read_all()
                current_records: list[WebAttachmentRecord] = []
                for claimed in state.records:
                    current = records.get(claimed.attachment_id)
                    if current is None or (
                        current.client_id != claimed.client_id
                        or current.scope_key != claimed.scope_key
                        or not current.submitted
                    ):
                        # One prompt owns one exact attachment set. Never
                        # reopen only the members which happened to survive a
                        # concurrent cleanup or scope change.
                        return ()
                    if not self._same_cache_identity(current, claimed):
                        raise WebAttachmentSubmissionClaimError(
                            "Web attachment submission claim no longer matches its exact cache record."
                        )
                    self._validate_submission_cache_record(current)
                    current_records.append(current)

                updated = dict(records)
                for current in current_records:
                    updated[current.attachment_id] = replace(
                        current,
                        submitted=False,
                        last_accessed_at=timestamp,
                        expires_at=timestamp + self._ttl_seconds,
                    )
                if current_records:
                    self._write_all(updated)
                return tuple(record.attachment_id for record in current_records)
            finally:
                # An authoritative no-effect outcome ends the external file
                # read even when local metadata restoration fails. Keep the
                # submitted record fail-closed, but never leak its process-local
                # pin or strand a deferred scope-deletion unlink.
                self._release_submission_claim_locked(receipt)

    def delete_scope(self, scope_key: str) -> list[str]:
        normalized_scope = self._require_text(scope_key, "scope_key", max_length=4096)
        removed: list[str] = []
        removed_records: list[WebAttachmentRecord] = []
        with self._lock:
            records = self._read_all()
            retained: dict[str, WebAttachmentRecord] = {}
            for attachment_id, record in records.items():
                if record.scope_key != normalized_scope:
                    retained[attachment_id] = record
                    continue
                removed.append(attachment_id)
                removed_records.append(record)
            if removed:
                # Commit logical invalidation before deleting cache bytes. A
                # metadata write failure therefore preserves a usable record
                # instead of leaving one which points at an already-unlinked
                # file. Physical deletion is idempotent and best-effort.
                self._write_all(retained)
                for record in removed_records:
                    self._retire_cache_name_locked(record.cache_name)
        return removed

    def delete_pending_scope(self, *, client_id: str, scope_key: str) -> list[str]:
        """Invalidate only one browser's unsubmitted attachments in a scope."""

        normalized_client_id = self._require_text(
            client_id,
            "client_id",
            max_length=128,
        )
        normalized_scope = self._require_text(
            scope_key,
            "scope_key",
            max_length=4096,
        )
        removed: list[str] = []
        removed_records: list[WebAttachmentRecord] = []
        with self._lock:
            records = self._read_all()
            retained: dict[str, WebAttachmentRecord] = {}
            for attachment_id, record in records.items():
                if (
                    record.client_id != normalized_client_id
                    or record.scope_key != normalized_scope
                    or record.submitted
                ):
                    retained[attachment_id] = record
                    continue
                removed.append(attachment_id)
                removed_records.append(record)
            if removed:
                # Keep metadata as the logical authority, matching
                # ``delete_scope``: a failed atomic metadata write must leave
                # every old record usable instead of pointing it at bytes
                # which this method already removed.
                self._write_all(retained)
                for record in removed_records:
                    self._retire_cache_name_locked(record.cache_name)
        return removed

    def rebind_pending_scope(
        self,
        *,
        client_id: str,
        source_scope_key: str,
        target_scope_key: str,
        cwd: str,
        attachment_ids: list[str] | None = None,
    ) -> list[str]:
        """Atomically move one browser's pending records between same-cwd scopes.

        Cache bytes are unchanged.  The optional id set is used only to roll
        back an earlier move without also capturing records which already
        belonged to the target scope.
        """

        normalized_client_id = self._require_text(
            client_id,
            "client_id",
            max_length=128,
        )
        normalized_source = self._require_text(
            source_scope_key,
            "source_scope_key",
            max_length=4096,
        )
        normalized_target = self._require_text(
            target_scope_key,
            "target_scope_key",
            max_length=4096,
        )
        workspace = pathlib.Path(
            self._require_text(cwd, "cwd", max_length=4096)
        ).expanduser()
        if not workspace.is_dir():
            raise ValueError("Attachment workspace does not exist or is not a directory.")
        normalized_cwd = str(workspace.resolve())
        selected_ids = None
        if attachment_ids is not None:
            normalized_ids = [self._require_id(value) for value in attachment_ids]
            if len(normalized_ids) != len(set(normalized_ids)):
                raise ValueError("Attachment ids must be unique.")
            selected_ids = set(normalized_ids)

        moved: list[str] = []
        with self._lock:
            records = self._read_all()
            updated = dict(records)
            for attachment_id, record in records.items():
                if (
                    record.client_id != normalized_client_id
                    or record.scope_key != normalized_source
                    or record.submitted
                    or (selected_ids is not None and attachment_id not in selected_ids)
                ):
                    continue
                try:
                    record_cwd = str(pathlib.Path(record.cwd).expanduser().resolve())
                except (OSError, RuntimeError) as exc:
                    raise ValueError("Attachment workspace could not be resolved safely.") from exc
                if record_cwd != normalized_cwd:
                    raise ValueError("Attachment scope cannot be rebound across workspaces.")
                updated[attachment_id] = replace(
                    record,
                    scope_key=normalized_target,
                    cwd=normalized_cwd,
                )
                moved.append(attachment_id)
            if selected_ids is not None and set(moved) != selected_ids:
                raise ValueError("Attachment rebind set is incomplete.")
            if moved and normalized_source != normalized_target:
                # ``_write_all`` uses replace-on-commit, so either every
                # selected record moves or the old metadata remains intact.
                self._write_all(updated)
        return sorted(moved)

    def download(
        self,
        *,
        attachment_id: str,
        now: float | None = None,
    ) -> WebAttachmentDownload:
        normalized_id = self._require_id(attachment_id)
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            record = records.get(normalized_id)
            if record is None:
                raise KeyError(normalized_id)
            content = self._read_cache_record(record)
            touched = replace(record, last_accessed_at=timestamp)
            records[normalized_id] = touched
            self._write_all(records)
            return WebAttachmentDownload(record=touched, content=content)

    def attachment_id_for_path(self, local_path: str, *, now: float | None = None) -> str:
        raw_path = str(local_path or "").strip()
        if not raw_path:
            return ""
        normalized_path = self._normalized_path(raw_path)
        with self._lock:
            timestamp = time.time() if now is None else float(now)
            records = self._cleanup_locked(self._read_all(), now=timestamp)
            for record in records.values():
                if not record.submitted:
                    continue
                if self._normalized_path(record.local_path) == normalized_path:
                    return record.attachment_id
            return ""

    def _path(self) -> pathlib.Path:
        return self._data_dir / "web_attachments.json"

    def _cache_dir(self) -> pathlib.Path:
        return self._data_dir / _CACHE_DIRNAME

    def _cache_path(self, cache_name: str) -> pathlib.Path:
        normalized = self._safe_cache_name(cache_name)
        return self._cache_dir() / normalized

    def _read_all(self) -> dict[str, WebAttachmentRecord]:
        path = self._path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid web_attachments.json: {exc}") from exc
        if self._is_retired_v1_index(raw):
            # Version 1 pointed at workspace files and did not record the
            # digest or filesystem identity required by the private-cache
            # contract.  Those records cannot be upgraded honestly.  Retire
            # only the Focus index; never delete or trust the referenced
            # workspace files.  A later projection may safely observe and
            # copy a still-existing file into the v2 cache again.
            self._clear_retired_v1_cache()
            self._write_all({})
            return {}
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            raise RuntimeError("invalid web_attachments.json schema")
        entries = raw.get("attachments")
        if not isinstance(entries, dict):
            raise RuntimeError("invalid web_attachments.json attachments")
        records: dict[str, WebAttachmentRecord] = {}
        for attachment_id, value in entries.items():
            if not isinstance(value, dict):
                raise RuntimeError("invalid web attachment entry")
            try:
                cache_name = self._safe_cache_name(value.get("cache_name"))
                record = WebAttachmentRecord(
                    attachment_id=self._require_id(str(attachment_id)),
                    client_id=self._require_text(value.get("client_id"), "client_id", max_length=128),
                    scope_key=self._require_text(value.get("scope_key"), "scope_key", max_length=4096),
                    cwd=self._require_text(value.get("cwd"), "cwd", max_length=4096),
                    display_name=self._safe_name(str(value.get("display_name", "") or "")),
                    media_type=str(
                        value.get("media_type", "application/octet-stream")
                        or "application/octet-stream"
                    ),
                    size=max(int(value.get("size") or 0), 0),
                    local_path=str(self._cache_path(cache_name)),
                    cache_name=cache_name,
                    content_sha256=self._require_digest(value.get("content_sha256")),
                    file_device=int(value.get("file_device") or 0),
                    file_inode=int(value.get("file_inode") or 0),
                    created_at=max(float(value.get("created_at") or 0.0), 0.0),
                    last_accessed_at=max(float(value.get("last_accessed_at") or 0.0), 0.0),
                    expires_at=max(float(value.get("expires_at") or 0.0), 0.0),
                    submitted=bool(value.get("submitted", False)),
                    source_path=str(value.get("source_path", "") or ""),
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid web attachment entry") from exc
            records[record.attachment_id] = record
        return records

    @staticmethod
    def _is_retired_v1_index(raw: object) -> bool:
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != _RETIRED_SCHEMA_VERSION
            or set(raw) != {"schema_version", "attachments"}
        ):
            return False
        entries = raw.get("attachments")
        if not isinstance(entries, dict):
            return False
        for attachment_id, value in entries.items():
            if not isinstance(attachment_id, str) or not isinstance(value, dict):
                return False
            try:
                if str(uuid.UUID(attachment_id)) != attachment_id:
                    return False
            except ValueError:
                return False
            if set(value) != _RETIRED_V1_ENTRY_KEYS:
                return False
        return True

    def _write_all(self, records: dict[str, WebAttachmentRecord]) -> None:
        path = self._path()
        if not records:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "attachments": {
                attachment_id: {
                    key: value
                    for key, value in asdict(record).items()
                    if key not in {"attachment_id", "local_path"}
                }
                for attachment_id, record in sorted(records.items())
            },
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)

    def _cleanup_locked(
        self,
        records: dict[str, WebAttachmentRecord],
        *,
        now: float,
    ) -> dict[str, WebAttachmentRecord]:
        retained, retired_cache_names = self._cleanup_plan_locked(records, now=now)
        if retired_cache_names:
            # Metadata is the logical authority. Commit retirement before a
            # cache file is unlinked or entered into the process-local deferred
            # unlink set.
            self._write_all(retained)
            for cache_name in retired_cache_names:
                self._retire_cache_name_locked(cache_name)
        return retained

    def _cleanup_plan_locked(
        self,
        records: dict[str, WebAttachmentRecord],
        *,
        now: float,
    ) -> tuple[dict[str, WebAttachmentRecord], tuple[str, ...]]:
        retained: dict[str, WebAttachmentRecord] = {}
        retired_cache_names: list[str] = []
        for attachment_id, record in records.items():
            try:
                self._validate_cache_record(record)
            except (KeyError, ValueError):
                retired_cache_names.append(record.cache_name)
                continue
            if record.expires_at > now:
                retained[attachment_id] = record
                continue
            retired_cache_names.append(record.cache_name)

        submitted = sorted(
            (record for record in retained.values() if record.submitted),
            key=lambda record: (record.last_accessed_at, record.created_at, record.attachment_id),
        )
        retained_bytes = sum(record.size for record in submitted)
        retained_count = len(submitted)
        for record in submitted:
            if (
                retained_bytes <= self._retained_max_bytes
                and retained_count <= self._retained_max_count
            ):
                break
            retained.pop(record.attachment_id, None)
            retained_bytes -= record.size
            retained_count -= 1
            retired_cache_names.append(record.cache_name)
        return retained, tuple(retired_cache_names)

    def _require_submission_claim_locked(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt,
    ) -> _WebAttachmentSubmissionClaimState:
        if (
            not isinstance(receipt, WebAttachmentSubmissionClaimReceipt)
            or receipt._store_token is not self._submission_claim_store_token
        ):
            raise WebAttachmentSubmissionClaimError(
                "Web attachment submission claim belongs to another store or has an invalid type."
            )
        state = self._submission_claims.get(receipt._receipt_token)
        if state is None or state.receipt is not receipt:
            raise WebAttachmentSubmissionClaimError(
                "Web attachment submission claim was already released, forged, or is not exact."
            )
        return state

    def _release_submission_claim_locked(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt,
    ) -> None:
        state = self._require_submission_claim_locked(receipt)
        cache_names = tuple(record.cache_name for record in state.records)
        for cache_name in cache_names:
            claim_tokens = self._submission_claim_tokens_by_cache_name.get(cache_name)
            if claim_tokens is None or receipt._receipt_token not in claim_tokens:
                raise RuntimeError(
                    "Web attachment submission claim index is inconsistent."
                )

        del self._submission_claims[receipt._receipt_token]
        for cache_name in cache_names:
            claim_tokens = self._submission_claim_tokens_by_cache_name[cache_name]
            claim_tokens.remove(receipt._receipt_token)
            if claim_tokens:
                continue
            del self._submission_claim_tokens_by_cache_name[cache_name]
            if cache_name not in self._deferred_unlink_cache_names:
                continue
            self._deferred_unlink_cache_names.remove(cache_name)
            self._unlink_cache_name(cache_name)

    def _retire_cache_name_locked(self, cache_name: str) -> None:
        if self._submission_claim_tokens_by_cache_name.get(cache_name):
            self._deferred_unlink_cache_names.add(cache_name)
            return
        self._unlink_cache_name(cache_name)

    def _validate_submission_cache_record(self, record: WebAttachmentRecord) -> None:
        self._validate_cache_record(record)
        if self.is_native_input_media_type(record.media_type):
            self.validate_native_input(
                record.media_type,
                self._read_cache_record(record),
            )

    @staticmethod
    def _same_cache_identity(
        current: WebAttachmentRecord,
        claimed: WebAttachmentRecord,
    ) -> bool:
        return (
            current.attachment_id == claimed.attachment_id
            and current.cache_name == claimed.cache_name
            and current.content_sha256 == claimed.content_sha256
            and current.size == claimed.size
            and current.file_device == claimed.file_device
            and current.file_inode == claimed.file_inode
        )

    def _write_private_cache_file(self, cache_name: str, payload: bytes) -> os.stat_result:
        normalized = self._safe_cache_name(cache_name)
        cache_dir = self._cache_dir()
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "posix":
            path = self._cache_path(normalized)
            with path.open("xb") as handle:
                handle.write(payload)
                handle.flush()
            os.chmod(path, 0o600)
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                path.unlink(missing_ok=True)
                raise ValueError("Attachment cache entry is not a regular file.")
            return metadata

        cache_fd = self._open_cache_dir_fd()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(normalized, flags, 0o600, dir_fd=cache_fd)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write to attachment cache")
                view = view[written:]
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Attachment cache entry is not a regular file.")
            return metadata
        except Exception:
            try:
                os.unlink(normalized, dir_fd=cache_fd)
            except OSError:
                pass
            raise
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(cache_fd)

    def _read_cache_record(self, record: WebAttachmentRecord) -> bytes:
        fd = self._open_cache_record_fd(record)
        try:
            chunks: list[bytes] = []
            remaining = record.size
            while remaining > 0:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Attachment cache entry was truncated.")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise ValueError("Attachment cache entry changed size.")
            payload = b"".join(chunks)
            if hashlib.sha256(payload).hexdigest() != record.content_sha256:
                raise ValueError("Attachment cache entry changed content.")
            return payload
        finally:
            os.close(fd)

    def _validate_cache_record(self, record: WebAttachmentRecord) -> None:
        fd = self._open_cache_record_fd(record)
        os.close(fd)

    def _open_cache_record_fd(self, record: WebAttachmentRecord) -> int:
        normalized = self._safe_cache_name(record.cache_name)
        if os.name != "posix":
            path = self._cache_path(normalized)
            if path.is_symlink():
                raise ValueError("Attachment cache entry is a symbolic link.")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        else:
            cache_fd = self._open_cache_dir_fd()
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(normalized, flags, dir_fd=cache_fd)
            except FileNotFoundError as exc:
                raise KeyError(record.attachment_id) from exc
            except OSError as exc:
                raise ValueError("Attachment cache entry could not be opened safely.") from exc
            finally:
                os.close(cache_fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise ValueError("Attachment cache entry is not a regular file.")
        if int(metadata.st_size) != record.size:
            os.close(fd)
            raise ValueError("Attachment cache entry changed size.")
        if record.file_device and int(metadata.st_dev) != record.file_device:
            os.close(fd)
            raise ValueError("Attachment cache entry changed identity.")
        if record.file_inode and int(metadata.st_ino) != record.file_inode:
            os.close(fd)
            raise ValueError("Attachment cache entry changed identity.")
        return fd

    def _open_cache_dir_fd(self) -> int:
        self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        data_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        data_fd = os.open(self._data_dir, data_flags)
        try:
            try:
                os.mkdir(_CACHE_DIRNAME, 0o700, dir_fd=data_fd)
            except FileExistsError:
                pass
            cache_flags = data_flags | getattr(os, "O_NOFOLLOW", 0)
            try:
                cache_fd = os.open(_CACHE_DIRNAME, cache_flags, dir_fd=data_fd)
            except OSError as exc:
                raise ValueError("Attachment cache is not a private directory.") from exc
            metadata = os.fstat(cache_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(cache_fd)
                raise ValueError("Attachment cache is not a directory.")
            return cache_fd
        finally:
            os.close(data_fd)

    def _clear_retired_v1_cache(self) -> None:
        """Remove only private-cache entries which schema v1 never owned."""

        cache_dir = self._cache_dir()
        if not cache_dir.exists():
            return
        if os.name != "posix":
            for child in cache_dir.iterdir():
                if child.is_dir() and not child.is_symlink():
                    raise RuntimeError("invalid retired web attachment cache entry")
                child.unlink()
            return

        cache_fd = self._open_cache_dir_fd()
        try:
            for raw_name in os.listdir(cache_fd):
                name = self._safe_cache_name(raw_name)
                metadata = os.stat(name, dir_fd=cache_fd, follow_symlinks=False)
                if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                    raise RuntimeError("invalid retired web attachment cache entry")
                os.unlink(name, dir_fd=cache_fd)
        finally:
            os.close(cache_fd)

    def _read_workspace_file(
        self,
        *,
        workspace: pathlib.Path,
        local_path: str,
    ) -> tuple[bytes, str, str]:
        candidate = pathlib.Path(local_path).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(workspace)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError("Observed media must remain inside the thread workspace.") from exc
        if not relative.parts:
            raise ValueError("Observed media must be a file inside the thread workspace.")

        if os.name != "posix":
            if resolved.is_symlink() or not resolved.is_file():
                raise ValueError("Observed media must be a regular file.")
            payload = resolved.read_bytes()
            if not payload or len(payload) > self._max_bytes:
                raise ValueError("Observed media file is empty or exceeds the preview limit.")
            return payload, resolved.name, str(resolved)

        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        current_fd = os.open(workspace, directory_flags)
        try:
            for part in relative.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(relative.parts[-1], file_flags, dir_fd=current_fd)
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Observed media must be a regular file.")
                size = int(before.st_size)
                if size <= 0 or size > self._max_bytes:
                    raise ValueError("Observed media file is empty or exceeds the preview limit.")
                chunks: list[bytes] = []
                remaining = size
                while remaining > 0:
                    chunk = os.read(file_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Observed media changed while it was copied.")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if os.read(file_fd, 1):
                    raise ValueError("Observed media changed while it was copied.")
                after = os.fstat(file_fd)
                if (
                    after.st_dev != before.st_dev
                    or after.st_ino != before.st_ino
                    or after.st_size != before.st_size
                    or after.st_mtime_ns != before.st_mtime_ns
                ):
                    raise ValueError("Observed media changed while it was copied.")
                return b"".join(chunks), resolved.name, str(resolved)
            finally:
                os.close(file_fd)
        finally:
            os.close(current_fd)

    def _unlink_cache_name(self, cache_name: str) -> None:
        try:
            normalized = self._safe_cache_name(cache_name)
        except ValueError:
            return
        if os.name != "posix":
            try:
                self._cache_path(normalized).unlink()
            except OSError:
                pass
            return
        try:
            cache_fd = self._open_cache_dir_fd()
        except OSError:
            return
        try:
            try:
                os.unlink(normalized, dir_fd=cache_fd)
            except OSError:
                pass
        finally:
            os.close(cache_fd)

    @staticmethod
    def _require_text(value: object, field: str, *, max_length: int) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > max_length:
            raise ValueError(f"invalid {field}")
        return normalized

    @staticmethod
    def _require_id(value: str) -> str:
        normalized = str(value or "").strip()
        try:
            return str(uuid.UUID(normalized))
        except ValueError as exc:
            raise ValueError("invalid attachment id") from exc

    @staticmethod
    def _require_digest(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("invalid attachment digest")
        return normalized

    @staticmethod
    def _safe_name(value: str) -> str:
        name = pathlib.Path(str(value or "attachment").replace("\x00", "")).name
        name = re.sub(r"[\\/]+", "_", name)
        name = "".join(character if character.isprintable() else "_" for character in name)
        name = re.sub(r"\s+", "_", name).strip("._")
        if not name:
            name = "attachment"
        return name[:160]

    @staticmethod
    def _safe_cache_name(value: object) -> str:
        normalized = str(value or "").strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or pathlib.PurePath(normalized).name != normalized
            or "/" in normalized
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise ValueError("invalid attachment cache name")
        return normalized

    @staticmethod
    def _normalized_path(value: str) -> str:
        return str(pathlib.Path(str(value or "")).expanduser().resolve())

    @staticmethod
    def _normalized_media_type(value: object) -> str:
        return normalize_native_media_type(value)

    @classmethod
    def is_native_input_media_type(cls, value: object) -> bool:
        return is_native_input_media_type(value)

    @classmethod
    def validate_native_input(cls, media_type: object, content: bytes) -> str:
        """Return a canonical type after verifying bytes used as native Codex media.

        Generic files deliberately retain their declared type and are delivered through
        the managed file manifest.  Types that would become native image/audio data
        URLs must instead be one of the explicit, signature-checked formats below.
        """

        return validate_declared_native_media(media_type, content)

    @staticmethod
    def _detected_native_media_type(content: bytes) -> str:
        return detect_native_media_type(content)

    @classmethod
    def _safe_preview_media_type(cls, path: pathlib.Path) -> str:
        media_type = cls._normalized_media_type(mimetypes.guess_type(path.name)[0])
        # Observed media is copied solely for Focus Web's controlled image
        # renderer.  Audio/video are ordinary agent files, not browser media,
        # and generic workspace files never acquire an authenticated URL.
        return media_type if is_safe_image_media_type(media_type) else ""
