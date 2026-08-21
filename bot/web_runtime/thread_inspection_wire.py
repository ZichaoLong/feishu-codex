"""Exact JSON byte encoding for Focus Web inspection responses."""

from __future__ import annotations

import json
from typing import Any


def encode_thread_inspection_json(payload: Any) -> bytes:
    """Encode response bytes, including those counted by bounded-view admission."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
