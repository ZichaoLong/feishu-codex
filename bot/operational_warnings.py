"""Bounded, thread-safe operator warning projection.

Logs remain the detailed forensic record.  This registry keeps a small
in-memory projection for status/control surfaces so important liveness signals
are visible without shell access and without becoming durable domain facts.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Callable

from bot.runtime_loop import RuntimeTaskObservation


DEFAULT_OPERATIONAL_WARNING_TTL_SECONDS = 300.0
_WARNING_SEVERITIES = frozenset({"warning", "error"})
_WARNING_ATTENTION_LEVELS = frozenset({"advisory", "correctness"})


@dataclass(frozen=True, slots=True)
class OperationalWarning:
    code: str
    source: str
    message: str
    severity: str
    attention: str
    first_seen_at: float
    last_seen_at: float
    occurrences: int
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "source": self.source,
            "message": self.message,
            "severity": self.severity,
            "attention": self.attention,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "occurrences": self.occurrences,
            "details": dict(self.details),
        }


class OperationalWarningRegistry:
    """Keep only currently relevant warning families.

    A warning remains current for ``ttl_seconds`` after its latest observation.
    Repetition within that window coalesces into the same family and extends
    the window.  Once the window expires, both ``record`` and ``snapshot``
    discard the old family so a healthy process returns to an empty warning
    set without a restart.
    """

    def __init__(
        self,
        *,
        limit: int = 64,
        ttl_seconds: float = DEFAULT_OPERATIONAL_WARNING_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if int(limit) <= 0:
            raise ValueError("operational warning limit must be positive")
        if float(ttl_seconds) <= 0:
            raise ValueError("operational warning TTL must be positive")
        self._limit = int(limit)
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._warnings: OrderedDict[tuple[str, str, str], OperationalWarning] = (
            OrderedDict()
        )

    def record(
        self,
        *,
        code: str,
        source: str,
        message: str,
        severity: str = "warning",
        attention: str = "correctness",
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized_code = str(code or "").strip()
        normalized_source = str(source or "").strip()
        normalized_message = str(message or "").strip()
        normalized_severity = str(severity or "warning").strip().lower()
        normalized_attention = str(attention or "correctness").strip().lower()
        if not normalized_code or not normalized_source or not normalized_message:
            raise ValueError("operator warning code, source, and message are required")
        if normalized_severity not in _WARNING_SEVERITIES:
            raise ValueError("operator warning severity must be warning or error")
        if normalized_attention not in _WARNING_ATTENTION_LEVELS:
            raise ValueError(
                "operator warning attention must be advisory or correctness"
            )
        key = (normalized_code, normalized_source, normalized_message)
        with self._lock:
            # Read time under the same lock as the mutation.  Concurrent
            # reporters therefore cannot overwrite a newer observation with
            # an older timestamp obtained before they entered the registry.
            now = float(self._clock())
            self._discard_expired_locked(now)
            existing = self._warnings.pop(key, None)
            if existing is None:
                warning = OperationalWarning(
                    code=normalized_code,
                    source=normalized_source,
                    message=normalized_message,
                    severity=normalized_severity,
                    attention=normalized_attention,
                    first_seen_at=now,
                    last_seen_at=now,
                    occurrences=1,
                    details=dict(details or {}),
                )
            else:
                warning = replace(
                    existing,
                    severity=(
                        "error"
                        if "error" in {existing.severity, normalized_severity}
                        else "warning"
                    ),
                    attention=(
                        "correctness"
                        if "correctness" in {existing.attention, normalized_attention}
                        else "advisory"
                    ),
                    last_seen_at=now,
                    occurrences=existing.occurrences + 1,
                    details=dict(details or {}),
                )
            self._warnings[key] = warning
            while len(self._warnings) > self._limit:
                self._warnings.popitem(last=False)

    def snapshot(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        requested = self._limit if limit is None else max(int(limit), 0)
        with self._lock:
            now = float(self._clock())
            self._discard_expired_locked(now)
            warnings = list(reversed(self._warnings.values()))[:requested]
        return [warning.to_dict() for warning in warnings]

    def _discard_expired_locked(self, now: float) -> None:
        expired_keys = [
            key
            for key, warning in self._warnings.items()
            if now >= warning.last_seen_at + self._ttl_seconds
        ]
        for key in expired_keys:
            self._warnings.pop(key, None)


@dataclass(frozen=True, slots=True)
class FocusRuntimeTaskObserver:
    """Project RuntimeLoop latency evidence into the warning registry."""

    warnings: OperationalWarningRegistry
    slow_queue_seconds: float
    slow_task_seconds: float

    def __call__(self, observation: RuntimeTaskObservation) -> None:
        if observation.queue_age_seconds >= self.slow_queue_seconds:
            self.warnings.record(
                code="runtime_queue_delay",
                source="RuntimeLoop",
                message="RuntimeLoop task queue delay exceeded its threshold.",
                attention="advisory",
                details={
                    "waiting_task": observation.task_name,
                    "queue_depth_at_enqueue": observation.queue_depth_at_enqueue,
                    "active_task_at_enqueue": observation.active_task_at_enqueue,
                    "active_task_age_seconds_at_enqueue": round(
                        observation.active_task_age_seconds_at_enqueue,
                        3,
                    ),
                    "queue_age_seconds": round(observation.queue_age_seconds, 3),
                    "threshold_seconds": self.slow_queue_seconds,
                },
            )
        if observation.task_duration_seconds >= self.slow_task_seconds:
            self.warnings.record(
                code="runtime_task_slow",
                source="RuntimeLoop",
                message="RuntimeLoop task duration exceeded its threshold.",
                attention="advisory",
                details={
                    "running_task": observation.task_name,
                    "queue_depth_at_enqueue": observation.queue_depth_at_enqueue,
                    "active_task_at_enqueue": observation.active_task_at_enqueue,
                    "active_task_age_seconds_at_enqueue": round(
                        observation.active_task_age_seconds_at_enqueue,
                        3,
                    ),
                    "queue_age_seconds": round(observation.queue_age_seconds, 3),
                    "task_duration_seconds": round(
                        observation.task_duration_seconds,
                        3,
                    ),
                    "threshold_seconds": self.slow_task_seconds,
                },
            )
