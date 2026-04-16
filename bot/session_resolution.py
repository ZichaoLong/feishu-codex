"""
共享线程发现与恢复目标解析逻辑。

飞书 handler 与 shell 级 fcodex 都应复用这里的规则，
避免出现两套 session / thread 发现行为。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol
from uuid import UUID

from bot.adapters.base import ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig


class ThreadListingAdapter(Protocol):
    def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        search_term: str | None = None,
        sort_key: str = "updated_at",
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
    ) -> tuple[list[ThreadSummary], str | None]:
        ...

    def list_threads_all(
        self,
        *,
        cwd: str | None = None,
        limit: int = 100,
        search_term: str | None = None,
        sort_key: str = "updated_at",
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
    ) -> list[ThreadSummary]:
        ...


def looks_like_thread_id(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def format_thread_match(thread: ThreadSummary) -> str:
    provider = thread.model_provider or "unknown"
    return f"`{thread.thread_id[:8]}…`@`{provider}`"


def list_current_dir_threads(
    adapter: ThreadListingAdapter,
    *,
    cwd: str,
    limit: int,
    sort_key: str = "updated_at",
) -> list[ThreadSummary]:
    return adapter.list_threads_all(
        cwd=cwd,
        limit=limit,
        sort_key=sort_key,
        model_providers=[],
    )


def list_global_threads(
    adapter: ThreadListingAdapter,
    *,
    limit: int,
    sort_key: str = "updated_at",
) -> list[ThreadSummary]:
    return adapter.list_threads_all(
        limit=limit,
        sort_key=sort_key,
        model_providers=[],
    )


def resolve_resume_target_by_name(
    adapter: ThreadListingAdapter,
    *,
    name: str,
    limit: int,
    sort_key: str = "updated_at",
) -> ThreadSummary:
    target = name.strip()
    if not target:
        raise ValueError("恢复目标不能为空")

    exact_name: list[ThreadSummary] = []
    cursor: str | None = None
    page_size = max(int(limit or 0), 1)
    while True:
        page, cursor = adapter.list_threads(
            limit=page_size,
            cursor=cursor,
            sort_key=sort_key,
            model_providers=[],
        )
        for thread in page:
            if thread.name != target:
                continue
            exact_name.append(thread)
            if len(exact_name) > 1:
                ids = ", ".join(format_thread_match(item) for item in exact_name[:5])
                raise ValueError(f"匹配到多个同名线程：{ids}")
        if not cursor:
            break
    if not exact_name:
        raise ValueError(f"未找到匹配的线程：`{target}`")
    return exact_name[0]


def resolve_resume_name_via_remote_backend(
    *,
    base_config: CodexAppServerConfig,
    app_server_url: str,
    query_limit: int,
    target: str,
) -> ThreadSummary:
    adapter = CodexAppServerAdapter(
        replace(
            base_config,
            app_server_mode="remote",
            app_server_url=app_server_url,
        )
    )
    try:
        return resolve_resume_target_by_name(
            adapter,
            name=target,
            limit=query_limit,
        )
    finally:
        adapter.stop()
