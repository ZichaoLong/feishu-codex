from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar

from bot.backend_reset.contract import (
    BackendResetGenerationStaleError,
    BackendResetPolicyRejectedError,
    BackendResetUnavailableError,
)
from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime.projection import FocusWebProjection


class WebBackendResetGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = pathlib.Path(self.temp_dir.name)
        static = root / "static"
        static.mkdir()
        (static / "index.html").write_text("Focus", encoding="utf-8")
        self.preview_calls = 0
        self.execute_calls: list[dict[str, object]] = []
        self.execute_error: BaseException | None = None
        self.gateway = WebGateway(
            config=WebGatewayConfig(
                static_dir=static,
                session_ttl_seconds=3600,
            ),
            data_dir=root,
            projection=FocusWebProjection(),
            ports=WebGatewayPorts(
                meta=lambda _client_id: {},
                operator_status=lambda: {},
                backend_reset_preview=self._preview,
                backend_reset_execute=self._execute,
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
                client_connected=lambda _client_id: None,
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

    def _preview(self) -> dict[str, object]:
        self.preview_calls += 1
        return {
            "instance": "default",
            "status": "available",
            "reason_code": "",
            "reason_text": "safe",
            "expected_connection_generation": 7,
            "pending_request_count": 0,
            "running_binding_count": 0,
            "attached_binding_count": 0,
            "active_loaded_thread_count": 0,
            "loaded_thread_count": 0,
            "runtime_verification_failed": False,
        }

    def _execute(self, **kwargs: object) -> dict[str, object]:
        self.execute_calls.append(dict(kwargs))
        if self.execute_error is not None:
            raise self.execute_error
        return {
            "force": kwargs["force"],
            "detached_binding_count": 0,
            "interrupted_binding_count": 0,
            "retired_request_count": 0,
            "purged_thread_count": 0,
            "projection_warnings": [],
        }

    async def _authenticate_and_register(self) -> dict[str, object]:
        runtime = WebGatewayRuntimeStore(pathlib.Path(self.temp_dir.name)).load()
        self.assertIsNotNone(runtime)
        assert runtime is not None
        async with self.session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": runtime.bootstrap_token},
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={"resume_client_id": "", "incarnation_id": "reset-document"},
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    def _headers(
        self,
        document: dict[str, object],
        *,
        origin: bool = True,
        csrf: bool = True,
    ) -> dict[str, str]:
        headers = {
            "X-Focus-Web-Client": str(document["client_id"]),
            "X-Focus-Web-Document": str(document["document_token"]),
        }
        if origin:
            headers["Origin"] = self.endpoint
        if csrf:
            headers["X-Focus-Web-Csrf"] = str(document["csrf_token"])
        return headers

    async def test_preview_requires_auth_and_current_document_but_not_csrf(self) -> None:
        async with self.session.get(f"{self.endpoint}/api/backend-reset") as response:
            self.assertEqual(response.status, 401)

        document = await self._authenticate_and_register()
        async with self.session.get(
            f"{self.endpoint}/api/backend-reset",
            headers=self._headers(document, origin=False, csrf=False),
        ) as response:
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        self.assertEqual(payload["expected_connection_generation"], 7)
        self.assertEqual(self.preview_calls, 1)

    async def test_execute_requires_origin_csrf_and_exact_body(self) -> None:
        document = await self._authenticate_and_register()
        valid = {"force": False, "expected_connection_generation": 7}
        for headers in (
            self._headers(document, origin=False),
            self._headers(document, csrf=False),
        ):
            async with self.session.post(
                f"{self.endpoint}/api/backend-reset",
                json=valid,
                headers=headers,
            ) as response:
                self.assertEqual(response.status, 403)
        self.assertEqual(self.execute_calls, [])

        invalid = (
            {},
            {"force": False, "expected_connection_generation": 7, "instance": "default"},
            {"force": 0, "expected_connection_generation": 7},
            {"force": False, "expected_connection_generation": 0},
        )
        for body in invalid:
            with self.subTest(body=body):
                async with self.session.post(
                    f"{self.endpoint}/api/backend-reset",
                    json=body,
                    headers=self._headers(document),
                ) as response:
                    self.assertEqual(response.status, 400)
                    payload = await response.json()
                    self.assertEqual(
                        payload["error"]["code"],
                        "invalid_backend_reset_request",
                    )
        self.assertEqual(self.execute_calls, [])

        async with self.session.post(
            f"{self.endpoint}/api/backend-reset",
            json=valid,
            headers=self._headers(document),
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            await response.json()
        self.assertEqual(
            self.execute_calls,
            [{"force": False, "expected_connection_generation": 7}],
        )

    async def test_reissued_document_rejects_old_execute_without_dispatch(self) -> None:
        old_document = await self._authenticate_and_register()
        async with self.session.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": old_document["client_id"],
                "incarnation_id": "replacement-reset-document",
            },
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            replacement = await response.json()
        self.assertEqual(replacement["client_id"], old_document["client_id"])

        async with self.session.post(
            f"{self.endpoint}/api/backend-reset",
            json={"force": False, "expected_connection_generation": 7},
            headers=self._headers(old_document),
        ) as response:
            self.assertEqual(response.status, 409)
            payload = await response.json()

        self.assertEqual(payload["error"]["code"], "document_replaced")
        self.assertEqual(self.execute_calls, [])

    async def test_known_no_effect_conflicts_are_typed_and_never_retried(self) -> None:
        document = await self._authenticate_and_register()
        cases = (
            (
                BackendResetGenerationStaleError(
                    expected_generation=7,
                    observed_generation=8,
                    source="physical",
                ),
                "backend_reset_stale",
            ),
            (BackendResetUnavailableError("closed"), "backend_reset_unavailable"),
            (
                BackendResetPolicyRejectedError("force is now required"),
                "backend_reset_policy_changed",
            ),
        )
        for error, code in cases:
            with self.subTest(code=code):
                self.execute_calls.clear()
                self.execute_error = error
                async with self.session.post(
                    f"{self.endpoint}/api/backend-reset",
                    json={"force": False, "expected_connection_generation": 7},
                    headers=self._headers(document),
                ) as response:
                    self.assertEqual(response.status, 409)
                    payload = await response.json()
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(len(self.execute_calls), 1)

    async def test_post_dispatch_failure_is_generic_unknown_boundary(self) -> None:
        document = await self._authenticate_and_register()
        self.execute_error = RuntimeError("secret backend URL ws://token@example")

        async with self.session.post(
            f"{self.endpoint}/api/backend-reset",
            json={"force": True, "expected_connection_generation": 7},
            headers=self._headers(document),
        ) as response:
            self.assertEqual(response.status, 500)
            body = await response.text()
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        self.assertEqual(len(self.execute_calls), 1)
        self.assertNotIn("secret", body)
        self.assertNotIn("ws://", body)
        self.assertIn("internal_error", body)


if __name__ == "__main__":
    unittest.main()
