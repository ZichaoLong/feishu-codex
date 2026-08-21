from __future__ import annotations

import asyncio
import hashlib
import pathlib
import secrets
import socket
import tempfile
import threading
import unittest
from dataclasses import replace
from http.cookies import SimpleCookie
from unittest.mock import patch

from aiohttp import ClientSession, DummyCookieJar, web
from aiohttp.client_exceptions import WSServerHandshakeError
from yarl import URL

from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime.projection import FocusWebProjection


class WebGatewayTrustedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = pathlib.Path(self.temp_dir.name)
        static_dir = self.root / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("Focus Web", encoding="utf-8")

        self.external_origin = "https://focus.example.test"
        self.external_authority = "focus.example.test"
        self.proxy_identity = "proxy-user@example.test"
        self.proxy_proof = secrets.token_urlsafe(32)
        self.assertEqual(len(self.proxy_proof), 43)
        self.proxy_proof_sha256 = hashlib.sha256(
            self.proxy_proof.encode("ascii")
        ).hexdigest()
        self.meta_clients: list[str] = []
        self.connected_clients: list[str] = []

        port = self._reserve_loopback_port()
        self.gateway = WebGateway(
            config=WebGatewayConfig(
                port=port,
                static_dir=static_dir,
                session_ttl_seconds=3600,
                disconnect_grace_seconds=0.1,
                trusted_proxy_origin=self.external_origin,
                trusted_proxy_proof_sha256=self.proxy_proof_sha256,
            ),
            data_dir=self.root,
            projection=FocusWebProjection(),
            ports=WebGatewayPorts(
                meta=self._meta,
                operator_status=lambda: {},
                backend_reset_preview=lambda: {"status": "available"},
                backend_reset_execute=lambda **kwargs: dict(kwargs),
                update_profile=lambda *_args, **_kwargs: {},
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
        self.addAsyncCleanup(asyncio.to_thread, self.gateway.stop)
        self.session = ClientSession(cookie_jar=DummyCookieJar())
        self.addAsyncCleanup(self.session.close)

    @staticmethod
    def _reserve_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _meta(self, client_id: str) -> dict[str, object]:
        self.meta_clients.append(client_id)
        return {"product": "Focus"}

    def test_gateway_defensive_config_requires_fixed_proxy_port(self) -> None:
        valid_config = self.gateway._config
        try:
            self.gateway._config = replace(valid_config, port=0)
            with self.assertRaisesRegex(ValueError, "固定非零 web_port"):
                self.gateway._validate_config()
        finally:
            self.gateway._config = valid_config

    def test_gateway_defensive_config_explains_remote_access_paths(self) -> None:
        valid_config = self.gateway._config
        try:
            self.gateway._config = replace(valid_config, host="0.0.0.0")
            with self.assertRaisesRegex(
                ValueError,
                "SSH tunnel.*configured trusted HTTPS proxy",
            ):
                self.gateway._validate_config()
        finally:
            self.gateway._config = valid_config

    def _bootstrap_token(self) -> str:
        runtime = WebGatewayRuntimeStore(self.root).load()
        self.assertIsNotNone(runtime)
        assert runtime is not None
        return runtime.bootstrap_token

    @staticmethod
    def _session_token(set_cookie: str) -> str:
        parsed = SimpleCookie()
        parsed.load(set_cookie)
        return parsed["focus_web_session_default"].value

    @staticmethod
    def _cookie_header(session_token: str) -> str:
        return f"focus_web_session_default={session_token}"

    def _external_headers(
        self,
        *,
        host: str | None = None,
        origin: str | None = None,
        identity: str | None = None,
        proof: str | None = None,
        session_token: str = "",
        include_origin: bool = True,
        include_identity: bool = True,
        include_proof: bool = True,
    ) -> dict[str, str]:
        headers = {"Host": host or self.external_authority}
        if include_origin:
            headers["Origin"] = self.external_origin if origin is None else origin
        if include_proof:
            headers["X-Focus-Trusted-Proxy-Proof"] = (
                self.proxy_proof if proof is None else proof
            )
        if include_identity:
            headers["X-Focus-Trusted-Proxy-Identity"] = (
                self.proxy_identity if identity is None else identity
            )
        if session_token:
            headers["Cookie"] = self._cookie_header(session_token)
        return headers

    @staticmethod
    def _document_headers(
        document: dict[str, object],
        *,
        session_token: str,
    ) -> dict[str, str]:
        return {
            "Cookie": WebGatewayTrustedProxyTests._cookie_header(session_token),
            "X-Focus-Web-Client": str(document["client_id"]),
            "X-Focus-Web-Document": str(document["document_token"]),
        }

    async def _register_external(
        self,
        *,
        identity: str | None = None,
    ) -> tuple[dict[str, object], str, str]:
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": "external-client",
                "incarnation_id": f"external-document-{identity or 'default'}",
            },
            headers=self._external_headers(identity=identity),
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
            set_cookie = response.headers["Set-Cookie"]
        return payload, self._session_token(set_cookie), set_cookie

    async def _authenticate_local(
        self,
        *,
        host: str | None = None,
        origin: str | None = None,
        bootstrap_token: str | None = None,
    ) -> tuple[dict[str, object], str]:
        headers: dict[str, str] = {}
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        elif host is None:
            headers["Origin"] = self.endpoint
        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": bootstrap_token or self._bootstrap_token()},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
            session_token = self._session_token(response.headers["Set-Cookie"])
        return payload, session_token

    async def _register_local(
        self,
        session_token: str,
        *,
        host: str | None = None,
        origin: str | None = None,
    ) -> dict[str, object]:
        headers = {
            "Cookie": self._cookie_header(session_token),
            "Origin": self.endpoint if origin is None else origin,
        }
        if host is not None:
            headers["Host"] = host
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": "local-client",
                "incarnation_id": "local-document",
            },
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    def _events_url(self, document: dict[str, object]) -> str:
        return str(
            URL(f"{self.endpoint}/api/events").with_query(
                client=document["client_id"],
                document=document["document_token"],
                csrf=document["csrf_token"],
            )
        )

    async def test_external_registration_issues_secure_cookie_without_leaks(
        self,
    ) -> None:
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": "external-cookie-client",
                "incarnation_id": "external-cookie-document",
            },
            headers=self._external_headers(),
        ) as response:
            self.assertEqual(response.status, 200)
            response_text = await response.text()
            response_headers = str(response.headers)
            cookie = response.headers["Set-Cookie"]

        self.assertIn("focus_web_session_default=", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn(self.proxy_proof, response_text)
        self.assertNotIn(self.proxy_proof, response_headers)
        self.assertNotIn(self.proxy_identity, response_text)
        self.assertNotIn(self.proxy_identity, response_headers)

        discovery = (self.root / "web_gateway_runtime.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(self.proxy_proof, discovery)
        self.assertNotIn(self.proxy_proof_sha256, discovery)
        self.assertNotIn(self.proxy_identity, discovery)
        self.assertNotIn(self.external_origin, discovery)

    async def test_external_registration_replaces_stale_cookie_for_new_document(
        self,
    ) -> None:
        stale_session = secrets.token_urlsafe(32)
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": "stale-session-client",
                "incarnation_id": "new-document-after-session-loss",
            },
            headers=self._external_headers(session_token=stale_session),
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
            replacement = self._session_token(response.headers["Set-Cookie"])

        self.assertTrue(str(payload["client_id"]).startswith("web-"))
        self.assertNotEqual(replacement, stale_session)
        self.assertIsNone(self.gateway._auth.authenticate(stale_session))
        self.assertIsNotNone(self.gateway._auth.authenticate(replacement))

    async def test_browser_headers_and_forwarding_claims_cannot_forge_proxy(
        self,
    ) -> None:
        wrong_proof = secrets.token_urlsafe(32)
        self.assertNotEqual(wrong_proof, self.proxy_proof)
        forwarded_only = {
            "Host": self.external_authority,
            "Origin": self.external_origin,
            "Forwarded": (
                "for=127.0.0.1;proto=https;host=focus.example.test"
            ),
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Host": self.external_authority,
            "X-Forwarded-Proto": "https",
        }
        cases = (
            ("no proxy headers", self._external_headers(
                include_proof=False,
                include_identity=False,
            ), 403),
            ("wrong proof", self._external_headers(proof=wrong_proof), 403),
            ("identity only", self._external_headers(include_proof=False), 403),
            ("proof only", self._external_headers(include_identity=False), 403),
            ("forwarded only", forwarded_only, 403),
            ("missing identity", self._external_headers(
                include_identity=False,
            ), 403),
            ("oversized identity", self._external_headers(identity="a" * 129), 403),
            ("invalid identity", self._external_headers(identity="browser?claim"), 403),
            ("proxy headers on local Host", {
                "Host": URL(self.endpoint).raw_authority,
                "Origin": self.endpoint,
                "X-Focus-Trusted-Proxy-Proof": self.proxy_proof,
                "X-Focus-Trusted-Proxy-Identity": self.proxy_identity,
            }, 400),
        )
        for label, headers, expected_status in cases:
            with self.subTest(label=label):
                async with self.session.post(
                    f"{self.endpoint}/api/client/register",
                    json={
                        "resume_client_id": "forgery-client",
                        "incarnation_id": "forgery-document",
                    },
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, expected_status)
                    self.assertNotIn("Set-Cookie", response.headers)

        self.assertEqual(self.gateway._auth._sessions, {})
        self.assertEqual(self.gateway._client_sessions, {})

    async def test_external_host_and_origin_match_exact_configured_https_origin(
        self,
    ) -> None:
        cases = (
            (
                "host name",
                self._external_headers(host="other.example.test"),
                400,
            ),
            (
                "host port",
                self._external_headers(host="focus.example.test:8443"),
                400,
            ),
            (
                "explicit default host port",
                self._external_headers(host="focus.example.test:443"),
                400,
            ),
            (
                "host case",
                self._external_headers(host="FOCUS.EXAMPLE.TEST"),
                400,
            ),
            (
                "host userinfo",
                self._external_headers(host="user@focus.example.test"),
                400,
            ),
            (
                "host path",
                self._external_headers(host="focus.example.test/path"),
                400,
            ),
            (
                "origin scheme",
                self._external_headers(origin="http://focus.example.test"),
                403,
            ),
            (
                "origin host",
                self._external_headers(origin="https://other.example.test"),
                403,
            ),
            (
                "origin port",
                self._external_headers(
                    origin="https://focus.example.test:8443"
                ),
                403,
            ),
        )
        for label, headers, expected_status in cases:
            with self.subTest(label=label):
                async with self.session.post(
                    f"{self.endpoint}/api/client/register",
                    json={
                        "resume_client_id": "authority-client",
                        "incarnation_id": "authority-document",
                    },
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, expected_status)
                    self.assertNotIn("Set-Cookie", response.headers)

        self.assertEqual(self.gateway._auth._sessions, {})
        document, _session_token, _cookie = await self._register_external()
        self.assertTrue(str(document["client_id"]).startswith("web-"))

    async def test_external_origin_cannot_consume_local_bootstrap(self) -> None:
        bootstrap_token = self._bootstrap_token()
        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": bootstrap_token},
            headers=self._external_headers(),
        ) as response:
            self.assertEqual(response.status, 403)
            self.assertNotIn("Set-Cookie", response.headers)

        payload, _session_token = await self._authenticate_local(
            bootstrap_token=bootstrap_token
        )
        self.assertTrue(payload["authenticated"])

    async def test_session_audiences_do_not_cross_http_or_websocket(self) -> None:
        _local_auth, local_session = await self._authenticate_local()
        local_document = await self._register_local(local_session)
        external_document, external_session, _cookie = (
            await self._register_external()
        )

        local_headers = self._document_headers(
            local_document,
            session_token=local_session,
        )
        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=local_headers,
        ) as response:
            self.assertEqual(response.status, 200)

        external_headers = self._external_headers(
            session_token=external_session,
        )
        external_headers.update(
            self._document_headers(
                external_document,
                session_token=external_session,
            )
        )
        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=external_headers,
        ) as response:
            self.assertEqual(response.status, 200)

        cross_http_headers = (
            self._document_headers(
                external_document,
                session_token=external_session,
            ),
            {
                **self._external_headers(session_token=local_session),
                **self._document_headers(
                    local_document,
                    session_token=local_session,
                ),
            },
            {
                **self._external_headers(
                    identity="another-proxy-user",
                    session_token=external_session,
                ),
                **self._document_headers(
                    external_document,
                    session_token=external_session,
                ),
            },
        )
        for headers in cross_http_headers:
            with self.subTest(headers=headers):
                async with self.session.get(
                    f"{self.endpoint}/api/meta",
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 401)

        cross_socket_headers = (
            self._document_headers(
                external_document,
                session_token=external_session,
            ),
            {
                **self._external_headers(session_token=local_session),
                **self._document_headers(
                    local_document,
                    session_token=local_session,
                ),
            },
            {
                **self._external_headers(
                    identity="another-proxy-user",
                    session_token=external_session,
                ),
                **self._document_headers(
                    external_document,
                    session_token=external_session,
                ),
            },
        )
        socket_urls = (
            self._events_url(external_document),
            self._events_url(local_document),
            self._events_url(external_document),
        )
        for url, headers in zip(socket_urls, cross_socket_headers, strict=True):
            with self.subTest(url=url, headers=headers):
                with self.assertRaises(WSServerHandshakeError) as caught:
                    await self.session.ws_connect(url, headers=headers)
                self.assertEqual(caught.exception.status, 401)

        valid_socket = await self.session.ws_connect(
            self._events_url(external_document),
            headers=external_headers,
        )
        try:
            self.assertEqual((await valid_socket.receive_json(timeout=2))["type"], "hello")
        finally:
            await valid_socket.close()

    async def test_external_websocket_caps_reject_excess_handshakes(self) -> None:
        document, session_token, _cookie = await self._register_external()
        headers = self._external_headers(session_token=session_token)
        headers.update(
            self._document_headers(document, session_token=session_token)
        )
        events_url = self._events_url(document)

        first_socket = await self.session.ws_connect(events_url, headers=headers)
        try:
            self.assertEqual(
                (await first_socket.receive_json(timeout=2))["type"],
                "hello",
            )
            with patch(
                "bot.web_runtime.gateway._MAX_EXTERNAL_SESSION_SOCKETS",
                1,
            ):
                with self.assertRaises(WSServerHandshakeError) as caught:
                    await self.session.ws_connect(events_url, headers=headers)
                self.assertEqual(caught.exception.status, 429)
        finally:
            await first_socket.close()

        with patch("bot.web_runtime.gateway._MAX_EXTERNAL_SOCKETS", 0):
            with self.assertRaises(WSServerHandshakeError) as caught:
                await self.session.ws_connect(events_url, headers=headers)
            self.assertEqual(caught.exception.status, 429)

    async def test_external_websocket_cap_reserves_inflight_handshake(self) -> None:
        original_config = self.gateway._config
        self.gateway._config = replace(
            original_config,
            disconnect_grace_seconds=5,
        )
        self.addCleanup(setattr, self.gateway, "_config", original_config)
        document, session_token, _cookie = await self._register_external()
        headers = self._external_headers(session_token=session_token)
        headers.update(
            self._document_headers(document, session_token=session_token)
        )
        events_url = self._events_url(document)
        prepare_started = threading.Event()
        release_prepare = threading.Event()
        original_prepare = web.WebSocketResponse.prepare
        prepare_calls = 0

        async def block_first_prepare(socket, request):
            nonlocal prepare_calls
            prepare_calls += 1
            if prepare_calls == 1:
                prepare_started.set()
                await asyncio.to_thread(release_prepare.wait)
            return await original_prepare(socket, request)

        first_connect = None
        first_socket = None
        with (
            patch(
                "bot.web_runtime.gateway._MAX_EXTERNAL_SESSION_SOCKETS",
                1,
            ),
            patch.object(web.WebSocketResponse, "prepare", block_first_prepare),
        ):
            first_connect = asyncio.create_task(
                self.session.ws_connect(events_url, headers=headers)
            )
            self.assertTrue(
                await asyncio.to_thread(prepare_started.wait, 2),
                "first WebSocket handshake did not reach prepare",
            )
            try:
                with self.assertRaises(WSServerHandshakeError) as caught:
                    await self.session.ws_connect(events_url, headers=headers)
                self.assertEqual(caught.exception.status, 429)
            finally:
                release_prepare.set()
            first_socket = await first_connect
            self.assertEqual(
                (await first_socket.receive_json(timeout=2))["type"],
                "hello",
            )

        assert first_socket is not None
        try:
            self.assertFalse(first_socket.closed)
        finally:
            await first_socket.close()

    async def test_local_ssh_forward_port_remap_remains_valid(self) -> None:
        gateway_port = int(URL(self.endpoint).port or 0)
        browser_port = 65535 if gateway_port != 65535 else 65534
        browser_authority = f"127.0.0.1:{browser_port}"
        browser_origin = f"http://{browser_authority}"

        _payload, local_session = await self._authenticate_local(
            host=browser_authority,
            origin=browser_origin,
        )
        document = await self._register_local(
            local_session,
            host=browser_authority,
            origin=browser_origin,
        )
        self.assertTrue(str(document["client_id"]).startswith("web-"))

    async def test_external_public_paths_and_provisional_session_are_bounded(
        self,
    ) -> None:
        async with self.session.get(
            f"{self.endpoint}/api/health",
            headers={"Host": self.external_authority},
        ) as response:
            self.assertEqual(response.status, 403)

        async with self.session.get(
            f"{self.endpoint}/api/health",
            headers=self._external_headers(include_origin=False),
        ) as response:
            self.assertEqual(response.status, 200)
            response_text = await response.text()
            response_headers = str(response.headers)
            self.assertNotIn("Set-Cookie", response.headers)

        self.assertNotIn(self.proxy_proof, response_text)
        self.assertNotIn(self.proxy_proof, response_headers)
        self.assertNotIn(self.proxy_identity, response_text)
        self.assertNotIn(self.proxy_identity, response_headers)

        async with self.session.get(
            f"{self.endpoint}/api/meta",
            headers=self._external_headers(include_origin=False),
        ) as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(self.meta_clients, [])

        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={},
            headers=self._external_headers(),
        ) as response:
            self.assertEqual(response.status, 400)
            self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.gateway._auth._sessions, {})
        self.assertEqual(self.gateway._client_sessions, {})

    async def test_failed_provisional_registration_drops_gateway_client(self) -> None:
        original_config = self.gateway._config
        original_floor = self.gateway._ports.document_intent_generation_floor

        def fail_floor(_client_id: str) -> int:
            raise RuntimeError("intent floor unavailable")

        self.gateway._config = replace(
            original_config,
            disconnect_grace_seconds=0,
        )
        self.gateway._ports.document_intent_generation_floor = fail_floor
        try:
            async with self.session.post(
                f"{self.endpoint}/api/client/register",
                json={
                    "resume_client_id": "failed-external-client",
                    "incarnation_id": "failed-external-document",
                },
                headers=self._external_headers(),
            ) as response:
                self.assertEqual(response.status, 500)
                self.assertNotIn("Set-Cookie", response.headers)
        finally:
            self.gateway._config = original_config
            self.gateway._ports.document_intent_generation_floor = original_floor

        self.assertEqual(self.gateway._auth._sessions, {})
        self.assertEqual(self.gateway._client_sessions, {})
        self.assertEqual(self.gateway._client_documents, {})
        self.assertEqual(self.gateway._session_clients, {})
        self.assertEqual(self.gateway._session_expiry_tasks, {})
        self.assertEqual(self.gateway._disconnect_tasks, {})


if __name__ == "__main__":
    unittest.main()
