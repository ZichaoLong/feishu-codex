from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar
from aiohttp.client_exceptions import WSServerHandshakeError
from yarl import URL

from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime.projection import FocusWebProjection


class WebGatewayRequestAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name)
        static_dir = self.root / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("Focus Web", encoding="utf-8")

        self.projection = FocusWebProjection()
        self.meta_clients: list[str] = []
        self.profile_updates: list[tuple[str, dict[str, object], int]] = []
        self.connected_clients: list[str] = []
        self.gateway = WebGateway(
            config=WebGatewayConfig(
                static_dir=static_dir,
                session_ttl_seconds=3600,
                # Request-admission assertions must not race the unrelated
                # initial document-retirement timer.
                disconnect_grace_seconds=0,
            ),
            data_dir=self.root,
            projection=self.projection,
            ports=WebGatewayPorts(
                meta=self._meta,
                operator_status=lambda: {},
                backend_reset_preview=lambda: {"status": "available"},
                backend_reset_execute=lambda **kwargs: dict(kwargs),
                update_profile=self._update_profile,
                next_turn_settings=lambda: {},
                update_next_turn_settings=lambda *_args, **_kwargs: {},
                stage_attachment=lambda *_args, **_kwargs: {},
                attachment_download=lambda *_args, **_kwargs: None,
                prepare_list_threads=lambda **_kwargs: {},
                prepare_read_thread=lambda *_args, **_kwargs: {},
                prepare_list_older_turns=lambda *_args, **_kwargs: {},
                run_prepared_thread_read=lambda prepared: prepared,
                abandon_prepared_thread_read=lambda _prepared: True,
                prepare_tool_detail=lambda *_args, **_kwargs: {},
                prepare_conversation_search=lambda *_args, **_kwargs: {},
                start_thread=lambda *_args, **_kwargs: {},
                prepare_prompt=lambda *_args, **_kwargs: object(),
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
                client_connected=self.connected_clients.append,
                client_transport_disconnected=lambda _client_id: None,
                client_document_reissued=lambda _client_id: None,
                client_disconnected=lambda _client_id: None,
            ),
        )
        self.endpoint = self.gateway.start()
        self.session = ClientSession(cookie_jar=CookieJar(unsafe=True))

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await asyncio.to_thread(self.gateway.stop)

    def _meta(self, client_id: str) -> dict[str, object]:
        self.meta_clients.append(client_id)
        return {"product": "Focus"}

    def _update_profile(
        self,
        client_id: str,
        changes: dict[str, object],
        *,
        intent_generation: int,
    ) -> dict[str, object]:
        self.profile_updates.append((client_id, changes, intent_generation))
        return {"accepted": True}

    def _bootstrap_token(self) -> str:
        runtime = WebGatewayRuntimeStore(self.root).load()
        self.assertIsNotNone(runtime)
        assert runtime is not None
        return runtime.bootstrap_token

    async def _authenticate(self) -> dict[str, object]:
        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": self._bootstrap_token()},
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def _register_document(self) -> dict[str, object]:
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": "request-admission-client",
                "incarnation_id": "request-admission-document",
            },
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    def _document_headers(
        self,
        document: dict[str, object],
        *,
        origin: str | None = None,
        csrf: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "X-Focus-Web-Client": str(document["client_id"]),
            "X-Focus-Web-Document": str(document["document_token"]),
        }
        if origin is not None:
            headers["Origin"] = origin
        if csrf is not None:
            headers["X-Focus-Web-Csrf"] = csrf
        return headers

    def _events_url(
        self,
        document: dict[str, object],
        *,
        csrf: str,
        document_token: str | None = None,
    ) -> str:
        return str(
            URL(f"{self.endpoint}/api/events").with_query(
                client=document["client_id"],
                document=(
                    document["document_token"]
                    if document_token is None
                    else document_token
                ),
                csrf=csrf,
            )
        )

    async def _authenticate_and_register(self) -> dict[str, object]:
        await self._authenticate()
        return await self._register_document()

    async def test_protected_api_requires_an_active_session(self) -> None:
        async with self.session.get(f"{self.endpoint}/api/meta") as response:
            self.assertEqual(response.status, 401)
            self.assertEqual(response.content_type, "application/json")
            payload = await response.json()

        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(self.meta_clients, [])

    async def test_authenticated_safe_get_needs_no_origin_or_csrf(self) -> None:
        document = await self._authenticate_and_register()

        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._document_headers(document),
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()

        self.assertEqual(payload["product"], "Focus")
        self.assertEqual(self.meta_clients, [document["client_id"]])

    async def test_one_browser_keeps_two_loopback_instance_sessions(self) -> None:
        default_document = await self._authenticate_and_register()
        explorer_root = self.root / "explorer"
        explorer_root.mkdir()
        explorer_gateway = WebGateway(
            config=WebGatewayConfig(
                instance_name="explorer",
                static_dir=self.gateway._config.static_dir,
                session_ttl_seconds=3600,
                disconnect_grace_seconds=0,
            ),
            data_dir=explorer_root,
            projection=FocusWebProjection(),
            ports=self.gateway._ports,
        )
        explorer_endpoint = explorer_gateway.start()
        try:
            explorer_runtime = WebGatewayRuntimeStore(explorer_root).load()
            self.assertIsNotNone(explorer_runtime)
            assert explorer_runtime is not None
            async with self.session.post(
                f"{explorer_endpoint}/api/auth/bootstrap",
                json={"token": explorer_runtime.bootstrap_token},
                headers={"Origin": explorer_endpoint},
            ) as response:
                self.assertEqual(response.status, 200)
            async with self.session.post(
                f"{explorer_endpoint}/api/client/register",
                json={
                    "resume_client_id": "explorer-client",
                    "incarnation_id": "explorer-document",
                },
                headers={"Origin": explorer_endpoint},
            ) as response:
                self.assertEqual(response.status, 200)
                explorer_document = await response.json()

            cookie_names = {cookie.key for cookie in self.session.cookie_jar}
            self.assertIn("focus_web_session_default", cookie_names)
            self.assertIn("focus_web_session_explorer", cookie_names)

            for endpoint, document in (
                (self.endpoint, default_document),
                (explorer_endpoint, explorer_document),
                (self.endpoint, default_document),
            ):
                async with self.session.get(
                    f"{endpoint}/api/meta",
                    headers=self._document_headers(document),
                ) as response:
                    self.assertEqual(response.status, 200)
        finally:
            await asyncio.to_thread(explorer_gateway.stop)

    async def test_mutation_requires_matching_origin_and_csrf(self) -> None:
        document = await self._authenticate_and_register()
        csrf = str(document["csrf_token"])
        url = f"{self.endpoint}/api/profile"
        body = {"working_dir": "/work/project"}

        cases = (
            (
                self._document_headers(document, csrf=csrf),
                403,
                None,
            ),
            (
                self._document_headers(
                    document,
                    origin="http://127.0.0.1:9",
                    csrf=csrf,
                ),
                403,
                None,
            ),
            (
                self._document_headers(document, origin=self.endpoint),
                403,
                "csrf_failed",
            ),
            (
                self._document_headers(
                    document,
                    origin=self.endpoint,
                    csrf="wrong-csrf",
                ),
                403,
                "csrf_failed",
            ),
        )
        for headers, expected_status, expected_code in cases:
            with self.subTest(headers=headers):
                async with self.session.post(
                    url,
                    json=body,
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, expected_status)
                    if expected_code is not None:
                        self.assertEqual(
                            (await response.json())["error"]["code"],
                            expected_code,
                        )
        self.assertEqual(self.profile_updates, [])

        async with self.session.post(
            url,
            json=body,
            headers=self._document_headers(
                document,
                origin=self.endpoint,
                csrf=csrf,
            ),
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(
            self.profile_updates,
            [(document["client_id"], body, 0)],
        )

    async def test_registration_is_the_only_session_origin_mutation_without_csrf(
        self,
    ) -> None:
        body = {
            "resume_client_id": "registration-client",
            "incarnation_id": "registration-document",
        }
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json=body,
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 401)

        await self._authenticate()
        for headers in (
            {},
            {"Origin": "http://127.0.0.1:9"},
        ):
            with self.subTest(headers=headers):
                async with self.session.post(
                    f"{self.endpoint}/api/client/register",
                    json=body,
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 403)

        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json=body,
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            document = await response.json()

        self.assertTrue(str(document["client_id"]).startswith("web-"))

    async def test_bootstrap_allows_missing_origin_but_rejects_cross_origin(
        self,
    ) -> None:
        token = self._bootstrap_token()
        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": token},
            headers={"Origin": "http://127.0.0.1:9"},
        ) as response:
            self.assertEqual(response.status, 403)

        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": token},
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()

        self.assertTrue(payload["authenticated"])

    async def test_non_loopback_host_is_rejected(self) -> None:
        async with self.session.get(
            f"{self.endpoint}/api/health",
            headers={"Host": "focus.example.test"},
        ) as response:
            self.assertEqual(response.status, 400)
            self.assertIn("loopback Host", await response.text())

    async def test_websocket_requires_session_origin_csrf_and_document(self) -> None:
        document = await self._authenticate_and_register()
        csrf = str(document["csrf_token"])

        anonymous = ClientSession(cookie_jar=CookieJar(unsafe=True))
        self.addAsyncCleanup(anonymous.close)
        with self.assertRaises(WSServerHandshakeError) as caught:
            await anonymous.ws_connect(
                self._events_url(document, csrf=csrf),
                headers={"Origin": self.endpoint},
            )
        self.assertEqual(caught.exception.status, 401)

        for url, origin, expected_status in (
            (
                self._events_url(document, csrf="wrong-csrf"),
                self.endpoint,
                403,
            ),
            (
                self._events_url(document, csrf=csrf),
                "http://127.0.0.1:9",
                403,
            ),
            (
                self._events_url(
                    document,
                    csrf=csrf,
                    document_token="wrong-document-token",
                ),
                self.endpoint,
                409,
            ),
        ):
            with self.subTest(url=url, origin=origin):
                with self.assertRaises(WSServerHandshakeError) as caught:
                    await self.session.ws_connect(
                        url,
                        headers={"Origin": origin},
                    )
                self.assertEqual(caught.exception.status, expected_status)

        socket = await self.session.ws_connect(
            self._events_url(document, csrf=csrf),
            headers={"Origin": self.endpoint},
        )
        try:
            hello = await socket.receive_json(timeout=2)
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(self.connected_clients, [document["client_id"]])
        finally:
            await socket.close()

    async def test_local_bootstrap_cookie_is_http_only_strict_and_not_secure(
        self,
    ) -> None:
        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": self._bootstrap_token()},
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            cookie = response.headers["Set-Cookie"]

        self.assertIn("focus_web_session_default=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=", cookie)
        self.assertNotIn("Secure", cookie)

    async def test_success_response_has_security_headers(self) -> None:
        async with self.session.get(f"{self.endpoint}/api/health") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(
                response.headers["Permissions-Policy"],
                "camera=(), microphone=(), geolocation=()",
            )
            content_security_policy = response.headers["Content-Security-Policy"]

        self.assertIn("default-src 'self'", content_security_policy)
        self.assertIn("frame-ancestors 'none'", content_security_policy)
        self.assertIn("connect-src 'self'", content_security_policy)

    async def test_internal_error_response_does_not_expose_exception_text(self) -> None:
        document = await self._authenticate_and_register()

        def raise_secret(_client_id: str) -> dict[str, object]:
            raise RuntimeError("secret backend detail")

        self.gateway._ports.meta = raise_secret
        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._document_headers(document),
        ) as response:
            self.assertEqual(response.status, 500)
            payload = await response.json()

        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["message"], "Internal server error.")
        self.assertNotIn("secret", str(payload))

    async def test_backend_reset_responses_are_never_cached(self) -> None:
        document = await self._authenticate_and_register()
        csrf = str(document["csrf_token"])
        safe_headers = self._document_headers(document)
        mutation_headers = self._document_headers(
            document,
            origin=self.endpoint,
            csrf=csrf,
        )

        async with self.session.get(
            f"{self.endpoint}/api/backend-reset",
            headers=safe_headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        async with self.session.post(
            f"{self.endpoint}/api/backend-reset",
            json={"force": False, "expected_connection_generation": 7},
            headers=mutation_headers,
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        def reject_preview() -> dict[str, object]:
            raise WebRuntimeError(
                "Reset preview unavailable.",
                code="reset_unavailable",
                status=409,
            )

        self.gateway._ports.backend_reset_preview = reject_preview
        async with self.session.get(
            f"{self.endpoint}/api/backend-reset",
            headers=safe_headers,
        ) as response:
            self.assertEqual(response.status, 409)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        def fail_preview() -> dict[str, object]:
            raise RuntimeError("secret reset detail")

        self.gateway._ports.backend_reset_preview = fail_preview
        async with self.session.get(
            f"{self.endpoint}/api/backend-reset",
            headers=safe_headers,
        ) as response:
            self.assertEqual(response.status, 500)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            payload = await response.json()

        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertNotIn("secret", str(payload))


if __name__ == "__main__":
    unittest.main()
