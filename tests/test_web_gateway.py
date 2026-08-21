import asyncio
import inspect
import pathlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession, CookieJar, FormData, WSMsgType, web
from yarl import URL

from bot import web_assets
from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.thread_runtime_coordination import ThreadRuntimeAdmissionError
from bot.web_runtime.auth import WebAuthSession
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime import gateway_request_decoder as request_decoder
from bot.web_runtime.controller import WebRuntimeError
from bot.web_runtime.projection import FocusWebProjection
from tests.web_runtime.gateway_harness import WebGatewayHarness
from tests.web_runtime.harness import WebRuntimeControllerHarness


class WebGatewayTests(WebGatewayHarness):
    @staticmethod
    def _prompt_body(document: dict, *, text: str = "hello") -> dict:
        mutation_id = "11111111-1111-4111-8111-111111111111"
        return {
            "text": text,
            "attachment_ids": [],
            "mutation_id": mutation_id,
            "source_scope_generation": 1,
            "source_attachment_scope": "thread:thread-1",
            "source_composer_scope_id": (
                f"{document['client_id']}:generation:1:thread:thread-1"
            ),
        }

    async def test_stop_disconnects_clients_without_default_executor(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="shutdown-client",
            incarnation_id="shutdown-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")
        self.assertEqual(self.connected, [document["client_id"]])

        with patch(
            "bot.web_runtime.gateway.asyncio.to_thread",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ):
            self.gateway.stop()

        self.assertEqual(self.disconnected, [document["client_id"]])
        await socket.close()

    def test_document_runtime_dispatches_are_request_bound(self):
        source = inspect.getsource(WebGateway)
        helper = inspect.getsource(WebGateway._document_request_to_thread)
        staged_helper = inspect.getsource(
            WebGateway._staged_document_request_to_thread
        )

        self.assertEqual(
            source.count("self._document_request_to_thread("),
            21,
        )
        self.assertEqual(
            source.count("self._staged_document_request_to_thread("),
            4,
        )
        self.assertNotIn("self._client_to_thread(", source)
        self.assertIn("self._required_client_id(request)", helper)
        self.assertIn("self._required_client_id(request)", staged_helper)

    async def test_interrupt_body_is_exact_before_port_dispatch(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="interrupt-document",
        )
        headers = self._client_headers(document)
        invalid_cases = (
            ({}, "invalid_turn_id"),
            ({"turn_id": None}, "invalid_turn_id"),
            ({"turn_id": True}, "invalid_turn_id"),
            ({"turn_id": 1}, "invalid_turn_id"),
            ({"turn_id": {}}, "invalid_turn_id"),
            ({"turn_id": []}, "invalid_turn_id"),
            ({"turn_id": "   "}, "invalid_turn_id"),
            ({"turn_id": " turn-1"}, "invalid_turn_id"),
            ({"turn_id": "turn-1 "}, "invalid_turn_id"),
            ({"turn_id": "turn-1", "extra": True}, "invalid_turn_id"),
            (["turn-1"], "invalid_json"),
        )
        for body, code in invalid_cases:
            with self.subTest(body=body):
                self.calls.clear()
                async with self.session.post(
                    f"{self.endpoint}/api/threads/thread-1/interrupt",
                    json=body,
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 400)
                    payload = await response.json()
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(self.calls, [])

        for turn_id in ("", "turn-1"):
            with self.subTest(turn_id=turn_id):
                self.calls.clear()
                async with self.session.post(
                    f"{self.endpoint}/api/threads/thread-1/interrupt",
                    json={"turn_id": turn_id},
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 200)
                    payload = await response.json()
                self.assertEqual(payload["turn_id"], turn_id)
                self.assertEqual(
                    self.calls,
                    [
                        (
                            "interrupt",
                            (document["client_id"], "thread-1"),
                            {"turn_id": turn_id},
                        )
                    ],
                )

    async def test_prompt_post_and_result_route_are_strict(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="prompt-intent-document",
        )
        headers = self._client_headers(document)
        prompt = self._prompt_body(document, text="keep draft")
        invalid_cases = (
            {},
            {**prompt, "phase": "dispatch"},
            {**prompt, "client_user_message_id": "focus-web:forged"},
            {**prompt, "recovery_capability": "A" * 43},
            {**prompt, "base_generation": 0},
        )
        for invalid_body in invalid_cases:
            with self.subTest(body=invalid_body):
                async with self.session.post(
                    f"{self.endpoint}/api/threads/thread-1/prompt",
                    json=invalid_body,
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 400)
                    payload = await response.json()
                    self.assertEqual(
                        payload["error"]["code"],
                        "invalid_prompt",
                    )
        self.assertEqual(self.calls, [])

        mutation_id = prompt["mutation_id"]
        async with self.session.post(
            f"{self.endpoint}/api/threads/thread-1/prompt",
            json={**prompt, "text": "exact follow-up"},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            receipt = await response.json()
        self.assertEqual(receipt["mutation_id"], mutation_id)
        self.assertEqual(receipt["client_user_message_id"], f"focus-web:{mutation_id}")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["mode"], "start")
        self.assertEqual(self.calls[-1][0], (document["client_id"], "thread-1"))
        self.assertEqual(
            self.calls[-1][1],
            {
                "text": "exact follow-up",
                "attachment_ids": [],
                "mutation_id": mutation_id,
                "source_scope_generation": 1,
                "source_attachment_scope": "thread:thread-1",
                "source_composer_scope_id": (
                    f"{document['client_id']}:generation:1:thread:thread-1"
                ),
            },
        )

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/prompt-result/{mutation_id}",
            headers=self._client_headers(
                document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), receipt)
        self.assertEqual(
            self.calls[-1],
            (
                "prompt_result",
                (document["client_id"], "thread-1"),
                {"mutation_id": mutation_id},
            ),
        )

        before_invalid_result = list(self.calls)
        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1/prompt-result/not-a-uuid",
            headers=self._client_headers(
                document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 400)
            self.assertEqual(
                (await response.json())["error"]["code"],
                "invalid_mutation_id",
            )
        self.assertEqual(self.calls, before_invalid_result)

    async def test_static_bootstrap_and_authenticated_mutation(self):
        default_gateway = object.__new__(WebGateway)
        default_gateway._config = WebGatewayConfig()
        packaged_static_dir = (
            pathlib.Path(web_assets.__file__).resolve().parent / "dist"
        )
        self.assertEqual(default_gateway._static_dir(), packaged_static_dir)

        async with self.session.get(self.endpoint) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Focus Web", await response.text())
            self.assertIn("Content-Security-Policy", response.headers)
            self.assertIn(
                "media-src 'self' data: blob:",
                response.headers["Content-Security-Policy"],
            )

        for path in [
            "THIRD_PARTY_NOTICES.html",
            "THIRD_PARTY_NOTICES.md",
            "THIRD_PARTY_SBOM.json",
        ]:
            async with self.session.get(f"{self.endpoint}/{path}") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

        async with self.session.get(f"{self.endpoint}/api/meta") as response:
            self.assertEqual(response.status, 401)
        async with self.session.get(f"{self.endpoint}/api/operator-status") as response:
            self.assertEqual(response.status, 401)

        auth = await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="static-document",
        )
        self.assertNotEqual(document["client_id"], "tab-1")
        self.assertTrue(str(document["client_id"]).startswith("web-"))
        headers = self._client_headers(document)
        async with self.session.post(
            f"{self.endpoint}/api/threads/thread-1/prompt",
            json=self._prompt_body(document),
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["mode"], "start")
        self.assertEqual(self.calls[0][0], (document["client_id"], "thread-1"))

        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._client_headers(
                document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["csrf_token"], auth["csrf_token"])

        async with self.session.get(
            f"{self.endpoint}/api/operator-status",
            headers=self._client_headers(
                document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual((await response.json())["status"], "ok")
        self.assertEqual(len(self.operator_status_threads), 1)
        self.assertNotEqual(self.operator_status_threads[0], "focus-runtime")

        async with self.session.post(
            f"{self.endpoint}/api/threads",
            json={
                "text": "first",
                "cwd": "/work/project",
                "attachment_ids": ["attachment-1"],
            },
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["thread_id"], "new-thread")

        async with self.session.post(
            f"{self.endpoint}/api/threads/thread-1/mutation-unknown",
            json={
                "action": "discard",
                "mutation_id": "11111111-1111-4111-8111-111111111111",
            },
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(
            payload["kwargs"],
            {
                "action": "discard",
                "mutation_id": "11111111-1111-4111-8111-111111111111",
            },
        )

        async with self.session.post(
            f"{self.endpoint}/api/threads/thread-1/mutation-unknown",
            json={
                "action": "verify_lifecycle",
                "mutation_id": "22222222-2222-4222-8222-222222222222",
            },
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(
            payload["kwargs"],
            {
                "action": "verify_lifecycle",
                "mutation_id": "22222222-2222-4222-8222-222222222222",
            },
        )

        for mutation_id, expected_code in (
            (None, "invalid_unknown_resolution"),
            ("", "invalid_mutation_id"),
            (7, "invalid_mutation_id"),
        ):
            with self.subTest(mutation_id=mutation_id):
                body = {"action": "discard"}
                if mutation_id is not None:
                    body["mutation_id"] = mutation_id
                async with self.session.post(
                    f"{self.endpoint}/api/threads/thread-1/mutation-unknown",
                    json=body,
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 400)
                    self.assertEqual(
                        (await response.json())["error"]["code"],
                        expected_code,
                    )

        for removed_field in ("recovery_capability", "recovery_scope_generation"):
            with self.subTest(removed_field=removed_field):
                async with self.session.post(
                    f"{self.endpoint}/api/threads/thread-1/mutation-unknown",
                    json={
                        "action": "discard",
                        "mutation_id": "11111111-1111-4111-8111-111111111111",
                        removed_field: "removed",
                    },
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 400)
                    self.assertEqual(
                        (await response.json())["error"]["code"],
                        "invalid_unknown_resolution",
                    )

    async def test_request_response_requires_exact_backend_and_service_capability(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="response-tab",
            incarnation_id="response-document",
        )
        url = f"{self.endpoint}/api/requests/request-1/respond"
        headers = self._client_headers(document)

        for body, expected_code in (
            (
                {"action": "approve_once", "response_capability": "cap-1"},
                "invalid_request_generation",
            ),
            (
                {"action": "approve_once", "connection_generation": 1},
                "invalid_response_capability",
            ),
            (
                {
                    "action": "approve_once",
                    "connection_generation": True,
                    "response_capability": "cap-1",
                },
                "invalid_request_generation",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                async with self.session.post(
                    url, json=body, headers=headers
                ) as response:
                    self.assertEqual(response.status, 400)
                    self.assertEqual(
                        (await response.json())["error"]["code"],
                        expected_code,
                    )

        async with self.session.post(
            url,
            json={
                "action": "approve_once",
                "answers": {},
                "connection_generation": 7,
                "response_capability": "service-capability-7",
            },
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            result = await response.json()
        self.assertEqual(result["args"], [document["client_id"], "request-1"])
        self.assertEqual(
            result["kwargs"],
            {
                "action": "approve_once",
                "answers": {},
                "connection_generation": 7,
                "response_capability": "service-capability-7",
            },
        )

    async def test_unknown_api_route_returns_json_404_instead_of_spa_fallback(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="unknown-api-tab",
            incarnation_id="unknown-api-document",
        )
        async with self.session.get(
            f"{self.endpoint}/api/does-not-exist",
            headers=self._client_headers(
                document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 404)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual((await response.json())["error"]["code"], "api_not_found")

        # Frontend client-side navigation remains a normal SPA fallback.
        async with self.session.get(f"{self.endpoint}/threads/root-1") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("Focus Web", await response.text())

    async def test_document_registration_requires_authenticated_same_origin_request(
        self,
    ):
        body = {
            "resume_client_id": "tab-register",
            "incarnation_id": "registration-document",
        }
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json=body,
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 401)

        await self._authenticate()
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json=body,
            headers={"Origin": "http://127.0.0.1:9"},
        ) as response:
            self.assertEqual(response.status, 403)

        # A new JS document has no in-memory CSRF token yet.  The endpoint is
        # intentionally the one same-origin, session-authenticated exception.
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json=body,
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)

    async def test_attachment_upload_and_cookie_authenticated_download(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="attachment-document",
        )
        headers = self._client_headers(document)
        form = FormData()
        form.add_field("thread_id", "")
        form.add_field("cwd", "/work/project")
        form.add_field("scope_generation", "1")
        form.add_field(
            "file",
            b"image-bytes",
            filename="diagram.png",
            content_type="image/png",
        )

        async with self.session.post(
            f"{self.endpoint}/api/attachments",
            data=form,
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["file_id"], "attachment-1")
        self.assertEqual(self.calls[-1][1]["scope_generation"], 1)

        async with self.session.get(f"{self.endpoint}{payload['url']}") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b"image-bytes")
            self.assertEqual(response.content_type, "image/png")

    async def test_attachment_download_rejects_non_image_bytes(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="generic-attachment-document",
        )
        headers = self._client_headers(document)
        form = FormData()
        form.add_field("thread_id", "")
        form.add_field("cwd", "/work/project")
        form.add_field("scope_generation", "1")
        form.add_field(
            "file",
            b"not-a-video-preview",
            filename="clip.mp4",
            content_type="video/mp4",
        )

        async with self.session.post(
            f"{self.endpoint}/api/attachments",
            data=form,
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
        self.assertEqual(payload["url"], "/api/attachments/attachment-1")

        async with self.session.get(
            f"{self.endpoint}/api/attachments/attachment-1"
        ) as response:
            self.assertEqual(response.status, 404)
            body = await response.json()
        self.assertEqual(body["error"]["code"], "attachment_preview_unavailable")

    async def test_clear_goal_forwards_intent_generation(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="goal-document",
        )
        headers = self._client_headers(document, intent_generation=7)

        async with self.session.delete(
            f"{self.endpoint}/api/threads/thread-1/goal",
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()

        self.assertEqual(payload["kwargs"], {"intent_generation": 7})
        self.assertEqual(
            self.calls,
            [
                (
                    "clear_goal",
                    (document["client_id"], "thread-1"),
                    {"intent_generation": 7},
                )
            ],
        )

    async def test_websocket_receives_revision_events_and_disconnects_client(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="events-document",
        )
        socket = await self._connect_events(document)
        hello = await socket.receive_json()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(self.connected, [document["client_id"]])

        self.projection.publish(
            "thread_invalidated", thread_id="thread-1", reason="test"
        )
        event = await socket.receive_json(timeout=2)
        self.assertEqual(event["type"], "thread_invalidated")
        self.assertEqual(event["thread_id"], "thread-1")

        await socket.close()
        await self._wait_until(lambda: bool(self.transport_disconnected))
        self.assertEqual(self.transport_disconnected, [document["client_id"]])
        self.assertEqual(self.disconnected, [])
        await self._wait_until(
            lambda: bool(self.disconnected)
            and document["client_id"] not in self.gateway._client_operation_locks
        )
        self.assertEqual(self.disconnected, [document["client_id"]])
        self.assertNotIn(document["client_id"], self.gateway._client_operation_locks)

    async def test_connect_projection_change_is_committed_before_hello(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="pending-f5",
            incarnation_id="pending-f5-document",
        )

        def connect_with_pending_change(client_id: str) -> None:
            self.connected.append(client_id)
            self.projection.publish(
                "pending_request_changed",
                thread_id="thread-1",
                reason="document_connected",
            )

        self.gateway._ports.client_connected = connect_with_pending_change
        socket = await self._connect_events(document)

        hello = await socket.receive_json(timeout=2)
        event = await socket.receive_json(timeout=2)
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["revision"], 1)
        self.assertEqual(event["type"], "pending_request_changed")
        self.assertEqual(event["revision"], hello["revision"])
        self.assertEqual(event["reason"], "document_connected")
        await socket.close()

    async def test_only_last_event_socket_marks_transport_disconnected(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="multi-socket-tab",
            incarnation_id="multi-socket-document",
        )
        first_socket = await self._connect_events(document)
        second_socket = await self._connect_events(document)
        self.assertEqual((await first_socket.receive_json())["type"], "hello")
        self.assertEqual((await second_socket.receive_json())["type"], "hello")

        await first_socket.close()
        await asyncio.sleep(0.05)
        self.assertEqual(self.transport_disconnected, [])

        await second_socket.close()
        await self._wait_until(lambda: bool(self.transport_disconnected))
        self.assertEqual(self.transport_disconnected, [document["client_id"]])

    async def test_last_socket_disconnect_linearizes_before_writer_http_mutation(self):
        """A queued HTTP writer action cannot overtake transport fail-close."""

        await self._authenticate()
        document = await self._register_document(
            resume_client_id="race-client",
            incarnation_id="race-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")

        disconnect_entered = threading.Event()
        allow_disconnect = threading.Event()
        disconnect_finished = threading.Event()

        def blocking_transport_disconnect(client_id: str) -> None:
            disconnect_entered.set()
            allow_disconnect.wait(timeout=2.0)
            self.transport_disconnected.append(client_id)
            disconnect_finished.set()

        def prompt_after_disconnect_check(client_id: str, thread_id: str, **kwargs):
            if disconnect_finished.is_set():
                raise WebRuntimeError(
                    "This browser document is disconnected.",
                    code="web_writer_disconnected",
                    status=409,
                )
            return self._prepare_prompt(client_id, thread_id, **kwargs)

        self.gateway._ports.client_transport_disconnected = (
            blocking_transport_disconnect
        )
        self.gateway._ports.prepare_prompt = prompt_after_disconnect_check

        await socket.close()
        self.assertTrue(await asyncio.to_thread(disconnect_entered.wait, 1.0))

        async def submit_prompt():
            async with self.session.post(
                f"{self.endpoint}/api/threads/thread-1/prompt",
                json=self._prompt_body(
                    document,
                    text="must-fail-closed",
                ),
                headers=self._client_headers(document),
            ) as response:
                return response.status, await response.json()

        request_task = asyncio.create_task(submit_prompt())
        await asyncio.sleep(0.05)
        self.assertFalse(request_task.done())

        allow_disconnect.set()
        status, payload = await request_task
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "web_writer_disconnected")
        self.assertEqual(self.calls, [])
        await self._wait_until(lambda: bool(self.transport_disconnected))

    async def test_last_socket_disconnect_linearizes_before_resume_capable_thread_read(
        self,
    ):
        """Cold thread reads cannot resume after the document transport is gone."""

        await self._authenticate()
        document = await self._register_document(
            resume_client_id="read-race-client",
            incarnation_id="read-race-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")

        disconnect_entered = threading.Event()
        allow_disconnect = threading.Event()
        disconnect_finished = threading.Event()

        def blocking_transport_disconnect(client_id: str) -> None:
            disconnect_entered.set()
            allow_disconnect.wait(timeout=2.0)
            self.transport_disconnected.append(client_id)
            disconnect_finished.set()

        def read_after_disconnect_check(client_id: str, thread_id: str, **kwargs):
            if disconnect_finished.is_set():
                raise WebRuntimeError(
                    "This browser document is disconnected.",
                    code="web_writer_disconnected",
                    status=409,
                )
            return {"client_id": client_id, "thread": {"id": thread_id}}

        self.gateway._ports.client_transport_disconnected = (
            blocking_transport_disconnect
        )
        self.gateway._ports.prepare_read_thread = read_after_disconnect_check

        await socket.close()
        self.assertTrue(await asyncio.to_thread(disconnect_entered.wait, 1.0))

        async def read_thread():
            async with self.session.get(
                f"{self.endpoint}/api/threads/thread-1",
                headers=self._client_headers(document),
            ) as response:
                return response.status, await response.json()

        request_task = asyncio.create_task(read_thread())
        await asyncio.sleep(0.05)
        self.assertFalse(request_task.done())

        allow_disconnect.set()
        status, payload = await request_task
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "web_writer_disconnected")
        await self._wait_until(lambda: bool(self.transport_disconnected))

    async def test_loaded_elsewhere_thread_read_is_an_effect_free_http_conflict(self):
        runtime = WebRuntimeControllerHarness()
        runtime.setUp()
        self.addCleanup(runtime.doCleanups)

        def reject_cross_instance_resume(*_args, **_kwargs):
            raise ThreadRuntimeAdmissionError(
                "loaded elsewhere",
                blocking_instance="explorer",
                blocking_status="idle",
            )

        runtime.acquire_claimed_resume_thread_page = reject_cross_instance_resume
        self.gateway._ports.prepare_read_thread = (
            runtime.controller.prepare_read_thread
        )
        self.gateway._ports.run_prepared_thread_read = (
            runtime.controller.run_prepared_thread_read
        )
        self.gateway._ports.client_connected = runtime.controller.client_connected

        await self._authenticate()
        document = await self._register_document(
            resume_client_id="loaded-elsewhere-client",
            incarnation_id="loaded-elsewhere-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1",
            headers=self._client_headers(document),
        ) as response:
            self.assertEqual(response.status, 409)
            payload = await response.json()

        self.assertEqual(payload["error"]["code"], "thread_loaded_elsewhere")
        self.assertIn("explorer", payload["error"]["message"])
        self.assertEqual(runtime.fake.resume_calls, [])
        self.assertEqual(runtime.service_runtime_leases, set())
        self.assertIsNone(runtime.store.load("thread-1"))
        self.assertIsNone(runtime.controller._runtime_interest.snapshot("thread-1"))
        self.assertIsNone(runtime.operations.unknown_mutation_projection("thread-1"))
        await socket.close()

    async def test_unverified_runtime_thread_read_is_an_effect_free_http_failure(self):
        runtime = WebRuntimeControllerHarness()
        runtime.setUp()
        self.addCleanup(runtime.doCleanups)

        def reject_unverified_resume(*_args, **_kwargs):
            raise ThreadRuntimeAdmissionError(
                "registry unavailable",
                blocking_status="unknown",
            )

        runtime.acquire_claimed_resume_thread_page = reject_unverified_resume
        self.gateway._ports.prepare_read_thread = (
            runtime.controller.prepare_read_thread
        )
        self.gateway._ports.run_prepared_thread_read = (
            runtime.controller.run_prepared_thread_read
        )
        self.gateway._ports.client_connected = runtime.controller.client_connected

        await self._authenticate()
        document = await self._register_document(
            resume_client_id="runtime-unverified-client",
            incarnation_id="runtime-unverified-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")

        async with self.session.get(
            f"{self.endpoint}/api/threads/thread-1",
            headers=self._client_headers(document),
        ) as response:
            self.assertEqual(response.status, 503)
            payload = await response.json()

        self.assertEqual(payload["error"]["code"], "thread_runtime_unverified")
        self.assertNotIn("still loaded", payload["error"]["message"])
        self.assertEqual(runtime.fake.resume_calls, [])
        self.assertEqual(runtime.service_runtime_leases, set())
        self.assertIsNone(runtime.store.load("thread-1"))
        self.assertIsNone(runtime.controller._runtime_interest.snapshot("thread-1"))
        self.assertIsNone(runtime.operations.unknown_mutation_projection("thread-1"))
        await socket.close()

    async def test_last_socket_disconnect_linearizes_before_thread_list_reconcile(self):
        """Thread listing cannot rebuild a document after transport loss."""

        await self._authenticate()
        document = await self._register_document(
            resume_client_id="list-race-client",
            incarnation_id="list-race-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")

        disconnect_entered = threading.Event()
        allow_disconnect = threading.Event()
        disconnect_finished = threading.Event()

        def blocking_transport_disconnect(client_id: str) -> None:
            disconnect_entered.set()
            allow_disconnect.wait(timeout=2.0)
            self.transport_disconnected.append(client_id)
            disconnect_finished.set()

        def list_after_disconnect_check(**kwargs):
            if disconnect_finished.is_set():
                raise WebRuntimeError(
                    "This browser document is disconnected.",
                    code="web_writer_disconnected",
                    status=409,
                )
            return {"threads": [], "query": kwargs}

        self.gateway._ports.client_transport_disconnected = (
            blocking_transport_disconnect
        )
        self.gateway._ports.prepare_list_threads = list_after_disconnect_check

        await socket.close()
        self.assertTrue(await asyncio.to_thread(disconnect_entered.wait, 1.0))

        async def list_threads():
            async with self.session.get(
                f"{self.endpoint}/api/threads",
                headers=self._client_headers(document),
            ) as response:
                return response.status, await response.json()

        request_task = asyncio.create_task(list_threads())
        await asyncio.sleep(0.05)
        self.assertFalse(request_task.done())

        allow_disconnect.set()
        status, payload = await request_task
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "web_writer_disconnected")
        await self._wait_until(lambda: bool(self.transport_disconnected))

    async def test_last_socket_disconnect_linearizes_before_history_page(self):
        """History reads cannot overtake document transport teardown."""

        await self._authenticate()
        document = await self._register_document(
            resume_client_id="turns-race-client",
            incarnation_id="turns-race-document",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")

        disconnect_entered = threading.Event()
        allow_disconnect = threading.Event()
        disconnect_finished = threading.Event()

        def blocking_transport_disconnect(client_id: str) -> None:
            disconnect_entered.set()
            allow_disconnect.wait(timeout=2.0)
            self.transport_disconnected.append(client_id)
            disconnect_finished.set()

        def turns_after_disconnect_check(client_id: str, thread_id: str, **kwargs):
            if disconnect_finished.is_set():
                raise WebRuntimeError(
                    "This browser document is disconnected.",
                    code="web_writer_disconnected",
                    status=409,
                )
            return {"client_id": client_id, "thread_id": thread_id, **kwargs}

        self.gateway._ports.client_transport_disconnected = (
            blocking_transport_disconnect
        )
        self.gateway._ports.prepare_list_older_turns = turns_after_disconnect_check

        await socket.close()
        self.assertTrue(await asyncio.to_thread(disconnect_entered.wait, 1.0))

        async def list_turns():
            async with self.session.get(
                f"{self.endpoint}/api/threads/thread-1/turns?cursor=cursor-1",
                headers=self._client_headers(document),
            ) as response:
                return response.status, await response.json()

        request_task = asyncio.create_task(list_turns())
        await asyncio.sleep(0.05)
        self.assertFalse(request_task.done())

        allow_disconnect.set()
        status, payload = await request_task
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "web_writer_disconnected")
        await self._wait_until(lambda: bool(self.transport_disconnected))

    async def test_duplicate_document_with_live_socket_receives_own_identity(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="tab-shared",
            incarnation_id="original-document",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")

        duplicate_document = await self._register_document(
            resume_client_id=original_document["client_id"],
            incarnation_id="duplicated-document",
        )
        self.assertTrue(duplicate_document["duplicate"])
        self.assertNotEqual(
            duplicate_document["client_id"],
            original_document["client_id"],
        )

        impersonation_headers = self._client_headers(
            original_document,
            include_origin=False,
            include_csrf=False,
        )
        impersonation_headers["X-Focus-Web-Client"] = duplicate_document["client_id"]
        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=impersonation_headers,
        ) as response:
            self.assertEqual(response.status, 409)
            payload = await response.json()
        self.assertEqual(payload["error"]["code"], "document_replaced")

        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._client_headers(
                original_document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)

        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._client_headers(
                duplicate_document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)

        await original_socket.close()

    async def test_same_session_resume_hint_is_accepted_only_during_disconnect_grace(
        self,
    ):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="tab-reload",
            incarnation_id="before-reload",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")

        await original_socket.close()
        await self._wait_until(
            lambda: original_document["client_id"] not in self.gateway._websockets
        )
        self.intent_generation_floors[original_document["client_id"]] = 7

        reloaded_document = await self._register_document(
            resume_client_id=original_document["client_id"],
            incarnation_id="after-reload",
        )
        self.assertFalse(reloaded_document["duplicate"])
        self.assertEqual(reloaded_document["client_id"], original_document["client_id"])
        self.assertEqual(reloaded_document["intent_generation_floor"], 7)
        self.assertNotEqual(
            reloaded_document["document_token"],
            original_document["document_token"],
        )
        self.assertNotEqual(
            reloaded_document["document_receipt"],
            original_document["document_receipt"],
        )
        self.assertEqual(
            self.document_reissued,
            [original_document["client_id"]],
        )

        registration_retry = await self._register_document(
            resume_client_id=reloaded_document["client_id"],
            incarnation_id="after-reload",
        )
        self.assertEqual(
            registration_retry["document_token"],
            reloaded_document["document_token"],
        )
        self.assertEqual(registration_retry["intent_generation_floor"], 7)
        self.assertEqual(
            self.document_reissued,
            [original_document["client_id"]],
        )

        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._client_headers(
                original_document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 409)
            payload = await response.json()
        self.assertEqual(payload["error"]["code"], "document_replaced")

        reloaded_socket = await self._connect_events(reloaded_document)
        self.assertEqual((await reloaded_socket.receive_json())["type"], "hello")
        await asyncio.sleep(0.6)
        self.assertNotIn(reloaded_document["client_id"], self.disconnected)

        await reloaded_socket.close()
        await self._wait_until(
            lambda: reloaded_document["client_id"] in self.disconnected
        )
        await self._wait_until(
            lambda: reloaded_document["client_id"] not in self.gateway._client_documents
        )

        after_grace_document = await self._register_document(
            resume_client_id=reloaded_document["client_id"],
            incarnation_id="after-grace-document",
        )
        self.assertFalse(after_grace_document["duplicate"])
        self.assertNotEqual(
            after_grace_document["client_id"],
            reloaded_document["client_id"],
        )
        self.assertEqual(
            self.document_reissued,
            [original_document["client_id"]],
        )

    async def test_document_reissue_linearizes_before_registration_retry(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="linearized-reload",
            incarnation_id="before-linearized-reload",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")
        await original_socket.close()
        await self._wait_until(
            lambda: original_document["client_id"] not in self.gateway._websockets
        )

        reissue_entered = threading.Event()
        allow_reissue = threading.Event()

        def blocking_document_reissue(client_id: str) -> None:
            reissue_entered.set()
            allow_reissue.wait(timeout=2.0)
            self.intent_generation_floors[client_id] = 11
            self.document_reissued.append(client_id)

        self.gateway._ports.client_document_reissued = blocking_document_reissue
        first = asyncio.create_task(
            self._register_document(
                resume_client_id=original_document["client_id"],
                incarnation_id="after-linearized-reload",
            )
        )
        self.assertTrue(await asyncio.to_thread(reissue_entered.wait, 1.0))
        retry = asyncio.create_task(
            self._register_document(
                resume_client_id=original_document["client_id"],
                incarnation_id="after-linearized-reload",
            )
        )
        await asyncio.sleep(0.05)
        self.assertFalse(first.done())
        self.assertFalse(retry.done())

        allow_reissue.set()
        first_document, retry_document = await asyncio.gather(first, retry)

        self.assertEqual(
            first_document["document_token"],
            retry_document["document_token"],
        )
        self.assertEqual(first_document["intent_generation_floor"], 11)
        self.assertEqual(retry_document["intent_generation_floor"], 11)
        self.assertEqual(
            self.document_reissued,
            [original_document["client_id"]],
        )

    async def test_last_socket_finalizer_linearizes_before_document_reissue(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="finalizer-reload",
            incarnation_id="before-finalizer-reload",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")

        disconnect_entered = threading.Event()
        allow_disconnect = threading.Event()
        events: list[str] = []

        def blocking_transport_disconnect(client_id: str) -> None:
            disconnect_entered.set()
            allow_disconnect.wait(timeout=2.0)
            self.transport_disconnected.append(client_id)
            events.append("transport_disconnected")

        def record_document_reissue(client_id: str) -> None:
            self.document_reissued.append(client_id)
            events.append("document_reissued")

        self.gateway._ports.client_transport_disconnected = (
            blocking_transport_disconnect
        )
        self.gateway._ports.client_document_reissued = record_document_reissue

        await original_socket.close()
        self.assertTrue(await asyncio.to_thread(disconnect_entered.wait, 1.0))
        registration = asyncio.create_task(
            self._register_document(
                resume_client_id=original_document["client_id"],
                incarnation_id="after-finalizer-reload",
            )
        )
        await asyncio.sleep(0.05)
        self.assertFalse(registration.done())

        allow_disconnect.set()
        reloaded_document = await registration

        self.assertEqual(
            reloaded_document["client_id"],
            original_document["client_id"],
        )
        self.assertEqual(
            events,
            ["transport_disconnected", "document_reissued"],
        )

    async def test_old_websocket_handshake_cannot_revive_reissued_document(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="handshake-reload",
            incarnation_id="before-handshake-reload",
        )
        prepare_entered = threading.Event()
        allow_prepare = threading.Event()
        original_prepare = web.WebSocketResponse.prepare

        async def blocking_prepare(socket, request):
            prepared = await original_prepare(socket, request)
            if request.query.get("document") == original_document["document_token"]:
                prepare_entered.set()
                await asyncio.to_thread(allow_prepare.wait, 2.0)
            return prepared

        with patch.object(web.WebSocketResponse, "prepare", blocking_prepare):
            old_connection = asyncio.create_task(
                self._connect_events(original_document)
            )
            self.assertTrue(await asyncio.to_thread(prepare_entered.wait, 1.0))

            reloaded_document = await self._register_document(
                resume_client_id=original_document["client_id"],
                incarnation_id="after-handshake-reload",
            )
            self.assertEqual(
                reloaded_document["client_id"],
                original_document["client_id"],
            )
            allow_prepare.set()
            old_socket = await old_connection
            old_message = await old_socket.receive(timeout=1.0)

        self.assertIn(old_message.type, {WSMsgType.CLOSE, WSMsgType.CLOSED})
        self.assertEqual(self.connected, [])
        self.assertNotIn(original_document["client_id"], self.gateway._websockets)

        new_socket = await self._connect_events(reloaded_document)
        self.assertEqual((await new_socket.receive_json())["type"], "hello")
        self.assertEqual(self.connected, [reloaded_document["client_id"]])
        await new_socket.close()

    async def test_grace_cleanup_wins_before_registration_reuses_client_id(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="cleanup-reload",
            incarnation_id="before-cleanup-reload",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")

        cleanup_entered = threading.Event()
        allow_cleanup = threading.Event()

        def blocking_full_disconnect(client_id: str) -> None:
            cleanup_entered.set()
            allow_cleanup.wait(timeout=2.0)
            self.disconnected.append(client_id)

        self.gateway._ports.client_disconnected = blocking_full_disconnect
        await original_socket.close()
        self.assertTrue(await asyncio.to_thread(cleanup_entered.wait, 1.5))

        registration = asyncio.create_task(
            self._register_document(
                resume_client_id=original_document["client_id"],
                incarnation_id="after-cleanup-reload",
            )
        )
        await asyncio.sleep(0.05)
        self.assertFalse(registration.done())

        allow_cleanup.set()
        replacement_document = await registration

        self.assertNotEqual(
            replacement_document["client_id"],
            original_document["client_id"],
        )
        self.assertEqual(self.disconnected, [original_document["client_id"]])
        self.assertEqual(self.document_reissued, [])
        replacement_socket = await self._connect_events(replacement_document)
        self.assertEqual((await replacement_socket.receive_json())["type"], "hello")
        await replacement_socket.close()

    async def test_document_reissue_callback_failure_returns_no_capability(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="failed-reload",
            incarnation_id="before-failed-reload",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")
        await original_socket.close()
        await self._wait_until(
            lambda: original_document["client_id"] not in self.gateway._websockets
        )
        attempts: list[str] = []

        def fail_document_reissue(client_id: str) -> None:
            attempts.append(client_id)
            raise RuntimeError("document reissue failed")

        self.gateway._ports.client_document_reissued = fail_document_reissue
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": original_document["client_id"],
                "incarnation_id": "after-failed-reload",
            },
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 500)
            payload = await response.json()
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(attempts, [original_document["client_id"]])
        self.assertNotIn(
            original_document["client_id"],
            self.gateway._client_documents,
        )

        replacement_document = await self._register_document(
            resume_client_id=original_document["client_id"],
            incarnation_id="replacement-after-failure",
        )
        self.assertNotEqual(
            replacement_document["client_id"],
            original_document["client_id"],
        )
        replacement_socket = await self._connect_events(replacement_document)
        self.assertEqual((await replacement_socket.receive_json())["type"], "hello")
        await replacement_socket.close()

    async def test_reissue_rejects_old_request_blocked_after_token_validation(self):
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="blocked-request-reload",
            incarnation_id="before-blocked-request-reload",
        )
        original_socket = await self._connect_events(original_document)
        self.assertEqual((await original_socket.receive_json())["type"], "hello")
        body_entered = threading.Event()
        allow_body = threading.Event()
        original_json_object = request_decoder.decode_json_object

        async def blocking_json_object(request):
            if request.path.endswith("/prompt"):
                body_entered.set()
                await asyncio.to_thread(allow_body.wait, 2.0)
            return await original_json_object(request)

        async def submit_old_prompt():
            async with self.session.post(
                f"{self.endpoint}/api/threads/thread-1/prompt",
                json=self._prompt_body(
                    original_document,
                    text="must not survive F5",
                ),
                headers=self._client_headers(original_document),
            ) as response:
                return response.status, await response.json()

        with patch(
            "bot.web_runtime.gateway.request_decoder.decode_json_object",
            blocking_json_object,
        ):
            old_request = asyncio.create_task(submit_old_prompt())
            self.assertTrue(await asyncio.to_thread(body_entered.wait, 1.0))
            await original_socket.close()
            await self._wait_until(
                lambda: original_document["client_id"] not in self.gateway._websockets
            )
            reloaded_document = await self._register_document(
                resume_client_id=original_document["client_id"],
                incarnation_id="after-blocked-request-reload",
            )
            reloaded_socket = await self._connect_events(reloaded_document)
            self.assertEqual((await reloaded_socket.receive_json())["type"], "hello")

            allow_body.set()
            status, payload = await old_request

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "document_replaced")
        self.assertEqual(self.calls, [])
        await reloaded_socket.close()

    async def test_revoked_session_rejects_request_blocked_after_authentication(self):
        auth = await self._authenticate()
        document = await self._register_document(
            resume_client_id="blocked-session-request",
            incarnation_id="before-session-revoke",
        )
        socket = await self._connect_events(document)
        self.assertEqual((await socket.receive_json())["type"], "hello")
        body_entered = threading.Event()
        allow_body = threading.Event()
        profile_updates: list[tuple[str, dict]] = []
        original_json_object = request_decoder.decode_json_object

        async def blocking_json_object(request):
            if request.path == "/api/profile":
                body_entered.set()
                await asyncio.to_thread(allow_body.wait, 2.0)
            return await original_json_object(request)

        def update_profile(client_id: str, changes: dict, **_kwargs):
            profile_updates.append((client_id, changes))
            return {"client_id": client_id, "changes": changes}

        async def submit_old_profile_update():
            async with self.session.post(
                f"{self.endpoint}/api/profile",
                json={"model": "stale-model"},
                headers=self._client_headers(document),
            ) as response:
                return response.status, await response.json()

        self.gateway._ports.update_profile = update_profile
        with patch(
            "bot.web_runtime.gateway.request_decoder.decode_json_object",
            blocking_json_object,
        ):
            old_request = asyncio.create_task(submit_old_profile_update())
            self.assertTrue(await asyncio.to_thread(body_entered.wait, 1.0))
            async with self.session.post(
                f"{self.endpoint}/api/auth/logout",
                headers={
                    "Origin": self.endpoint,
                    "X-Focus-Web-Csrf": auth["csrf_token"],
                },
            ) as response:
                self.assertEqual(response.status, 200)

            allow_body.set()
            status, payload = await old_request

        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(profile_updates, [])
        await socket.close()

    async def test_same_document_websocket_reconnect_is_not_a_reissue(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="transport-reconnect",
            incarnation_id="same-document",
        )
        first = await self._connect_events(document)
        self.assertEqual((await first.receive_json())["type"], "hello")
        await first.close()
        await self._wait_until(lambda: bool(self.transport_disconnected))

        second = await self._connect_events(document)
        self.assertEqual((await second.receive_json())["type"], "hello")

        self.assertEqual(self.document_reissued, [])
        await second.close()

    async def test_unconnected_document_registration_does_not_keep_a_resume_hint_alive(
        self,
    ):
        await self._authenticate()
        first_document = await self._register_document(
            resume_client_id="unconnected-tab",
            incarnation_id="unconnected-document",
        )

        await self._wait_until(
            lambda: first_document["client_id"] not in self.gateway._client_documents
        )
        replacement_document = await self._register_document(
            resume_client_id=first_document["client_id"],
            incarnation_id="replacement-document",
        )

        self.assertNotEqual(
            replacement_document["client_id"],
            first_document["client_id"],
        )

    async def test_socket_sender_delivers_events_in_fifo_order(self):
        class FakeSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []

            async def send_json(self, event):
                self.sent.append(dict(event))

            async def close(self, **_kwargs):
                self.closed = True

        socket = FakeSocket()
        state = SimpleNamespace(
            queue=asyncio.Queue(maxsize=4),
            task=None,
            overflowed=False,
        )
        self.gateway._socket_senders[socket] = state
        state.task = asyncio.create_task(self.gateway._run_socket_sender(socket, state))
        try:
            self.gateway._enqueue_projection_event({"type": "first", "revision": 1})
            self.gateway._enqueue_projection_event({"type": "second", "revision": 2})
            for _ in range(20):
                if len(socket.sent) == 2:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(
                [event["type"] for event in socket.sent],
                ["first", "second"],
            )
        finally:
            await self._cleanup_fake_socket(socket, state)

    async def test_socket_queue_overflow_degrades_to_one_invalidation(self):
        class FakeSocket:
            closed = False

        socket = FakeSocket()
        state = SimpleNamespace(
            queue=asyncio.Queue(maxsize=2),
            task=None,
            overflowed=False,
        )
        self.gateway._socket_senders[socket] = state
        self.addCleanup(self.gateway._socket_senders.pop, socket, None)
        state.queue.put_nowait({"type": "first", "revision": 1})
        state.queue.put_nowait({"type": "second", "revision": 2})

        self.gateway._enqueue_projection_event(
            {"type": "third", "runtime_epoch": "epoch-1", "revision": 3}
        )

        self.assertTrue(state.overflowed)
        invalidation = state.queue.get_nowait()
        self.assertEqual(invalidation["type"], "projection_invalidated")
        self.assertEqual(invalidation["reason"], "socket_backpressure")
        self.gateway._enqueue_projection_event({"type": "fourth", "revision": 4})
        self.assertTrue(state.queue.empty())

    async def _cleanup_fake_socket(self, socket, state) -> None:
        socket.closed = True
        self.gateway._socket_senders.pop(socket, None)
        if state.task is not None:
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)

    async def test_origin_must_match_gateway_port(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="origin-document",
        )
        headers = self._client_headers(document)
        headers["Origin"] = "http://127.0.0.1:9"
        async with self.session.post(
            f"{self.endpoint}/api/threads/thread-1/prompt",
            json=self._prompt_body(document),
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 403)
        self.assertEqual(self.calls, [])

    async def test_internal_errors_do_not_expose_exception_text(self):
        await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="internal-error-document",
        )

        def _raise_secret(_client_id):
            raise RuntimeError("secret backend detail")

        self.gateway._ports.meta = _raise_secret
        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._client_headers(
                document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 500)
            payload = await response.json()
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["message"], "Internal server error.")
        self.assertNotIn("secret", str(payload))

    async def test_logout_closes_session_websockets_and_disconnects_client(self):
        auth = await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="logout-document",
        )
        socket = await self._connect_events(document)
        await socket.receive_json()

        async with self.session.post(
            f"{self.endpoint}/api/auth/logout",
            headers={
                "Origin": self.endpoint,
                "X-Focus-Web-Csrf": auth["csrf_token"],
            },
        ) as response:
            self.assertEqual(response.status, 200)

        event = await socket.receive_json(timeout=2)
        self.assertEqual(event["type"], "session_expired")
        await socket.close()
        await self._wait_until(lambda: bool(self.disconnected))
        self.assertEqual(self.disconnected, [document["client_id"]])

    async def test_session_expiry_closes_websocket_and_disconnects_client(self):
        auth = await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-1",
            incarnation_id="expiry-websocket-document",
        )
        socket = await self._connect_events(document)
        await socket.receive_json()
        cookies = self.session.cookie_jar.filter_cookies(URL(self.endpoint))
        session_token = cookies["focus_web_session_default"].value
        expired = WebAuthSession(
            session_token=session_token,
            csrf_token=auth["csrf_token"],
            expires_at=time.time(),
        )
        assert self.gateway._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self.gateway._expire_session_after(expired),
            self.gateway._loop,
        )

        event = await socket.receive_json(timeout=2)
        self.assertEqual(event["type"], "session_expired")
        await socket.close()
        await asyncio.to_thread(future.result, 2)
        self.assertIsNone(self.gateway._auth.authenticate(session_token))
        self.assertEqual(self.disconnected, [document["client_id"]])

    async def test_session_expiry_disconnects_http_only_writer(self):
        auth = await self._authenticate()
        document = await self._register_document(
            resume_client_id="tab-http",
            incarnation_id="expiry-http-document",
        )
        async with self.session.post(
            f"{self.endpoint}/api/threads/thread-1/prompt",
            json=self._prompt_body(document),
            headers=self._client_headers(document),
        ) as response:
            self.assertEqual(response.status, 200)
        cookies = self.session.cookie_jar.filter_cookies(URL(self.endpoint))
        session_token = cookies["focus_web_session_default"].value
        expired = WebAuthSession(
            session_token=session_token,
            csrf_token=auth["csrf_token"],
            expires_at=time.time(),
        )
        assert self.gateway._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self.gateway._expire_session_after(expired),
            self.gateway._loop,
        )

        await asyncio.to_thread(future.result, 2)
        self.assertEqual(self.disconnected, [document["client_id"]])

    async def test_client_id_hint_cannot_cross_authenticated_sessions(self):
        await self._authenticate()
        first_document = await self._register_document(
            resume_client_id="tab-shared",
            incarnation_id="first-session-document",
        )
        async with self.session.get(
            f"{self.endpoint}/api/threads",
            headers=self._client_headers(
                first_document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)

        second_session = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.addAsyncCleanup(second_session.close)
        await self._authenticate_with(second_session)
        second_document = await self._register_document(
            resume_client_id=first_document["client_id"],
            incarnation_id="second-session-document",
            session=second_session,
        )
        self.assertTrue(second_document["duplicate"])
        self.assertNotEqual(second_document["client_id"], first_document["client_id"])

        impersonation_headers = self._client_headers(
            first_document,
            include_origin=False,
            include_csrf=False,
        )
        impersonation_headers["X-Focus-Web-Client"] = second_document["client_id"]
        async with self.session.get(
            f"{self.endpoint}/api/threads",
            headers=impersonation_headers,
        ) as response:
            self.assertEqual(response.status, 409)
            payload = await response.json()
        self.assertEqual(payload["error"]["code"], "document_replaced")

        async with second_session.get(
            f"{self.endpoint}/api/threads",
            headers=self._client_headers(
                second_document,
                include_origin=False,
                include_csrf=False,
            ),
        ) as response:
            self.assertEqual(response.status, 200)


class WebGatewayStartupTests(unittest.TestCase):
    def test_startup_failure_cleans_runner_and_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("Focus Web", encoding="utf-8")
            projection = FocusWebProjection()
            gateway = WebGateway(
                config=WebGatewayConfig(static_dir=static_dir),
                data_dir=root,
                projection=projection,
                ports=WebGatewayPorts(
                    meta=lambda _client_id: {},
                    operator_status=lambda: {},
                    backend_reset_preview=lambda: {},
                    backend_reset_execute=lambda **_kwargs: {},
                    update_profile=lambda _client_id, _changes: {},
                    next_turn_settings=lambda: {},
                    update_next_turn_settings=lambda *_args, **_kwargs: {},
                    stage_attachment=lambda *_args, **_kwargs: {},
                    attachment_download=lambda *_args, **_kwargs: None,
                    prepare_list_threads=lambda **_kwargs: {},
                    prepare_read_thread=lambda *_args: {},
                    prepare_list_older_turns=lambda *_args, **_kwargs: {},
                    run_prepared_thread_read=lambda prepared: prepared,
                    abandon_prepared_thread_read=lambda _prepared: True,
                    prepare_tool_detail=lambda *_args, **_kwargs: {},
                    prepare_conversation_search=lambda *_args, **_kwargs: {},
                    start_thread=lambda *_args, **_kwargs: {},
                    prepare_prompt=lambda *_args, **_kwargs: {},
                    run_prepared_prompt=lambda _prepared: {},
                    abandon_prepared_prompt=lambda _prepared: True,
                    prompt_result=lambda *_args, **_kwargs: {},
                    interrupt=lambda *_args, **_kwargs: {},
                    resolve_unknown_mutation=lambda *_args, **_kwargs: {},
                    rename_thread=lambda *_args, **_kwargs: {},
                    compact_thread=lambda *_args, **_kwargs: {},
                    start_review=lambda *_args, **_kwargs: {},
                    goal=lambda *_args, **_kwargs: {},
                    set_goal=lambda *_args, **_kwargs: {},
                    clear_goal=lambda *_args, **_kwargs: {},
                    archive_thread=lambda *_args, **_kwargs: {},
                    unarchive_thread=lambda *_args, **_kwargs: {},
                    delete_thread=lambda *_args, **_kwargs: {},
                    respond_request=lambda *_args, **_kwargs: {},
                    document_intent_generation_floor=lambda _client_id: 0,
                    client_connected=lambda _client_id: None,
                    client_transport_disconnected=lambda _client_id: None,
                    client_document_reissued=lambda _client_id: None,
                    client_disconnected=lambda _client_id: None,
                ),
            )

            async def _fail_start(_site):
                raise RuntimeError("listen failed")

            with patch("bot.web_runtime.gateway.web.TCPSite.start", _fail_start):
                with self.assertRaisesRegex(RuntimeError, "listen failed"):
                    gateway.start()

            self.assertEqual(gateway.endpoint, "")
            self.assertIsNone(gateway._runner)
            self.assertIsNone(WebGatewayRuntimeStore(root).load())
            gateway.stop()

            runtime_path = root / "web_gateway_runtime.json"
            runtime_path.write_text("{not-json", encoding="utf-8")
            with patch("bot.web_runtime.gateway.web.TCPSite.start", _fail_start):
                with self.assertRaisesRegex(RuntimeError, "listen failed"):
                    gateway.start()

            self.assertEqual(gateway.endpoint, "")
            self.assertIsNone(gateway._runner)
            self.assertTrue(runtime_path.exists())
            with self.assertRaisesRegex(RuntimeError, "web_gateway_runtime.json"):
                WebGatewayRuntimeStore(root).load()
            gateway.stop()


if __name__ == "__main__":
    unittest.main()
