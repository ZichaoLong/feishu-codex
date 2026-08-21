"""Runtime-owned Feishu thread subscription registry."""

from __future__ import annotations

import logging
from typing import Callable, TypeAlias

ChatBindingKey: TypeAlias = tuple[str, str]
ThreadMembershipChanged: TypeAlias = Callable[[str], None]
logger = logging.getLogger(__name__)


class ThreadSubscriptionRegistry:
    """
    Runtime-owned Feishu thread subscription state.

    This object is intentionally not internally synchronized. Callers must only
    use it under an outer serialization boundary such as `RuntimeLoop` plus the
    handler/runtime lock. If it ever needs standalone concurrent use, that is a
    contract change and this type should gain its own synchronization.
    """

    def __init__(
        self,
        *,
        membership_changed: ThreadMembershipChanged | None = None,
    ) -> None:
        self._subscribers_by_thread_id: dict[str, set[ChatBindingKey]] = {}
        self._membership_changed = membership_changed

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
        if len(subscribers) != before:
            self._notify_membership_changed(normalized_thread_id)
        return before == 0

    def unsubscribe(self, binding: ChatBindingKey, thread_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False

        changed = False
        subscribers = self._subscribers_by_thread_id.get(normalized_thread_id)
        if subscribers is not None and binding in subscribers:
            subscribers.remove(binding)
            changed = True
            if not subscribers:
                self._subscribers_by_thread_id.pop(normalized_thread_id, None)
        if changed:
            self._notify_membership_changed(normalized_thread_id)

        return normalized_thread_id not in self._subscribers_by_thread_id

    def subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        subscribers = self._subscribers_by_thread_id.get(normalized_thread_id) or set()
        return tuple(sorted(subscribers))

    def clear(self) -> None:
        changed_thread_ids = tuple(self._subscribers_by_thread_id)
        self._subscribers_by_thread_id.clear()
        for thread_id in changed_thread_ids:
            self._notify_membership_changed(thread_id)

    def _notify_membership_changed(self, thread_id: str) -> None:
        if self._membership_changed is None:
            return
        try:
            self._membership_changed(thread_id)
        except Exception:
            # Subscription state is lifecycle authority; presentation fan-out
            # must never turn a committed binding mutation into a failure.
            logger.exception(
                "Thread subscription membership listener failed: thread_id=%s",
                thread_id,
            )
