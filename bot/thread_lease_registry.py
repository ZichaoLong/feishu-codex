"""
Thread subscription and single-writer lease registry.

Bindings may subscribe to the same Codex thread concurrently so they can keep a
shared thread selection. Actual execution writes are still serialized through a
single write lease per thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

ChatBindingKey: TypeAlias = tuple[str, str]


@dataclass(frozen=True, slots=True)
class ThreadUnsubscribeResult:
    removed: bool
    write_lease_released: bool
    thread_orphaned: bool


@dataclass(frozen=True, slots=True)
class WriteLeaseAcquireResult:
    granted: bool
    owner: ChatBindingKey | None


class ThreadLeaseRegistry:
    def __init__(self) -> None:
        self._subscribers_by_thread_id: dict[str, set[ChatBindingKey]] = {}
        self._write_owner_by_thread_id: dict[str, ChatBindingKey] = {}

    @staticmethod
    def _normalize_thread_id(thread_id: str) -> str:
        return str(thread_id or "").strip()

    def subscribe(self, binding: ChatBindingKey, thread_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False
        subscribers = self._subscribers_by_thread_id.setdefault(normalized_thread_id, set())
        before = len(subscribers)
        subscribers.add(binding)
        return before == 0

    def unsubscribe(self, binding: ChatBindingKey, thread_id: str) -> ThreadUnsubscribeResult:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return ThreadUnsubscribeResult(
                removed=False,
                write_lease_released=False,
                thread_orphaned=False,
            )

        subscribers = self._subscribers_by_thread_id.get(normalized_thread_id)
        removed = False
        if subscribers is not None and binding in subscribers:
            subscribers.remove(binding)
            removed = True
            if not subscribers:
                self._subscribers_by_thread_id.pop(normalized_thread_id, None)

        write_lease_released = False
        if self._write_owner_by_thread_id.get(normalized_thread_id) == binding:
            self._write_owner_by_thread_id.pop(normalized_thread_id, None)
            write_lease_released = True

        thread_orphaned = normalized_thread_id not in self._subscribers_by_thread_id
        return ThreadUnsubscribeResult(
            removed=removed,
            write_lease_released=write_lease_released,
            thread_orphaned=thread_orphaned,
        )

    def subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        subscribers = self._subscribers_by_thread_id.get(normalized_thread_id) or set()
        return tuple(sorted(subscribers))

    def lease_owner(self, thread_id: str) -> ChatBindingKey | None:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return None
        return self._write_owner_by_thread_id.get(normalized_thread_id)

    def acquire_write_lease(self, binding: ChatBindingKey, thread_id: str) -> WriteLeaseAcquireResult:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return WriteLeaseAcquireResult(granted=False, owner=None)

        current_owner = self._write_owner_by_thread_id.get(normalized_thread_id)
        if current_owner is not None and current_owner != binding:
            return WriteLeaseAcquireResult(granted=False, owner=current_owner)

        self.subscribe(binding, normalized_thread_id)
        self._write_owner_by_thread_id[normalized_thread_id] = binding
        return WriteLeaseAcquireResult(granted=True, owner=binding)

    def release_write_lease(self, binding: ChatBindingKey, thread_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False
        if self._write_owner_by_thread_id.get(normalized_thread_id) != binding:
            return False
        self._write_owner_by_thread_id.pop(normalized_thread_id, None)
        return True
