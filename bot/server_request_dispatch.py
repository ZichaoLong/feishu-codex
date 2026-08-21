"""Typed outcome boundary for routing one Codex server request to a surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ServerRequestDispatchOutcome = Literal[
    "committed",
    "known_not_committed",
    "outcome_unknown",
]
ServerRequestSurfaceClaimOutcome = Literal["claimed", "declined"]


class ServerRequestDispatchKnownNotCommitted(RuntimeError):
    """The dispatcher proved that no external or surface-local effect occurred."""


class ServerRequestSurfaceIdentityConflict(RuntimeError):
    """A surface retained a different capability under the same request key.

    The older surface record may already have produced an external effect, so
    the dispatcher must neither fall through to another surface nor classify
    the new callback as safe to retry.
    """


@dataclass(frozen=True, slots=True)
class ServerRequestSurfaceClaim:
    """Typed proof that one surface did or did not retain the request."""

    outcome: ServerRequestSurfaceClaimOutcome

    @classmethod
    def claimed(cls) -> ServerRequestSurfaceClaim:
        return cls("claimed")

    @classmethod
    def declined(cls) -> ServerRequestSurfaceClaim:
        return cls("declined")

    @classmethod
    def from_retained(cls, retained: object) -> ServerRequestSurfaceClaim:
        """Claim only after the surface reports a literal retained receipt."""

        return cls("claimed" if retained is True else "declined")


@dataclass(frozen=True, slots=True)
class ServerRequestDispatchReceipt:
    """Classification of one complete surface-routing attempt.

    A normal return is ``committed`` only after the selected surface has
    recorded its request owner. ``known_not_committed`` requires explicit
    proof that no effect occurred. Every unclassified exception is
    ``outcome_unknown`` and is never automatic-retry authority.
    """

    outcome: ServerRequestDispatchOutcome
    reason: str = ""

    @classmethod
    def committed(cls) -> ServerRequestDispatchReceipt:
        return cls("committed")

    @classmethod
    def known_not_committed(
        cls,
        reason: str = "",
    ) -> ServerRequestDispatchReceipt:
        return cls("known_not_committed", str(reason or "").strip())

    @classmethod
    def outcome_unknown(
        cls,
        reason: str = "",
    ) -> ServerRequestDispatchReceipt:
        return cls("outcome_unknown", str(reason or "").strip())
