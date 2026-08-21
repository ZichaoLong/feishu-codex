import unittest
from typing import get_type_hints

from bot.adapters.base import ThreadSummary
from bot.fcodex.cli import _resolve_thread_target_via_attached_endpoint
from bot.thread_resolution import (
    format_thread_match,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)


class SessionResolutionTests(unittest.TestCase):
    class _Adapter:
        def __init__(self, threads: list[ThreadSummary]) -> None:
            self.threads = threads

        def list_threads(
            self,
            *,
            cwd=None,
            limit=100,
            cursor=None,
            search_term=None,
            sort_key="updated_at",
            source_kinds=None,
            model_providers=None,
        ):
            del cwd
            del search_term
            del sort_key
            del source_kinds
            self.kwargs = {"limit": limit, "cursor": cursor, "model_providers": model_providers}
            start = int(cursor or 0)
            end = start + limit
            next_cursor = str(end) if end < len(self.threads) else None
            return list(self.threads[start:end]), next_cursor

        def list_threads_all(self, **kwargs):
            self.kwargs = kwargs
            return list(self.threads)

    def test_looks_like_thread_id(self) -> None:
        self.assertTrue(looks_like_thread_id("019d2e94-a475-7bc1-b2f7-a3ce37628ede"))
        self.assertFalse(looks_like_thread_id("demo"))

    def test_format_thread_match(self) -> None:
        thread = ThreadSummary(
            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            model_provider="provider2_api",
        )
        self.assertEqual(format_thread_match(thread), "`019d2e94…`@`provider2_api`")

    def test_resolve_resume_target_by_name_uses_cross_provider_listing(self) -> None:
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            model_provider="provider2_api",
        )
        adapter = self._Adapter([thread])

        resolved = resolve_resume_target_by_name(adapter, name="demo", limit=100)

        self.assertEqual(resolved.thread_id, "thread-1")
        self.assertEqual(adapter.kwargs["model_providers"], [])

    def test_resolve_resume_target_by_name_rejects_multiple_matches(self) -> None:
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-a",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-b",
            name="demo",
            preview="world",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        adapter = self._Adapter([thread_1, thread_2])

        with self.assertRaisesRegex(ValueError, "匹配到多个同名线程"):
            resolve_resume_target_by_name(adapter, name="demo", limit=100)

    def test_resolve_resume_target_by_name_scans_beyond_first_page_for_duplicate(self) -> None:
        threads = [
            ThreadSummary(
                thread_id=f"thread-{index}",
                cwd=f"/tmp/project-{index}",
                name="demo" if index in {1, 150} else f"name-{index}",
                preview="hello",
                created_at=0,
                updated_at=200 - index,
                source="cli",
                status="notLoaded",
            )
            for index in range(1, 151)
        ]
        adapter = self._Adapter(threads)

        with self.assertRaisesRegex(ValueError, "匹配到多个同名线程"):
            resolve_resume_target_by_name(adapter, name="demo", limit=100)

    def test_fcodex_attached_endpoint_thread_target_type_hints_resolve(self) -> None:
        hints = get_type_hints(_resolve_thread_target_via_attached_endpoint)

        self.assertEqual(
            hints["return"],
            tuple[ThreadSummary | None, str | None],
        )


if __name__ == "__main__":
    unittest.main()
