"""Opaque identity authority for resident binding-runtime sessions.

The binding runtime manager owns the mutable resident objects and serializes
all calls into this authority.  This module deliberately knows nothing about
their shape: it binds one exact object identity to one binding and one opaque
handle, without exposing or copying the resident object.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_contract import BindingRuntimeHandle


@dataclass(slots=True)
class _ResidentBindingSession:
    resident_state: object
    handle: BindingRuntimeHandle


class BindingRuntimeSessionAuthority:
    """Issue and validate handles for exact resident object incarnations.

    The class is intentionally lock-agnostic.  Its owning manager must
    serialize ``install``, ``current``, ``require``, and retirement together
    with updates to the manager's resident-state map.
    """

    _issuer_ids = count(1)

    def __init__(self) -> None:
        self._issuer_nonce = next(self._issuer_ids)
        self._next_incarnation = 0
        self._current_by_binding: dict[
            ChatBindingKey, _ResidentBindingSession
        ] = {}

    def install(
        self,
        binding: ChatBindingKey,
        *,
        resident_state: object,
    ) -> BindingRuntimeHandle:
        """Install one resident identity and return its unique current handle.

        Reinstalling the same current object is idempotent.  Replacing it, or
        installing it again after retirement, always creates a new
        incarnation so an older handle cannot regain authority through ABA.
        """

        self._require_resident_state(resident_state)
        current = self._current_by_binding.get(binding)
        if current is not None and current.resident_state is resident_state:
            return current.handle
        for other_binding, other in self._current_by_binding.items():
            if other_binding != binding and other.resident_state is resident_state:
                raise RuntimeError(
                    "resident binding-runtime object is already installed "
                    "for another binding"
                )

        self._next_incarnation += 1
        handle = BindingRuntimeHandle(
            _issuer_nonce=self._issuer_nonce,
            binding=binding,
            incarnation=self._next_incarnation,
        )
        self._current_by_binding[binding] = _ResidentBindingSession(
            resident_state=resident_state,
            handle=handle,
        )
        return handle

    def advance(
        self,
        handle: BindingRuntimeHandle,
        *,
        binding: ChatBindingKey,
        resident_state: object,
    ) -> BindingRuntimeHandle:
        """Rotate authority after a binding-owner revision changes in place.

        Binding A -> B -> A can keep the same mutable state object.  Requiring
        the exact old handle before rotation and always minting a new one
        prevents a command captured against A from regaining authority after
        that field-level ABA.
        """

        self.require(
            handle,
            binding=binding,
            resident_state=resident_state,
        )
        self._current_by_binding.pop(binding)
        return self.install(binding, resident_state=resident_state)

    def current(
        self,
        binding: ChatBindingKey,
        *,
        resident_state: object,
    ) -> BindingRuntimeHandle | None:
        """Return the handle only when both binding and object are current."""

        current = self._current_by_binding.get(binding)
        if current is None or current.resident_state is not resident_state:
            return None
        return current.handle

    def require(
        self,
        handle: BindingRuntimeHandle,
        *,
        binding: ChatBindingKey,
        resident_state: object,
    ) -> None:
        """Require an exact authority-issued handle and resident identity."""

        if type(handle) is not BindingRuntimeHandle:
            raise RuntimeError("binding runtime handle lacks typed identity")
        if handle._issuer_nonce != self._issuer_nonce:
            raise RuntimeError("binding runtime handle belongs to another authority")
        current = self._current_by_binding.get(binding)
        if (
            handle.binding != binding
            or current is None
            or current.handle is not handle
            or current.resident_state is not resident_state
            or current.handle.incarnation != handle.incarnation
        ):
            raise RuntimeError("binding runtime handle is stale or replaced")

    def retire(
        self,
        handle: BindingRuntimeHandle,
        *,
        binding: ChatBindingKey,
        resident_state: object,
    ) -> None:
        """Retire exactly the incarnation authorized by ``handle``."""

        self.require(
            handle,
            binding=binding,
            resident_state=resident_state,
        )
        self._current_by_binding.pop(binding)

    def retire_all(self) -> None:
        """Retire every current handle without resetting incarnation order."""

        self._current_by_binding.clear()

    @staticmethod
    def _require_resident_state(resident_state: object) -> None:
        if resident_state is None:
            raise TypeError("resident binding-runtime object cannot be None")
