"""Snapshot process-local server requests before backend replacement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


class BackendResetServerRequestOwner(Protocol):
    def pending_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class BackendResetInteractionReceipt:
    """Inventory later retired by the machine-stop epoch transition."""

    pending_request_count: int

    def to_dict(self) -> dict[str, int]:
        return {"pending": self.pending_request_count}


class BackendResetInteractionCoordinator:
    """Read the canonical pending-map size without pre-stop response effects."""

    def __init__(self, pending_count: Callable[[], int]) -> None:
        if not callable(pending_count):
            raise TypeError("backend reset pending-count port must be callable")
        self._pending_count = pending_count

    @classmethod
    def from_owner(
        cls,
        owner: BackendResetServerRequestOwner,
    ) -> BackendResetInteractionCoordinator:
        return cls(owner.pending_count)

    def pending_count(self) -> int:
        value = self._pending_count()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                "server-request pending_count returned an invalid value"
            )
        return value

    def prepare_all(self) -> BackendResetInteractionReceipt:
        """Capture diagnostics; machine stop performs the actual retirement."""

        return BackendResetInteractionReceipt(self.pending_count())
