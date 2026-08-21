"""Detached projection for one RuntimeLoop-frozen Web notification.

This module owns no runtime fact.  The event coordinator freezes one bounded
receipt after applying the notification to the read model; turn projection and
attachment-cache materialization then run on a service-ingress worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from bot.web_runtime.projection import (
    project_goal_payload,
    project_subagent_tasks,
    project_turns,
)
from bot.web_runtime.thread_read_model import (
    WebThreadNotificationUpdate,
    WebThreadReadObservationReceipt,
)


@dataclass(frozen=True, slots=True)
class WebNotificationProjectionReceipt:
    """Detached inputs and exact fences for one notification projection."""

    sequence: int
    method: str
    thread_id: str
    runtime_epoch: str
    observation: WebThreadReadObservationReceipt
    update: WebThreadNotificationUpdate
    cwd: str = ""
    collaboration_turns: tuple[dict[str, Any], ...] = field(
        default=(),
        repr=False,
        compare=False,
    )


def project_notification(
    receipt: WebNotificationProjectionReceipt,
    *,
    attachment_url_for_path: Callable[[str], str],
    attachment_url_for_id: Callable[[str], str],
) -> dict[str, Any]:
    """Materialize one detached thread delta without reading runtime state."""

    update = receipt.update
    detail = dict(update.detail)
    if update.goal_changed:
        detail["goal"] = project_goal_payload(update.goal)
    if update.raw_turn is None:
        return detail
    projected = project_turns(
        [update.raw_turn],
        attachment_url_for_path=attachment_url_for_path,
        attachment_url_for_id=attachment_url_for_id,
    )
    turn_id = str(detail.get("turn_id", "") or "").strip()
    detail.update(
        {
            "turns": projected,
            "tasks": project_subagent_tasks(receipt.collaboration_turns),
            "active_turn_id": turn_id
            if str(update.raw_turn.get("status", "") or "") == "inProgress"
            else "",
            "active_turn_status": str(
                update.raw_turn.get("status", "") or ""
            ),
        }
    )
    return detail
