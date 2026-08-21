"""Generation-pinned response authority for upstream JSON-RPC requests."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TypeAlias

from bot.codex_protocol.deadline import held_lock_before_deadline


ServerRequestIdKey: TypeAlias = tuple[str, int | str]
ServerRequestResponseAuthority: TypeAlias = tuple[ServerRequestIdKey, int, int]


@dataclass(frozen=True, slots=True)
class ServerRequestAuthorityRotationReceipt:
    """Proof that stopped-backend response capabilities were retired."""

    retired_epoch: int
    active_epoch: int
    remembered_request_count: int
    consumed_authority_count: int


class ServerRequestAuthorityError(RuntimeError):
    """A generation-less response cannot claim one exact upstream request."""


class ServerRequestAuthorityRegistry:
    """Own receiving generations and one-shot response claims.

    Every claim requires the exact receiving generation. Remembered authority
    is retired after a response, a resolved notification, physical disconnect,
    or an explicit stopped-backend rotation so long-lived services do not keep
    process-lifetime request tombstones.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._epoch = 1
        self._generations: dict[ServerRequestIdKey, set[int]] = {}
        self._consumed: set[ServerRequestResponseAuthority] = set()

    def remember(self, request_id: int | str, connection_generation: int) -> None:
        if (
            isinstance(connection_generation, bool)
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
        ):
            raise ValueError(
                "server request receiving generation must be a positive integer"
            )
        key = self._request_key(request_id)
        with self._lock:
            self._generations.setdefault(key, set()).add(connection_generation)

    def claim(
        self,
        request_id: int | str,
        *,
        connection_generation: int,
        deadline_monotonic: float | None,
    ) -> ServerRequestResponseAuthority:
        key = self._request_key(request_id)
        if (
            isinstance(connection_generation, bool)
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
        ):
            raise ValueError(
                "server request response generation must be a positive integer"
            )
        with held_lock_before_deadline(
            self._lock,
            deadline_monotonic=deadline_monotonic,
            operation="server request response authority",
        ):
            generations = self._generations.get(key, set())
            if not generations:
                raise ServerRequestAuthorityError(
                    "Codex server request has no recorded receiving websocket generation"
                )
            generation = connection_generation
            if generation not in generations:
                raise ServerRequestAuthorityError(
                    "Codex server request was not received on the claimed websocket generation"
                )
            authority = (key, generation, self._epoch)
            if authority in self._consumed:
                raise ServerRequestAuthorityError(
                    "Codex server request response authority was already consumed"
                )
            self._consumed.add(authority)
            return authority

    def release(self, authority: ServerRequestResponseAuthority) -> None:
        with self._lock:
            self._consumed.discard(authority)

    def retire(self, authority: ServerRequestResponseAuthority) -> None:
        """Forget a submitted/unknown claim so duplicate attempts stay closed."""

        key, generation, epoch = authority
        with self._lock:
            if epoch != self._epoch or authority not in self._consumed:
                return
            self._consumed.discard(authority)
            generations = self._generations.get(key)
            if generations is None:
                return
            generations.discard(generation)
            if not generations:
                self._generations.pop(key, None)

    def remembered_request_count(self) -> int:
        with self._lock:
            return sum(map(len, self._generations.values()))

    def retire_request_generation(
        self,
        request_id: int | str,
        connection_generation: int,
    ) -> bool:
        """Retire one resolved request on its exact receiving connection."""

        self._validate_generation(connection_generation, operation="retirement")
        key = self._request_key(request_id)
        with self._lock:
            generations = self._generations.get(key)
            if generations is None or connection_generation not in generations:
                return False
            generations.discard(connection_generation)
            if not generations:
                self._generations.pop(key, None)
            self._consumed = {
                authority
                for authority in self._consumed
                if not (
                    authority[0] == key
                    and authority[1] == connection_generation
                )
            }
            return True

    def retire_connection_generation(self, connection_generation: int) -> int:
        """Retire every capability made unreachable by physical disconnect."""

        self._validate_generation(connection_generation, operation="retirement")
        with self._lock:
            retired = 0
            for key, generations in tuple(self._generations.items()):
                if connection_generation not in generations:
                    continue
                generations.discard(connection_generation)
                retired += 1
                if not generations:
                    self._generations.pop(key, None)
            self._consumed = {
                authority
                for authority in self._consumed
                if authority[1] != connection_generation
            }
            return retired

    def rotate_after_backend_stop(self) -> ServerRequestAuthorityRotationReceipt:
        """Retire all exact claims after the owned app-server machine stopped."""

        with self._lock:
            retired_epoch = self._epoch
            receipt = ServerRequestAuthorityRotationReceipt(
                retired_epoch=retired_epoch,
                active_epoch=retired_epoch + 1,
                remembered_request_count=sum(map(len, self._generations.values())),
                consumed_authority_count=len(self._consumed),
            )
            self._epoch = receipt.active_epoch
            self._generations.clear()
            self._consumed.clear()
            return receipt

    @staticmethod
    def _request_key(request_id: int | str) -> ServerRequestIdKey:
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise ValueError("Codex server request id must be an integer or string")
        return (
            ("int", request_id)
            if isinstance(request_id, int)
            else ("str", request_id)
        )

    @staticmethod
    def _validate_generation(connection_generation: int, *, operation: str) -> None:
        if (
            isinstance(connection_generation, bool)
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
        ):
            raise ValueError(
                f"server request {operation} generation must be a positive integer"
            )
