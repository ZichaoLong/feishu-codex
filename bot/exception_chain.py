"""Small, shared exception-chain traversal primitives."""

from __future__ import annotations

from collections.abc import Iterator


def iter_exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield ``error`` and its preferred cause/context chain once each.

    Explicit ``__cause__`` takes precedence over implicit ``__context__``,
    matching the classifiers which use this helper.  Identity tracking keeps
    manually constructed or third-party exception cycles bounded.
    """

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__
