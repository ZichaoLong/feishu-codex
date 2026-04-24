from __future__ import annotations

from collections.abc import Callable

THREAD_RESUME_PROFILE_LOADED_REASON = (
    "当前 thread 仍处于 loaded 状态；请先释放飞书侧订阅，并关闭所有打开该 thread 的 `fcodex` TUI。"
)
THREAD_RESUME_PROFILE_ADAPTER_UNAVAILABLE_REASON = (
    "当前无法确认该 thread 是否已完全 unloaded；请稍后重试。"
)


def check_thread_resume_profile_mutable(
    thread_id: str,
    *,
    unbound_reason: str,
    has_attached_binding: Callable[[str], bool] | None = None,
    has_runtime_lease: Callable[[str], bool] | None = None,
    list_loaded_thread_ids: Callable[[], list[str]],
) -> tuple[bool, str]:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        return False, unbound_reason
    if has_attached_binding is not None and has_attached_binding(normalized_thread_id):
        return False, THREAD_RESUME_PROFILE_LOADED_REASON
    if has_runtime_lease is not None and has_runtime_lease(normalized_thread_id):
        return False, THREAD_RESUME_PROFILE_LOADED_REASON
    try:
        loaded_thread_ids = {
            str(item or "").strip()
            for item in list_loaded_thread_ids()
            if str(item or "").strip()
        }
    except Exception:
        return False, THREAD_RESUME_PROFILE_ADAPTER_UNAVAILABLE_REASON
    if normalized_thread_id in loaded_thread_ids:
        return False, THREAD_RESUME_PROFILE_LOADED_REASON
    return True, ""
