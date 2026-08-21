"""Typed proofs for authoritative Feishu destination loss."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class FeishuDestinationLossProofType(str, Enum):
    CHAT_DISBANDED_EVENT = "im.chat.disbanded_v1"
    BOT_REMOVED_EVENT = "im.chat.member.bot.deleted_v1"
    OUTBOUND_BOT_OUTSIDE_CHAT = "outbound:230002"
    OUTBOUND_CHAT_DISSOLVED = "outbound:232009"


class FeishuDestinationLossState(str, Enum):
    PENDING = "pending"
    SETTLED = "settled"


def _required_text(value: object, *, field: str, maximum: int = 1024) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} is too long")
    return normalized


@dataclass(frozen=True, slots=True)
class FeishuDestinationLossProof:
    """One positive Feishu proof that a chat delivery destination is gone."""

    source_id: str
    chat_id: str
    proof_type: FeishuDestinationLossProofType

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_id",
            _required_text(self.source_id, field="source_id"),
        )
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, field="chat_id"),
        )
        if type(self.proof_type) is not FeishuDestinationLossProofType:
            raise TypeError("proof_type must be FeishuDestinationLossProofType")

    @property
    def proof_id(self) -> str:
        """Return the collision-free ledger identity for this source fact."""

        return f"{self.proof_type.value}:{self.source_id}"


@dataclass(frozen=True, slots=True)
class FeishuDestinationLossRecord:
    """Durable acceptance and settlement state for one exact proof."""

    proof: FeishuDestinationLossProof
    state: FeishuDestinationLossState
    accepted_at: float
    settled_at: float | None = None

    def __post_init__(self) -> None:
        if type(self.proof) is not FeishuDestinationLossProof:
            raise TypeError("proof must be FeishuDestinationLossProof")
        if type(self.state) is not FeishuDestinationLossState:
            raise TypeError("state must be FeishuDestinationLossState")
        accepted_at = float(self.accepted_at)
        if not math.isfinite(accepted_at) or accepted_at <= 0:
            raise ValueError("accepted_at must be a positive finite timestamp")
        object.__setattr__(self, "accepted_at", accepted_at)
        settled_at = self.settled_at
        if self.state is FeishuDestinationLossState.PENDING:
            if settled_at is not None:
                raise ValueError(
                    "pending destination-loss proof cannot have settled_at"
                )
            return
        if settled_at is None:
            raise ValueError("settled destination-loss proof requires settled_at")
        normalized_settled_at = float(settled_at)
        if (
            not math.isfinite(normalized_settled_at)
            or normalized_settled_at < accepted_at
        ):
            raise ValueError("settled_at must be finite and not precede accepted_at")
        object.__setattr__(self, "settled_at", normalized_settled_at)
