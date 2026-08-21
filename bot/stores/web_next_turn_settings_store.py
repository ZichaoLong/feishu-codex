"""Durable instance-wide settings for the next eligible Focus Web turn.

The validated instance config is a service-start seed only while no durable
record exists.  The first explicit Web mutation creates the record; subsequent
reads and partial updates use that single record across every browser and
thread in the Focus instance.
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from bot.approval_policy import normalize_approval_policy
from bot.permissions_profile import normalize_permissions_profile_id
from bot.stores.versioned_records import (
    VersionedRecordsUnavailable,
    read_versioned_records,
    write_versioned_records,
)


_SCHEMA_VERSION = 1
_INITIAL_GENERATION = 1
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_RECORD_KEY = "settings"


@dataclass(frozen=True, slots=True)
class WebNextTurnSettings:
    """One immutable instance-wide Web settings snapshot."""

    approval_policy: str
    permissions_profile_id: str
    model: str = ""
    reasoning_effort: str = ""
    generation: int = _INITIAL_GENERATION


class WebNextTurnSettingsStore:
    """Own one atomic settings record without browser or thread partitioning."""

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        initial: WebNextTurnSettings,
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._initial = self._normalize(initial)
        self._lock = threading.Lock()

    def load(self) -> WebNextTurnSettings:
        """Read persisted authority or the non-materialized service-start seed."""

        with self._lock:
            return self._read() or self._initial

    def update(
        self,
        changes: Mapping[str, Any],
        *,
        validate_merged: Callable[[WebNextTurnSettings], None] | None = None,
    ) -> WebNextTurnSettings:
        allowed = {
            "approval_policy",
            "permissions_profile_id",
            "model",
            "reasoning_effort",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ValueError(
                "unsupported Web next-turn setting fields: " + ", ".join(unknown)
            )
        if not changes:
            return self.load()
        with self._lock:
            current = self._read() or self._initial
            updated = self._normalize(
                replace(
                    current,
                    **changes,
                    generation=current.generation + 1,
                )
            )
            if validate_merged is not None:
                validate_merged(updated)
            self._write(updated)
            return updated

    def _path(self) -> pathlib.Path:
        return self._data_dir / "web_next_turn_settings.json"

    def _read(self) -> WebNextTurnSettings | None:
        path = self._path()
        if not path.exists():
            return None
        try:
            records = read_versioned_records(
                path,
                schema_version=_SCHEMA_VERSION,
                accept_unversioned=False,
            )
            if set(records) != {_RECORD_KEY}:
                raise ValueError("invalid record inventory")
            value = records[_RECORD_KEY]
            if not isinstance(value, dict):
                raise ValueError("invalid settings")
            required = {
                "approval_policy",
                "permissions_profile_id",
                "model",
                "reasoning_effort",
                "generation",
            }
            if set(value) != required:
                raise ValueError("invalid setting fields")
            loaded = WebNextTurnSettings(**value)
            normalized = self._normalize(loaded)
            if normalized != loaded:
                raise ValueError("non-canonical setting fields")
            return normalized
        except (TypeError, ValueError, VersionedRecordsUnavailable) as exc:
            raise RuntimeError(f"invalid web_next_turn_settings.json: {exc}") from exc

    def _write(self, settings: WebNextTurnSettings) -> None:
        write_versioned_records(
            self._path(),
            {_RECORD_KEY: asdict(settings)},
            schema_version=_SCHEMA_VERSION,
        )

    @staticmethod
    def _normalize(value: WebNextTurnSettings) -> WebNextTurnSettings:
        if (
            type(value.generation) is not int
            or value.generation <= 0
            or value.generation > _MAX_SAFE_INTEGER
        ):
            raise ValueError("generation must be an exact positive safe integer")
        for name in (
            "approval_policy",
            "permissions_profile_id",
            "model",
            "reasoning_effort",
        ):
            if not isinstance(getattr(value, name), str):
                raise ValueError(f"{name} must be a string")
        approval_policy = value.approval_policy.strip().lower()
        permissions_profile_id = value.permissions_profile_id.strip().lower()
        if not approval_policy:
            raise ValueError("approval_policy must not be empty")
        if not permissions_profile_id:
            raise ValueError("permissions_profile_id must not be empty")
        return WebNextTurnSettings(
            approval_policy=normalize_approval_policy(approval_policy),
            permissions_profile_id=normalize_permissions_profile_id(
                permissions_profile_id
            ),
            model=value.model.strip(),
            reasoning_effort=value.reasoning_effort.strip().lower(),
            generation=value.generation,
        )
