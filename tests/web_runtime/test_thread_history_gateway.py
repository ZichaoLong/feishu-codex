"""Gateway admission regressions for bounded thread-history reads."""

from tests.web_runtime.gateway_harness import WebGatewayHarness


class ThreadHistoryGatewayTests(WebGatewayHarness):
    async def test_history_page_admits_only_exact_items_view(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="history-view-client",
            incarnation_id="history-view-document",
        )
        headers = self._client_headers(document)

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns"
            "?cursor=cursor-1&items_view=summary",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["cursor"], "cursor-1")
        self.assertEqual(payload["items_view"], "summary")
        self.assertEqual(payload["turn_limit"], 10)

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns?items_view=summary",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["cursor"], "")
        self.assertEqual(payload["items_view"], "summary")

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns?cursor=cursor-2",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["items_view"], "full")

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns"
            "?cursor=cursor-20&items_view=summary&turn_limit=20",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["turn_limit"], 20)

        calls = 0

        def count_history_calls(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {}

        self.gateway._ports.prepare_list_older_turns = count_history_calls
        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/turns"
            "?cursor=cursor-3&items_view=compact",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 400)
            payload = await response.json()
        self.assertEqual(payload["error"]["code"], "invalid_items_view")
        self.assertEqual(calls, 0)

    async def test_recent_and_history_turn_limit_fail_closed(self) -> None:
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="turn-limit-client",
            incarnation_id="turn-limit-document",
        )
        headers = self._client_headers(document)

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1?turn_limit=5",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["turn_limit"], 5)

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1?turn_limit=10",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["turn_limit"], 10)

        for query in (
            "turn_limit=",
            "turn_limit=40",
            "turn_limit=%2010%20",
            "turn_limit=5&turn_limit=20",
        ):
            async with self.session.get(
                f"{self.endpoint}/api/threads/thread-1/turns?items_view=summary&{query}",
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 400, query)
                payload = await response.json()
            self.assertEqual(payload["error"]["code"], "invalid_turn_limit", query)
