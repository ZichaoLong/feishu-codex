"""Gateway admission regressions for cursor-paged thread inspection reads."""

from __future__ import annotations

import asyncio
import threading

from bot.web_runtime.thread_inspection_wire import encode_thread_inspection_json
from tests.web_runtime.gateway_harness import WebGatewayHarness


class ThreadInspectionGatewayTests(WebGatewayHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tool_detail_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.search_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def prepare_tool_detail(*args, **kwargs):
            client_id = str(args[0])
            self.assertTrue(self.gateway._client_operation_locks[client_id].locked())
            self.tool_detail_calls.append((args, kwargs))
            return (
                "inspection",
                {
                    "kind": "tool",
                    "view": kwargs["view"],
                    "change_index": kwargs["change_index"],
                    "cursor": kwargs["cursor"],
                },
            )

        def prepare_conversation_search(*args, **kwargs):
            client_id = str(args[0])
            self.assertTrue(self.gateway._client_operation_locks[client_id].locked())
            self.search_calls.append((args, kwargs))
            return (
                "inspection",
                {"query": kwargs["query"], "cursor": kwargs["cursor"]},
            )

        self.gateway._ports.prepare_tool_detail = prepare_tool_detail
        self.gateway._ports.prepare_conversation_search = (
            prepare_conversation_search
        )

    async def test_tool_detail_uses_exact_path_and_document_serialization(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="inspection-client",
            incarnation_id="inspection-document",
        )
        headers = self._client_headers(document)

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns/turn-1/tool-items/item-1?view=preview",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                await response.json(),
                {
                    "kind": "tool",
                    "view": "preview",
                    "change_index": None,
                    "cursor": None,
                },
            )
        self.assertEqual(
            self.tool_detail_calls[-1],
            (
                (document["client_id"], "thread-1", "turn-1", "item-1"),
                {"view": "preview", "change_index": None, "cursor": None},
            ),
        )

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns/turn-1/tool-items/item-1"
            "?view=full&change_index=4294967295",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                await response.json(),
                {
                    "kind": "tool",
                    "view": "full",
                    "change_index": 4_294_967_295,
                    "cursor": None,
                },
            )
        self.assertEqual(
            self.tool_detail_calls[-1][1],
            {"view": "full", "change_index": 4_294_967_295, "cursor": None},
        )

    async def test_tool_detail_query_fails_before_port_dispatch(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="inspection-invalid-client",
            incarnation_id="inspection-invalid-document",
        )
        headers = self._client_headers(document)
        base = (
            f"{self.endpoint}/api/threads/thread-1/turns/turn-1/tool-items/item-1"
        )

        for query in (
            "",
            "view=raw",
            "change_index=01",
            "view=preview&change_index=0&change_index=1",
            "view=preview&change_index=0&other=value",
            "view=preview&cursor=",
            "view=preview&cursor=one&cursor=two",
        ):
            with self.subTest(query=query):
                async with self.session.get(f"{base}?{query}", headers=headers) as response:
                    self.assertEqual(response.status, 400)
                    payload = await response.json()
                self.assertEqual(payload["error"]["code"], "invalid_tool_detail_query")
        self.assertEqual(self.tool_detail_calls, [])

    async def test_tool_detail_forwards_one_opaque_cursor(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="inspection-cursor-client",
            incarnation_id="inspection-cursor-document",
        )
        headers = self._client_headers(document)
        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns/turn-1/tool-items/item-1",
            params={"view": "preview", "cursor": "opaque-page"},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(
            self.tool_detail_calls[-1][1],
            {"view": "preview", "change_index": None, "cursor": "opaque-page"},
        )

    async def test_conversation_search_normalizes_query_and_preserves_cursor(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="search-client",
            incarnation_id="search-document",
        )
        headers = self._client_headers(document)

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/conversation-search",
            params={"query": "  needle  ", "cursor": "opaque cursor"},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                await response.json(),
                {"query": "needle", "cursor": "opaque cursor"},
            )
        self.assertEqual(
            self.search_calls,
            [
                (
                    (document["client_id"], "thread-1"),
                    {"query": "needle", "cursor": "opaque cursor"},
                )
            ],
        )

    async def test_response_body_uses_the_same_compact_utf8_bytes_as_size_admission(
        self,
    ) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="inspection-bytes-client",
            incarnation_id="inspection-bytes-document",
        )
        headers = self._client_headers(document)
        payload = {
            "runtime_epoch": "epoch-界",
            "revision": 1,
            "thread_id": "thread-1",
            "query": "查找",
            "cursor": None,
            "occurrences": [{
                "turn_id": "turn-1",
                "item_id": "item-1",
                "snippet": "前缀😀查找后缀",
                "snippet_match_range": {"start": 4, "end": 6},
                "turn_cursor": "游标",
            }],
            "next_cursor": None,
        }
        self.gateway._ports.prepare_conversation_search = (
            lambda *_args, **_kwargs: ("inspection", payload)
        )

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/conversation-search",
            params={"query": "查找"},
            headers=headers,
        ) as response:
            body = await response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual(body, encode_thread_inspection_json(payload))
            self.assertEqual(int(response.headers["Content-Length"]), len(body))

    async def test_conversation_search_query_fails_before_port_dispatch(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="search-invalid-client",
            incarnation_id="search-invalid-document",
        )
        headers = self._client_headers(document)
        base = f"{self.endpoint}/api/threads/thread-1/conversation-search"

        for query in (
            "",
            "?query=",
            "?query=needle&query=other",
            "?query=needle&cursor=one&cursor=two",
            "?query=needle&other=value",
        ):
            with self.subTest(query=query):
                async with self.session.get(f"{base}{query}", headers=headers) as response:
                    self.assertEqual(response.status, 400)
                    payload = await response.json()
                self.assertEqual(
                    payload["error"]["code"],
                    "invalid_conversation_search_query",
                )
        self.assertEqual(self.search_calls, [])

    async def test_document_lock_is_released_before_inspection_execution(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="inspection-staged-client",
            incarnation_id="inspection-staged-document",
        )
        headers = self._client_headers(document)
        execution_started = threading.Event()
        allow_execution = threading.Event()

        def run_prepared(prepared):
            client_id = str(document["client_id"])
            self.assertFalse(
                self.gateway._client_operation_locks[client_id].locked()
            )
            execution_started.set()
            if not allow_execution.wait(timeout=2.0):
                raise AssertionError("inspection execution was not released")
            return prepared[1]

        async def request_inspection() -> tuple[int, dict]:
            async with self.session.get(
                f"{self.endpoint}/api/threads/thread-1/conversation-search",
                params={"query": "needle"},
                headers=headers,
            ) as response:
                return response.status, await response.json()

        self.gateway._ports.run_prepared_thread_read = run_prepared
        inspection = asyncio.create_task(request_inspection())
        try:
            self.assertTrue(
                await asyncio.to_thread(execution_started.wait, 1.0),
                "inspection execution did not start",
            )
            async with asyncio.timeout(0.5):
                async with self.session.get(
                    f"{self.endpoint}/api/meta",
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 200)
        finally:
            allow_execution.set()
            inspection_status, inspection_payload = await inspection

        self.assertEqual(inspection_status, 200)
        self.assertEqual(
            inspection_payload,
            {"query": "needle", "cursor": None},
        )
