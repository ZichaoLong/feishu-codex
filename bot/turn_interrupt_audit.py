"""Best-effort, redacted audit projection for ordinary turn interrupts.

This module owns no interrupt authority or lifecycle fact. Callers record an
attempt only after their surface-specific admission has selected an exact
thread and an exact-or-empty current/startup target, immediately before
crossing the last local effect boundary.
"""

from __future__ import annotations

import hashlib
import logging
from enum import Enum


logger = logging.getLogger(__name__)

_REFERENCE_DIGEST_LENGTH = 16
_INVALID_INTERNAL_SOURCE = "invalid_internal_source"


class TurnInterruptSource(str, Enum):
    """Trusted local surface which reached an interrupt effect boundary."""

    WEB_DOCUMENT = "web_document"
    FEISHU_BINDING = "feishu_binding"
    FCODEX_ENDPOINT = "fcodex_endpoint"


def _short_reference(value: object) -> str:
    """Return a stable redacted reference without rendering ``value``."""

    if not isinstance(value, str):
        return "invalid"
    try:
        encoded = value.encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(encoded).hexdigest()[:_REFERENCE_DIGEST_LENGTH]
    except Exception:
        return "invalid"


def record_turn_interrupt_dispatch_attempt(
    *,
    source: TurnInterruptSource,
    thread_id: str,
    turn_id: str,
) -> None:
    """Project one redacted exact-thread/current-or-startup-target attempt.

    ``source`` is deliberately absent from every external wire.  A programming
    error cannot turn an arbitrary string into a trusted source, and logging
    failure must never become interrupt admission or settlement authority.
    """

    try:
        source_value = (
            source.value
            if isinstance(source, TurnInterruptSource)
            else _INVALID_INTERNAL_SOURCE
        )
        logger.info(
            "turn_interrupt_dispatch_attempt "
            "phase=attempt source=%s thread_ref=%s turn_ref=%s",
            source_value,
            _short_reference(thread_id),
            _short_reference(turn_id),
        )
    except Exception:
        # Telemetry is intentionally best-effort.  In particular, a broken log
        # sink cannot decide whether a user's exact interrupt is dispatched.
        return
