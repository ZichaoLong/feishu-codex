"""Runtime-owned Feishu thread subscription registry."""

from __future__ import annotations

from typing import TypeAlias

ChatBindingKey: TypeAlias = tuple[str, str]


class ThreadSubscriptionRegistry:
    """
    Runtime-owned Feishu thread subscription state.

    This object is intentionally not internally synchronized. Callers must only
    use it under an outer serialization boundary such as `RuntimeLoop` plus the
    handler/runtime lock. If it ever needs standalone concurrent use, that is a
    contract change and this type should gain its own synchronization.
    """

    def __init__(self) -> None:
        self._subscribers_by_thread_id: dict[str, set[ChatBindingKey]] = {}

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

    def unsubscribe(self, binding: ChatBindingKey, thread_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False

        subscribers = self._subscribers_by_thread_id.get(normalized_thread_id)
        if subscribers is not None and binding in subscribers:
            subscribers.remove(binding)
            if not subscribers:
                self._subscribers_by_thread_id.pop(normalized_thread_id, None)

        return normalized_thread_id not in self._subscribers_by_thread_id

    def subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        subscribers = self._subscribers_by_thread_id.get(normalized_thread_id) or set()
        return tuple(sorted(subscribers))
