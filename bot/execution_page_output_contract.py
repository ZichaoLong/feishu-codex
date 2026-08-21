"""Typed boundary for opening an execution presentation page."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bot.binding_runtime_contract import BindingSessionSnapshot


class InitialExecutionPageOpenStatus(StrEnum):
    ACTIVE = "active"
    REJECTED = "rejected"
    SEND_UNKNOWN = "send_unknown"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class InitialExecutionPageOpenResult:
    status: InitialExecutionPageOpenStatus
    session: BindingSessionSnapshot | None
    message_id: str = ""

    def __post_init__(self) -> None:
        if type(self.status) is not InitialExecutionPageOpenStatus:
            raise TypeError("initial execution page result requires a typed status")
        if self.session is not None and type(self.session) is not BindingSessionSnapshot:
            raise TypeError("initial execution page result session must be exact or None")
        if type(self.message_id) is not str:
            raise TypeError("initial execution page result message_id must be a string")
        normalized_message_id = self.message_id.strip()
        object.__setattr__(self, "message_id", normalized_message_id)
        if self.status is not InitialExecutionPageOpenStatus.STALE and self.session is None:
            raise ValueError("a settled initial execution page result requires session")
        if self.status is InitialExecutionPageOpenStatus.ACTIVE and not normalized_message_id:
            raise ValueError("an active initial execution page requires message_id")
        if self.status is InitialExecutionPageOpenStatus.REJECTED and normalized_message_id:
            raise ValueError("a rejected initial execution page cannot have message_id")

    @property
    def active(self) -> bool:
        return self.status is InitialExecutionPageOpenStatus.ACTIVE

    @property
    def send_unknown(self) -> bool:
        return self.status is InitialExecutionPageOpenStatus.SEND_UNKNOWN
