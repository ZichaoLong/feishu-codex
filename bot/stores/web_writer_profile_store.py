"""Persistent per-browser navigation owned by one Focus instance."""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

_SCHEMA_VERSION = 3
_MIN_SCOPE_GENERATION = 1
_PROFILE_FIELDS = {
    "selected_thread_id",
    "working_dir",
    "scope_generation",
    "updated_at",
}


@dataclass(frozen=True, slots=True)
class WebWriterProfile:
    client_id: str
    selected_thread_id: str = ""
    working_dir: str = ""
    # Monotonic browser-scope generation used only for stale upload
    # admission.  It is deliberately independent from ``updated_at``.
    scope_generation: int = _MIN_SCOPE_GENERATION
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class WebWriterSelectionClearReceipt:
    """Durable before/after proof for one exact selection-to-draft change."""

    cleared_thread_id: str
    previous: WebWriterProfile
    current: WebWriterProfile


class WebWriterProfileStore:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def load(self, client_id: str) -> WebWriterProfile | None:
        normalized_client_id = self._normalize_client_id(client_id)
        with self._lock:
            return self._read_all().get(normalized_client_id)

    def save(self, profile: WebWriterProfile) -> WebWriterProfile:
        normalized = self._normalize_profile(profile)
        with self._lock:
            profiles = self._read_all()
            profiles[normalized.client_id] = normalized
            self._write_all(profiles)
        return normalized

    def update(self, client_id: str, **changes: Any) -> WebWriterProfile:
        normalized_client_id = self._normalize_client_id(client_id)
        with self._lock:
            profiles = self._read_all()
            current = profiles.get(normalized_client_id) or WebWriterProfile(
                client_id=normalized_client_id
            )
            updated = self._normalize_profile(
                replace(current, **changes, updated_at=time.time())
            )
            profiles[normalized_client_id] = updated
            self._write_all(profiles)
        return updated

    def update_if_matches(
        self,
        client_id: str,
        expected: WebWriterProfile | None,
        **changes: Any,
    ) -> WebWriterProfile | None:
        """Apply one exact-profile CAS, returning ``None`` on mismatch."""

        normalized_client_id = self._normalize_client_id(client_id)
        normalized_expected = (
            self._normalize_profile(expected) if expected is not None else None
        )
        if (
            normalized_expected is not None
            and normalized_expected.client_id != normalized_client_id
        ):
            raise ValueError("expected web writer profile has another client id")
        with self._lock:
            profiles = self._read_all()
            current = profiles.get(normalized_client_id)
            if current != normalized_expected:
                return None
            if not changes:
                return current
            base = current or WebWriterProfile(client_id=normalized_client_id)
            updated = self._normalize_profile(
                replace(base, **changes, updated_at=time.time())
            )
            profiles[normalized_client_id] = updated
            self._write_all(profiles)
        return updated

    def clear(self, client_id: str) -> None:
        normalized_client_id = self._normalize_client_id(client_id)
        with self._lock:
            profiles = self._read_all()
            if profiles.pop(normalized_client_id, None) is not None:
                self._write_all(profiles)

    def clear_selected_thread(
        self,
        thread_id: str,
    ) -> tuple[WebWriterSelectionClearReceipt, ...]:
        """Atomically move every exact matching selection into draft scope.

        A lifecycle event may be replayed after another selection has already
        replaced its target.  Only profiles whose current durable selection
        still equals ``thread_id`` are changed.  Their selection clear and
        single generation advance are persisted by the same file replace;
        mismatches and replays return no receipt and do not advance again.
        """

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ()
        receipts: list[WebWriterSelectionClearReceipt] = []
        with self._lock:
            profiles = self._read_all()
            updated_at = time.time()
            for client_id, profile in tuple(profiles.items()):
                if profile.selected_thread_id != normalized_thread_id:
                    continue
                updated = self._normalize_profile(
                    replace(
                        profile,
                        selected_thread_id="",
                        scope_generation=profile.scope_generation + 1,
                        updated_at=updated_at,
                    )
                )
                profiles[client_id] = updated
                receipts.append(
                    WebWriterSelectionClearReceipt(
                        cleared_thread_id=normalized_thread_id,
                        previous=profile,
                        current=updated,
                    )
                )
            if receipts:
                self._write_all(profiles)
        return tuple(sorted(receipts, key=lambda receipt: receipt.current.client_id))

    def _path(self) -> pathlib.Path:
        return self._data_dir / "web_writer_profiles.json"

    def _read_all(self) -> dict[str, WebWriterProfile]:
        path = self._path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid web_writer_profiles.json: {exc}") from exc
        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if type(schema_version) is not int or schema_version not in {
            1,
            2,
            _SCHEMA_VERSION,
        }:
            raise RuntimeError("invalid web_writer_profiles.json schema")
        raw_profiles = raw.get("profiles")
        if not isinstance(raw_profiles, dict):
            raise RuntimeError("invalid web_writer_profiles.json profiles")
        profiles: dict[str, WebWriterProfile] = {}
        for client_id, value in raw_profiles.items():
            if not isinstance(value, dict):
                raise RuntimeError("invalid web writer profile entry")
            if schema_version == _SCHEMA_VERSION and set(value) != _PROFILE_FIELDS:
                raise RuntimeError("invalid web writer profile entry")
            try:
                profile = self._normalize_profile(
                    WebWriterProfile(
                        client_id=str(client_id),
                        selected_thread_id=str(value.get("selected_thread_id", "") or ""),
                        working_dir=str(value.get("working_dir", "") or ""),
                        # Schema v1 profiles predate upload-scope CAS.  Legacy
                        # v1/v2 setting fields are deliberately ignored: they
                        # are neither migrated nor consulted as settings.
                        scope_generation=(
                            _MIN_SCOPE_GENERATION
                            if schema_version == 1
                            else self._require_scope_generation(
                                value["scope_generation"]
                            )
                        ),
                        updated_at=float(value.get("updated_at") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("invalid web writer profile entry") from exc
            profiles[profile.client_id] = profile
        if schema_version != _SCHEMA_VERSION:
            # Canonicalize legacy navigation immediately so the retired four
            # per-client setting fields do not remain as a competing durable
            # shape.  Their values are intentionally discarded, never used to
            # seed the instance-wide Web settings owner.
            self._write_all(profiles)
        return profiles

    def _write_all(self, profiles: dict[str, WebWriterProfile]) -> None:
        path = self._path()
        if not profiles:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "profiles": {
                client_id: {
                    key: value
                    for key, value in asdict(profile).items()
                    if key != "client_id"
                }
                for client_id, profile in sorted(profiles.items())
            },
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _normalize_client_id(client_id: str) -> str:
        normalized = str(client_id or "").strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("invalid web client id")
        return normalized

    @classmethod
    def _normalize_profile(cls, profile: WebWriterProfile) -> WebWriterProfile:
        return WebWriterProfile(
            client_id=cls._normalize_client_id(profile.client_id),
            selected_thread_id=str(profile.selected_thread_id or "").strip(),
            working_dir=str(profile.working_dir or "").strip(),
            scope_generation=cls._require_scope_generation(profile.scope_generation),
            updated_at=max(float(profile.updated_at), 0.0),
        )

    @staticmethod
    def _require_scope_generation(value: object) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("scope_generation must be an exact positive integer")
        return value
