"""Authoritative policy for direct frontend targets.

ThreadSpawn descendants belong to their parent root. They may be observed as
tasks, and an already-pending interaction may be routed through its own exact
server-request capability, but they are not independent conversations a frontend may
resume, mutate, interrupt, archive, or bind to a Feishu session.

The check intentionally consumes an authoritative ``ThreadSummary`` instead
of a cached child-to-root map.  A missing cache entry must not turn a child
into an unlocked root target.
"""

from __future__ import annotations

from collections.abc import Callable

from bot.adapters.base import ThreadSnapshot, ThreadSummary

_DIRECT_ROOT_SOURCES = frozenset({"cli", "vscode", "exec", "appServer"})


class DirectThreadTargetPolicyError(ValueError):
    """Raised when a frontend tries to directly operate a parent-owned thread."""


class DirectThreadTargetRegistry:
    """Current-process proof that an exact thread is a frontend-direct root.

    It stores no parent relation, child status, result, runtime state, or
    recovery intent. Unknown ids therefore remain ineligible for shared
    approval until an authoritative ``ThreadSummary`` proves a direct root.
    """

    def __init__(self) -> None:
        self._thread_ids: set[str] = set()

    def remember(self, summary: ThreadSummary) -> None:
        if not isinstance(summary, ThreadSummary):
            raise TypeError("direct thread target evidence must be a ThreadSummary")
        thread_id = str(summary.thread_id or "").strip()
        if not thread_id:
            raise DirectThreadTargetPolicyError(
                "无法确认直接操作目标的权威 thread id；已按 fail-closed 拒绝。"
            )
        if direct_thread_target_denial(summary):
            self._thread_ids.discard(thread_id)
            return
        self._thread_ids.add(thread_id)

    def is_known(self, thread_id: str) -> bool:
        return str(thread_id or "").strip() in self._thread_ids

    def forget(self, thread_id: str) -> None:
        self._thread_ids.discard(str(thread_id or "").strip())

    def clear(self) -> None:
        self._thread_ids.clear()


def direct_thread_target_denial(summary: ThreadSummary, *, operation: str = "操作") -> str:
    """Return a human-safe denial for a non-root direct target, if any.

    A malformed ``threadSpawn`` record (missing parent id) is still rejected:
    treating malformed ancestry as a standalone root is the dangerous
    direction.  Other auxiliary/review/guardian relations are not
    automatically ThreadSpawn descendants merely because they have a parent;
    their existing lifecycle contracts remain unchanged.
    """

    thread_id = str(summary.thread_id or "").strip()
    source = str(summary.source or "").strip()
    subagent_kind = str(summary.subagent_kind or "").strip()
    if subagent_kind == "threadSpawn":
        prefix = f"线程 `{thread_id}` " if thread_id else "该线程 "
        return (
            f"{prefix}是 parent-owned ThreadSpawn subagent；不能直接{operation}。"
            "请在 root thread 上继续或管理；已存在的 child 审批/输入由 Focus 按 exact request 路由。"
        )
    if (
        summary.source_status != "known"
        or source not in _DIRECT_ROOT_SOURCES | {"subAgent"}
        or (source == "subAgent" and not subagent_kind)
        or (source != "subAgent" and subagent_kind)
    ):
        prefix = f"线程 `{thread_id}` " if thread_id else "该线程 "
        return (
            f"{prefix}的 upstream Thread.source 无法被权威确认；不能直接{operation}。"
            "Focus 不把 unknown 或 malformed source 猜成 root。"
        )
    return ""


def require_direct_thread_target(
    summary: ThreadSummary,
    *,
    expected_thread_id: str = "",
    operation: str = "操作",
) -> ThreadSummary:
    """Return a verified direct target or fail closed for a child-like thread.

    Callers must not treat a response for a different thread as evidence that
    their requested target is a root.  This is especially important at the
    fcodex/service boundary, where a malformed or stale JSON-RPC response
    cannot be allowed to turn an unknown target into a writable one.
    """

    expected = str(expected_thread_id or "").strip()
    actual = str(summary.thread_id or "").strip()
    if expected and actual != expected:
        raise DirectThreadTargetPolicyError(
            "无法确认直接操作目标的权威 thread id；已按 fail-closed 拒绝。"
        )
    denial = direct_thread_target_denial(summary, operation=operation)
    if denial:
        raise DirectThreadTargetPolicyError(denial)
    return summary


def read_direct_thread_target(
    thread_id: str,
    *,
    read_thread: Callable[[str], ThreadSnapshot],
    operation: str = "操作",
) -> ThreadSummary:
    """Read and validate one frontend-direct target from the authority.

    This function intentionally does *not* catch transport/protocol errors.
    Each surface maps those errors into its own user-facing failure, but all of
    them must fail closed before they create a subscription, take a lease, or
    send an upstream mutation.  A valid ``ThreadSpawn`` record without a
    parent still fails in :func:`require_direct_thread_target`.
    """

    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        raise DirectThreadTargetPolicyError("thread_id 不能为空。")
    snapshot = read_thread(normalized_thread_id)
    if not isinstance(snapshot, ThreadSnapshot) or not isinstance(snapshot.summary, ThreadSummary):
        raise DirectThreadTargetPolicyError(
            "无法确认直接操作目标的权威 thread 摘要；已按 fail-closed 拒绝。"
        )
    return require_direct_thread_target(
        snapshot.summary,
        expected_thread_id=normalized_thread_id,
        operation=operation,
    )
