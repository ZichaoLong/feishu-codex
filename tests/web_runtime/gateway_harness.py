"""Reusable live-Gateway fixture without reusable test cases."""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from aiohttp import ClientSession, CookieJar
from yarl import URL

from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime.projection import FocusWebProjection


class WebGatewayHarness(unittest.IsolatedAsyncioTestCase):
    """Start one authenticated live Gateway for focused route regressions."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = pathlib.Path(self.temp_dir.name)
        self.root = root
        self.attachment_record = None
        static_dir = root / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text(
            "<html><body>Focus Web</body></html>",
            encoding="utf-8",
        )
        (static_dir / "THIRD_PARTY_NOTICES.html").write_text(
            "<html><body>Third-party notices</body></html>",
            encoding="utf-8",
        )
        (static_dir / "THIRD_PARTY_NOTICES.md").write_text(
            "# Third-party notices\n",
            encoding="utf-8",
        )
        (static_dir / "THIRD_PARTY_SBOM.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        self.calls: list[tuple] = []
        self.prompt_results: dict[str, dict[str, str]] = {}
        self.connected: list[str] = []
        self.transport_disconnected: list[str] = []
        self.document_reissued: list[str] = []
        self.disconnected: list[str] = []
        self.intent_generation_floors: dict[str, int] = {}
        self.operator_status_threads: list[str] = []
        self.projection = FocusWebProjection()
        self.gateway = WebGateway(
            config=WebGatewayConfig(
                static_dir=static_dir,
                # Keep reconnect tests fast while preserving a non-zero grace.
                disconnect_grace_seconds=0.5,
                session_ttl_seconds=3600,
            ),
            data_dir=root,
            projection=self.projection,
            ports=WebGatewayPorts(
                meta=lambda _client_id: {
                    "product": "Focus",
                    **self.projection.coordinates(),
                },
                operator_status=self._operator_status,
                backend_reset_preview=lambda: {},
                backend_reset_execute=lambda **_kwargs: {},
                update_profile=lambda client_id, changes: {
                    "client_id": client_id,
                    "changes": changes,
                },
                next_turn_settings=lambda: {
                    "next_turn_settings": {
                        "generation": 1,
                        "model": "",
                        "reasoning_effort": "",
                        "approval_policy": "never",
                        "permissions_profile_id": ":danger-full-access",
                    },
                    **self.projection.coordinates(),
                },
                update_next_turn_settings=lambda client_id, changes: {
                    "client_id": client_id,
                    "changes": changes,
                },
                stage_attachment=self._stage_attachment,
                attachment_download=self._attachment_download,
                prepare_list_threads=lambda **kwargs: (
                    "list",
                    {"threads": [], "query": kwargs},
                ),
                prepare_read_thread=lambda client_id, thread_id, **kwargs: (
                    "read",
                    {
                        "client_id": client_id,
                        "thread": {"id": thread_id},
                        **kwargs,
                    },
                ),
                prepare_list_older_turns=lambda client_id, thread_id, **kwargs: (
                    "history",
                    {
                        "client_id": client_id,
                        "thread_id": thread_id,
                        **kwargs,
                    },
                ),
                run_prepared_thread_read=lambda prepared: prepared[1],
                abandon_prepared_thread_read=lambda _prepared: True,
                prepare_tool_detail=lambda *args, **kwargs: (
                    "inspection",
                    {"args": args, "kwargs": kwargs},
                ),
                prepare_conversation_search=lambda *args, **kwargs: (
                    "inspection",
                    {"args": args, "kwargs": kwargs},
                ),
                start_thread=lambda client_id, **kwargs: {
                    "accepted": True,
                    "thread_id": "new-thread",
                    "turn_id": "turn-new",
                    "client_id": client_id,
                    "input": kwargs,
                },
                prepare_prompt=self._prepare_prompt,
                run_prepared_prompt=self._run_prepared_prompt,
                abandon_prepared_prompt=self._abandon_prepared_prompt,
                prompt_result=self._prompt_result,
                interrupt=self._interrupt,
                resolve_unknown_mutation=lambda *args, **kwargs: {
                    "accepted": True,
                    "args": args,
                    "kwargs": kwargs,
                },
                rename_thread=lambda *args, **kwargs: {
                    "accepted": True,
                    "args": args,
                    "kwargs": kwargs,
                },
                compact_thread=lambda *args, **kwargs: {
                    "accepted": True,
                    "args": args,
                    "kwargs": kwargs,
                },
                start_review=lambda *args, **kwargs: {
                    "accepted": True,
                    "args": args,
                    "kwargs": kwargs,
                },
                goal=lambda *args, **kwargs: {
                    "goal": None,
                    "args": args,
                    "kwargs": kwargs,
                },
                set_goal=lambda *args, **kwargs: {
                    "goal": {},
                    "args": args,
                    "kwargs": kwargs,
                },
                clear_goal=self._clear_goal,
                archive_thread=lambda *args, **kwargs: {
                    "upstream_outcome": "success",
                    "args": args,
                },
                unarchive_thread=lambda *args, **kwargs: {
                    "upstream_outcome": "success",
                    "args": args,
                },
                delete_thread=lambda *args, **kwargs: {
                    "upstream_outcome": "success",
                    "args": args,
                },
                respond_request=lambda *args, **kwargs: {
                    "accepted": True,
                    "args": args,
                    "kwargs": kwargs,
                },
                document_intent_generation_floor=(
                    self._document_intent_generation_floor
                ),
                client_connected=self.connected.append,
                client_transport_disconnected=self.transport_disconnected.append,
                client_document_reissued=self.document_reissued.append,
                client_disconnected=self.disconnected.append,
            ),
        )
        self.endpoint = self.gateway.start()
        self.session = ClientSession(cookie_jar=CookieJar(unsafe=True))

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await asyncio.to_thread(self.gateway.stop)

    def _operator_status(self) -> dict[str, object]:
        self.operator_status_threads.append(threading.current_thread().name)
        return {
            "status": "ok",
            "observed_at": time.time(),
            "poll_after_seconds": 15.0,
            "warnings": [],
            "runtime_loop": {},
        }

    def _document_intent_generation_floor(self, client_id: str) -> int:
        lock = self.gateway._client_operation_locks.get(client_id)
        self.assertIsNotNone(lock)
        self.assertTrue(lock and lock.locked())
        return self.intent_generation_floors.get(client_id, 0)

    def _prepare_prompt(self, *args, **kwargs):
        return SimpleNamespace(args=args, kwargs=kwargs)

    def _run_prepared_prompt(self, prepared):
        return self._start_prompt(*prepared.args, **prepared.kwargs)

    def _abandon_prepared_prompt(self, prepared):
        self.calls.append(("abandon_prompt", prepared))
        return True

    def _start_prompt(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        mutation_id = kwargs["mutation_id"]
        result = {
            "thread_id": args[1],
            "mutation_id": mutation_id,
            "client_user_message_id": f"focus-web:{mutation_id}",
            "status": "succeeded",
            "mode": "start",
            "turn_id": "turn-1",
            "reason_code": "",
        }
        self.prompt_results[mutation_id] = result
        return result

    def _prompt_result(self, *args, **kwargs):
        self.calls.append(("prompt_result", args, kwargs))
        return self.prompt_results[kwargs["mutation_id"]]

    def _interrupt(self, *args, **kwargs):
        self.calls.append(("interrupt", args, kwargs))
        return {
            "accepted": True,
            "thread_id": args[1],
            "turn_id": kwargs["turn_id"],
        }

    def _clear_goal(self, *args, **kwargs):
        self.calls.append(("clear_goal", args, kwargs))
        return {"goal": None, "args": args, "kwargs": kwargs}

    def _stage_attachment(self, client_id, **kwargs):
        path = self.root / "uploaded.bin"
        path.write_bytes(kwargs["content"])
        self.attachment_record = SimpleNamespace(
            local_path=str(path),
            media_type=kwargs["media_type"],
            display_name=kwargs["display_name"],
        )
        self.calls.append(((client_id,), kwargs))
        return {
            "file_id": "attachment-1",
            "name": kwargs["display_name"],
            "media_type": kwargs["media_type"],
            "size": len(kwargs["content"]),
            "url": "/api/attachments/attachment-1",
        }

    def _attachment_download(self, attachment_id):
        self.assertEqual(attachment_id, "attachment-1")
        if self.attachment_record is None:
            raise AssertionError("attachment was not staged")
        return SimpleNamespace(
            record=self.attachment_record,
            content=pathlib.Path(self.attachment_record.local_path).read_bytes(),
        )

    async def _authenticate(self):
        return await self._authenticate_with(self.session)

    async def _authenticate_with(self, session: ClientSession):
        runtime = WebGatewayRuntimeStore(pathlib.Path(self.temp_dir.name)).load()
        self.assertIsNotNone(runtime)
        assert runtime is not None
        async with session.post(
            f"{self.endpoint}/api/auth/bootstrap",
            json={"token": runtime.bootstrap_token},
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def _register_document(
        self,
        *,
        resume_client_id: str = "",
        incarnation_id: str,
        session: ClientSession | None = None,
    ) -> dict:
        browser = self.session if session is None else session
        async with browser.post(
            f"{self.endpoint}/api/client/register",
            json={
                "resume_client_id": resume_client_id,
                "incarnation_id": incarnation_id,
            },
            headers={"Origin": self.endpoint},
        ) as response:
            self.assertEqual(response.status, 200)
            document = await response.json()
        self.assertRegex(str(document.get("document_receipt", "")), r"[0-9a-f]{64}")
        self.assertIsInstance(document.get("intent_generation_floor"), int)
        self.assertEqual(
            document["document_receipt"],
            hashlib.sha256(document["document_token"].encode()).hexdigest(),
        )
        return document

    def _client_headers(
        self,
        document: dict,
        *,
        include_origin: bool = True,
        include_csrf: bool = True,
        intent_generation: int | None = None,
    ) -> dict[str, str]:
        headers = {
            "X-Focus-Web-Client": str(document["client_id"]),
            "X-Focus-Web-Document": str(document["document_token"]),
        }
        if include_origin:
            headers["Origin"] = self.endpoint
        if include_csrf:
            headers["X-Focus-Web-Csrf"] = str(document["csrf_token"])
        if intent_generation is not None:
            headers["X-Focus-Web-Intent"] = str(intent_generation)
        return headers

    def _events_url(self, document: dict) -> str:
        return str(
            URL(f"{self.endpoint}/api/events").with_query(
                client=document["client_id"],
                document=document["document_token"],
                csrf=document["csrf_token"],
            )
        )

    async def _connect_events(
        self,
        document: dict,
        *,
        session: ClientSession | None = None,
    ):
        browser = self.session if session is None else session
        return await browser.ws_connect(
            self._events_url(document),
            headers={"Origin": self.endpoint},
        )

    async def _wait_until(self, predicate, *, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Timed out waiting for asynchronous Gateway state.")
            await asyncio.sleep(0.01)
