"""Typed outcome shared by Feishu queued upstream-start transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


FeishuStartDisposition: TypeAlias = Literal[
    "started",
    "known_no_effect_settled",
    "blocked_unsettled",
]


@dataclass(frozen=True, slots=True)
class PromptTurnStartResult:
    started: bool
    thread_id: str = ""
    turn_id: str = ""
    reason_code: str = ""
    reason_text: str = ""
    # Conservative by default: a caller may advance a queued successor only
    # when the transaction explicitly proves success or exact owner settlement.
    disposition: FeishuStartDisposition = "blocked_unsettled"


@dataclass(frozen=True, slots=True)
class FeishuOperationSettlement:
    owner_settled: bool
    disposition: FeishuStartDisposition
