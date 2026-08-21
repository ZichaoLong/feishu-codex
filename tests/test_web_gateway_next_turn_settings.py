from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest

from aiohttp import ClientSession, CookieJar

from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime.projection import FocusWebProjection


class WebGatewayNextTurnSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = pathlib.Path(self.temp_dir.name)
        static_dir = root / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("Focus", encoding="utf-8")
        self.projection = FocusWebProjection()
        self.reads: list[bool] = []
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.gateway = WebGateway(
            config=WebGatewayConfig(
                static_dir=static_dir,
                session_ttl_seconds=3600,
            ),
            data_dir=root,
            projection=self.projection,
            ports=WebGatewayPorts(
                meta=lambda _client_id: {},
                operator_status=lambda: {},
                backend_reset_preview=lambda: {},
                backend_reset_execute=lambda **_kwargs: {},
                update_profile=lambda *_args, **_kwargs: {},
                next_turn_settings=self._read_settings,
                update_next_turn_settings=self._update_settings,
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

    def _read_settings(self) -> dict[str, object]:
        self.reads.append(True)
        return {
            **self.projection.coordinates(),
            "next_turn_settings": {
                "generation": 4,
                "model": "gpt-test",
                "reasoning_effort": "high",
                "approval_policy": "never",
                "permissions_profile_id": ":workspace",
            },
        }

    def _update_settings(
        self,
        client_id: str,
        changes: dict[str, object],
    ) -> dict[str, object]:
        self.updates.append((client_id, changes))
        return {
            **self.projection.coordinates(),
            "next_turn_settings": {
                "generation": 5,
                "model": str(changes.get("model", "")),
                "reasoning_effort": "high",
                "approval_policy": "never",
                "permissions_profile_id": ":workspace",
            },
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
            json={
                "resume_client_id": "settings-document",
                "incarnation_id": "settings-routes",
            },
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    def _headers(
        self,
        document: dict[str, object],
        *,
        csrf: bool,
    ) -> dict[str, str]:
        headers = {
            "Origin": self.endpoint,
            "X-Focus-Web-Client": str(document["client_id"]),
            "X-Focus-Web-Document": str(document["document_token"]),
        }
        if csrf:
            headers["X-Focus-Web-Csrf"] = str(document["csrf_token"])
        return headers

    async def test_dedicated_document_routes_do_not_consume_navigation_intent(
        self,
    ) -> None:
        document = await self._authenticate_and_register()
        async with self.session.get(
            f"{self.endpoint}/api/settings/next-turn",
            headers=self._headers(document, csrf=False),
        ) as response:
            self.assertEqual(response.status, 200)
            read_payload = await response.json()
        update_headers = self._headers(document, csrf=True)
        update_headers["X-Focus-Web-Intent"] = "999"
        async with self.session.post(
            f"{self.endpoint}/api/settings/next-turn",
            json={"model": "gpt-next"},
            headers=update_headers,
        ) as response:
            self.assertEqual(response.status, 200)
            update_payload = await response.json()

        self.assertEqual(self.reads, [True])
        self.assertEqual(
            self.updates,
            [(document["client_id"], {"model": "gpt-next"})],
        )
        self.assertEqual(read_payload["next_turn_settings"]["generation"], 4)
        self.assertEqual(update_payload["next_turn_settings"]["generation"], 5)
