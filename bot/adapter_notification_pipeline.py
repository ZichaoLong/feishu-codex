"""Ordered app-server notification fan-out.

Notification ordering is a runtime contract: later consumers may depend on
facts projected by earlier consumers.  Keep that contract here instead of as
an incidental sequence of calls inside the composition root.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

NotificationHandler = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class NotificationStage:
    name: str
    handle: NotificationHandler


class AdapterNotificationPipeline:
    """Dispatch one notification through the canonical ordered stages."""

    STAGE_ORDER = (
        "effective_settings_facts",
        "active_turn_owner",
        "server_requests",
        "web_runtime",
        "operation_owner",
        # Reconcile the admission before Feishu projection may drain FIFO and
        # start another turn; an old terminal event must not capture that turn.
        "feishu_root_operation",
        "feishu_projection",
    )

    def __init__(
        self,
        *,
        stages: Mapping[str, NotificationHandler],
        assert_runtime_context: Callable[[], None],
    ) -> None:
        supplied = set(stages)
        required = set(self.STAGE_ORDER)
        missing = sorted(required - supplied)
        unknown = sorted(supplied - required)
        if missing or unknown:
            details: list[str] = []
            if missing:
                details.append(f"missing stages: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown stages: {', '.join(unknown)}")
            raise ValueError("invalid notification pipeline: " + "; ".join(details))
        self._stages = tuple(
            NotificationStage(name=name, handle=stages[name])
            for name in self.STAGE_ORDER
        )
        self._assert_runtime_context = assert_runtime_context

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self._stages)

    def dispatch(self, method: str, params: dict[str, Any]) -> None:
        """Apply the complete fan-out, stopping at the first failed stage."""

        self._assert_runtime_context()
        for stage in self._stages:
            stage.handle(method, params)
