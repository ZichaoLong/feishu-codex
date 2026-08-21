"""One strict envelope contract for local JSON record stores."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping

from bot.atomic_file import atomic_write_text


class VersionedRecordsUnavailable(RuntimeError):
    """A versioned records file cannot be interpreted without guessing."""

    def __init__(self, path: pathlib.Path, reason: str) -> None:
        self.path = pathlib.Path(path)
        self.reason = str(reason or "unavailable")
        super().__init__(self.reason)


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def read_versioned_records(
    path: pathlib.Path,
    *,
    schema_version: int,
    accept_unversioned: bool = True,
) -> dict[str, object]:
    """Read ``records`` without ever interpreting bad state as empty.

    The historical unversioned record mapping is an optional one-way input
    format.  The caller remains responsible for validating each record.
    """

    state_path = pathlib.Path(path)
    try:
        if not state_path.exists():
            return {}
        raw = json.loads(
            state_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except _DuplicateJsonKey as exc:
        raise VersionedRecordsUnavailable(state_path, "duplicate JSON key") from exc
    except Exception as exc:
        raise VersionedRecordsUnavailable(state_path, "invalid or unreadable JSON") from exc
    if not isinstance(raw, dict):
        raise VersionedRecordsUnavailable(state_path, "top level is not an object")
    if "schema_version" not in raw:
        if not accept_unversioned:
            raise VersionedRecordsUnavailable(state_path, "missing schema version")
        return raw
    version = raw.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != int(schema_version)
        or set(raw) != {"schema_version", "records"}
    ):
        raise VersionedRecordsUnavailable(state_path, "unsupported or invalid schema")
    records = raw.get("records")
    if not isinstance(records, dict):
        raise VersionedRecordsUnavailable(state_path, "records is not an object")
    return records


def write_versioned_records(
    path: pathlib.Path,
    records: Mapping[str, object],
    *,
    schema_version: int,
) -> None:
    """Atomically persist the canonical private records envelope."""

    payload = {
        "schema_version": int(schema_version),
        "records": {str(key): value for key, value in sorted(records.items())},
    }
    atomic_write_text(
        pathlib.Path(path),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        mode=0o600,
    )
