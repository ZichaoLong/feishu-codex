"""Reusable owner-level fixture for Web runtime controller tests."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
import uuid

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import CodexRpcPreSendError
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import ServerRequestIdentity, ServerRequestRoutingMode
from bot.server_request_registry import ServerRequestRegistry
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.stores.web_attachment_store import WebAttachmentStore
from bot.stores.web_next_turn_settings_store import (
    WebNextTurnSettings,
    WebNextTurnSettingsStore,
)
from bot.stores.web_writer_profile_store import WebWriterProfileStore
from bot.thread_runtime_authority import ThreadRuntimeAuthority
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.interaction_inbox import (
    WebInteractionInbox,
    WebInteractionInboxPorts,
)
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.controller import WebRuntimeController, WebRuntimePorts
from tests.web_runtime.fakes import _FakeRuntime


_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)
_WAV_1X1 = b"RIFF\x00\x00\x00\x00WAVEfmt \x00\x00\x00\x00\x00\x00\x00\x00"


class WebRuntimeControllerHarness(unittest.TestCase):
    """Build one fully wired Controller without defining reusable test cases."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = InteractionLeaseStore(pathlib.Path(self.temp_dir.name))
        self.fake = _FakeRuntime()
        self.workspace = pathlib.Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        self.fake.cwd = str(self.workspace)
        self.projection = FocusWebProjection()
        self.events: list[dict] = []
        self.projection.subscribe(self.events.append)
        self.attachment_store = WebAttachmentStore(
            pathlib.Path(self.temp_dir.name),
            ttl_seconds=300,
        )
        self.profile_store = WebWriterProfileStore(pathlib.Path(self.temp_dir.name))
        self.next_turn_settings_store = WebNextTurnSettingsStore(
            pathlib.Path(self.temp_dir.name),
            initial=WebNextTurnSettings(
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
                model="gpt-test",
                reasoning_effort="high",
            ),
        )
        self.effective_settings = ThreadEffectiveSettingsRegistry()
        self.external_pending_roots: set[str] = set()
        self.server_request_registry = ServerRequestRegistry(resolved_limit=256)
        self.server_request_generation = 1
        self.backend_connection_generation = 1
        self.server_request_registry.activate_connection_epoch(
            self.server_request_generation
        )
        self.interaction_inbox = WebInteractionInbox(
            ports=WebInteractionInboxPorts(
                respond=self.fake.respond,
                active_matches=self.server_request_registry.active_matches,
            ),
            runtime_context_guard=lambda: None,
        )
        self.document_registry = WebDocumentRegistry(runtime_context_guard=lambda: None)
        self.remembered_direct_thread_summaries: list[ThreadSummary] = []
        self.remember_direct_thread_summary_hook = (
            self.remembered_direct_thread_summaries.append
        )

        def remember_direct_thread_summary(summary: ThreadSummary) -> None:
            self.remember_direct_thread_summary_hook(summary)

        self.remember_direct_thread_summary = remember_direct_thread_summary
        self.service_runtime_leases: set[str] = set()

        def acquire_service_runtime(thread_id: str, **_kwargs) -> bool:
            newly_acquired = thread_id not in self.service_runtime_leases
            self.service_runtime_leases.add(thread_id)
            return newly_acquired

        def release_service_runtime(thread_id: str) -> None:
            self.fake.release_runtime(thread_id)
            self.service_runtime_leases.discard(thread_id)

        def prepare_service_runtime_release(thread_id: str) -> object | None:
            normalized_thread_id = str(thread_id or "").strip()
            if normalized_thread_id not in self.service_runtime_leases:
                return None
            return ("service-runtime-release", normalized_thread_id)

        def release_prepared_service_runtime(receipt: object) -> bool:
            if (
                not isinstance(receipt, tuple)
                or len(receipt) != 2
                or receipt[0] != "service-runtime-release"
            ):
                raise ValueError("invalid prepared service-runtime release")
            thread_id = str(receipt[1])
            if thread_id not in self.service_runtime_leases:
                return False
            release_service_runtime(thread_id)
            return True

        self.resume_authority = ThreadRuntimeAuthority(
            adapter=self.fake,
            effective_settings=self.effective_settings,
            acquire_runtime_lease=acquire_service_runtime,
            release_runtime_lease=release_service_runtime,
            resume_failure_known_no_effect=lambda exc: isinstance(
                exc,
                CodexRpcPreSendError,
            ),
        )
        # Test-level mutable transport hooks.  Production coordinators receive
        # stable typed ports; regression tests replace these harness hooks
        # instead of reaching through the Controller's private composition bag.
        self.begin_resume_thread_page = self.resume_authority.begin_resume_thread_page
        self.claim_resume_thread_page = self.resume_authority.claim_resume_thread_page
        self.acquire_claimed_resume_thread_page = (
            self.resume_authority.acquire_claimed_resume_thread_page
        )
        self.complete_claimed_resume_thread_page = (
            self.resume_authority.complete_claimed_resume_thread_page
        )
        self.execute_prepared_resume_thread_page = (
            self.resume_authority.execute_prepared_resume_thread_page
        )
        self.settle_prepared_resume_thread_page = (
            self.resume_authority.settle_prepared_resume_thread_page
        )

        def require_connection_generation(generation: int) -> None:
            if generation != self.backend_connection_generation:
                raise RuntimeError("backend connection generation changed")

        self.require_connection_generation = require_connection_generation

        self.controller = WebRuntimeController(
            instance_name="default",
            web_display_name="Focus Web",
            interaction_lease_store=self.store,
            profile_store=self.profile_store,
            next_turn_settings_store=self.next_turn_settings_store,
            attachment_store=self.attachment_store,
            remember_direct_thread_summary=self.remember_direct_thread_summary,
            effective_settings=self.effective_settings,
            projection=self.projection,
            document_registry=self.document_registry,
            interaction_inbox=self.interaction_inbox,
            ports=WebRuntimePorts(
                list_threads=lambda **kwargs: self.fake.list_threads(**kwargs),
                read_thread=lambda thread_id,
                include_turns,
                **kwargs: self.fake.read_thread(
                    thread_id,
                    include_turns,
                    **kwargs,
                ),
                list_models=lambda: self.fake.list_models(),
                list_loaded_thread_ids=lambda **kwargs: (
                    self.fake.list_loaded_thread_ids(**kwargs)
                ),
                managed_loaded_thread_inventory=(
                    lambda: self.fake.list_managed_loaded_thread_inventory()
                ),
                list_thread_runtime_leases=(
                    self.fake.list_thread_runtime_leases
                ),
                create_and_commit_thread=(
                    self.resume_authority.create_and_commit_thread
                ),
                begin_resume_thread_page=lambda *args, **kwargs: (
                    self.begin_resume_thread_page(*args, **kwargs)
                ),
                claim_resume_thread_page=lambda thread_id: (
                    self.claim_resume_thread_page(thread_id)
                ),
                acquire_claimed_resume_thread_page=lambda *args, **kwargs: (
                    self.acquire_claimed_resume_thread_page(*args, **kwargs)
                ),
                complete_claimed_resume_thread_page=lambda *args, **kwargs: (
                    self.complete_claimed_resume_thread_page(*args, **kwargs)
                ),
                abandon_resume_thread_page_claim=(
                    self.resume_authority.abandon_resume_thread_page_claim
                ),
                abandon_acquired_resume_thread_page=(
                    self.resume_authority.abandon_acquired_resume_thread_page
                ),
                execute_prepared_resume_thread_page=lambda prepared: (
                    self.execute_prepared_resume_thread_page(prepared)
                ),
                settle_prepared_resume_thread_page=lambda *args, **kwargs: (
                    self.settle_prepared_resume_thread_page(*args, **kwargs)
                ),
                list_thread_turns=lambda *args, **kwargs: (
                    self.fake.list_thread_turns(*args, **kwargs)
                ),
                list_thread_items=lambda *args, **kwargs: (
                    self.fake.list_thread_items(*args, **kwargs)
                ),
                search_thread_occurrences=lambda *args, **kwargs: (
                    self.fake.search_thread_occurrences(*args, **kwargs)
                ),
                start_turn=lambda **kwargs: self.resume_authority.start_turn(
                    **kwargs
                ),
                steer_turn=lambda **kwargs: self.fake.steer_turn(**kwargs),
                connection_generation=(
                    lambda **_kwargs: self.backend_connection_generation
                ),
                capture_connection_generation=(
                    lambda: self.backend_connection_generation
                ),
                run_if_connection_generation=(
                    lambda generation, callback: (
                        require_connection_generation(generation),
                        callback(),
                    )[1]
                ),
                compact_thread=lambda thread_id: self.fake.compact_thread(thread_id),
                start_review=lambda *args, **kwargs: self.fake.start_review(
                    *args, **kwargs
                ),
                rename_thread=lambda *args, **kwargs: self.fake.rename_thread(
                    *args, **kwargs
                ),
                get_thread_goal=lambda thread_id, **kwargs: (
                    self.fake.get_thread_goal(thread_id, **kwargs)
                ),
                prepare_runtime_lease_preflight=lambda _thread_id: None,
                set_thread_goal=lambda *args, **kwargs: self.fake.set_thread_goal(
                    *args, **kwargs
                ),
                clear_thread_goal=lambda *args, **kwargs: self.fake.clear_thread_goal(
                    *args, **kwargs
                ),
                archive_thread=lambda *args, **kwargs: self.fake.archive_thread(
                    *args, **kwargs
                ),
                unarchive_thread=lambda *args, **kwargs: self.fake.unarchive_thread(
                    *args, **kwargs
                ),
                delete_thread=lambda *args, **kwargs: self.fake.delete_thread(
                    *args, **kwargs
                ),
                interrupt_turn=lambda **kwargs: self.fake.interrupt_turn(**kwargs),
                prepare_unsubscribe_thread=(
                    self.resume_authority.prepare_unsubscribe_thread
                ),
                execute_prepared_unsubscribe_thread=(
                    self.resume_authority.execute_prepared_unsubscribe_thread
                ),
                settle_prepared_unsubscribe_thread=(
                    self.resume_authority.settle_prepared_unsubscribe_thread
                ),
                abandon_prepared_unsubscribe_thread=(
                    self.resume_authority.abandon_prepared_unsubscribe_thread
                ),
                prepare_service_thread_runtime_lease_release=(
                    prepare_service_runtime_release
                ),
                release_prepared_service_thread_runtime_lease=(
                    release_prepared_service_runtime
                ),
                schedule_runtime_cleanup=lambda thread_id, known_non_active: (
                    self.controller.run_runtime_cleanup_transaction(
                        thread_id,
                        known_non_active,
                    )
                ),
                schedule_notification_projection=lambda receipt: (
                    self.controller.run_notification_projection_transaction(
                        receipt
                    )
                ),
                schedule_attachment_cleanup=lambda scope_key: (
                    self.controller.run_notification_attachment_cleanup(
                        scope_key
                    )
                ),
                thread_subscribers=self.fake.thread_subscribers,
                has_external_pending_interaction_for_root=(
                    lambda root_thread_id: root_thread_id in self.external_pending_roots
                ),
            ),
            runtime_call=lambda callback, *args, **kwargs: callback(
                *args,
                **kwargs,
            ),
            default_working_dir=str(self.workspace),
        )
        # Composition-fixture access to the extracted owner. Individual tests
        # exercise its typed API instead of reaching through Controller state.
        self.operations = self.controller._operations
        self.thread_open = self.controller._thread_open
        self.turn_commands = self.controller._turn_commands
        self.controller.client_connected("tab-1")

        def prepare_web_prompt(
            client_id: str,
            thread_id: str,
            *,
            text: str,
            attachment_ids: list[str] | None = None,
            mutation_id: str = "",
        ):
            if self.document_registry.materialized_thread_id(client_id) != thread_id:
                self.controller.read_thread(client_id, thread_id)
            profile = self.controller.meta(client_id)["writer_profile"]
            scope_generation = int(profile["scope_generation"])
            return self.controller.prepare_prompt(
                client_id,
                thread_id,
                mutation_id=mutation_id or str(uuid.uuid4()),
                text=text,
                attachment_ids=attachment_ids or [],
                source_scope_generation=scope_generation,
                source_attachment_scope=f"thread:{thread_id}",
                source_composer_scope_id=(
                    f"{client_id}:generation:{scope_generation}:thread:{thread_id}"
                ),
            )

        self.prepare_web_prompt = prepare_web_prompt

        def submit_web_prompt(*args, **kwargs) -> dict:
            prepared = prepare_web_prompt(*args, **kwargs)
            return self.controller.run_prepared_prompt(prepared)

        self.submit_web_prompt = submit_web_prompt

        def submit_web_prompt_with_started_notification(*args, **kwargs) -> dict:
            receipt = submit_web_prompt(*args, **kwargs)
            if receipt["status"] == "succeeded" and receipt["mode"] == "start":
                self.deliver_main_turn_lifecycle(
                    "turn/started",
                    str(receipt["thread_id"]),
                    str(receipt["turn_id"]),
                )
            return receipt

        self.submit_web_prompt_with_started_notification = (
            submit_web_prompt_with_started_notification
        )

    def deliver_main_turn_lifecycle(
        self,
        method: str,
        thread_id: str,
        turn_id: str,
    ) -> None:
        """Apply the shared lease stage, then the Web projection stage."""

        if method == "turn/started":
            lease = self.store.load(thread_id)
            if lease is not None and not lease.turn_id:
                self.assertIsNotNone(self.store.activate_turn(lease, turn_id))
        elif method == "turn/completed":
            self.store.release_turn(thread_id, turn_id)
        elif method == "thread/closed":
            self.store.clear_thread(thread_id)
        self.controller.handle_notification(
            method,
            {
                "threadId": thread_id,
                **(
                    {"turn": {"id": turn_id, "status": "inProgress"}}
                    if method == "turn/started"
                    else {"turn": {"id": turn_id, "status": "completed"}}
                    if method == "turn/completed"
                    else {}
                ),
            },
        )

    def seed_web_active_turn_writer(
        self,
        client_id: str,
        thread_id: str,
        turn_id: str = "turn-1",
    ) -> None:
        """Install the exact legacy writer fact needed by single-surface tests."""

        acquired = self.store.acquire(
            thread_id,
            self.operations.holder(client_id),
        )
        self.assertTrue(acquired.granted)
        self.assertIsNotNone(acquired.lease)
        assert acquired.lease is not None
        self.assertIsNotNone(self.store.activate_turn(acquired.lease, turn_id))

    def _claim_server_request(
        self,
        request_id: int | str,
        method: str,
        params: dict,
    ) -> ServerRequestIdentity:
        candidate = ServerRequestIdentity(
            request_id=request_id,
            connection_generation=self.server_request_generation,
            method=method,
            params=params,
        )
        claim = self.server_request_registry.register(candidate)
        self.assertIn(claim.outcome, {"new", "replay"})
        self.assertIsNotNone(claim.identity)
        canonical = claim.identity
        assert canonical is not None
        self.assertTrue(self.server_request_registry.active_matches(canonical))
        return canonical

    def _handle_adapter_request(
        self,
        request_id: int | str,
        method: str,
        params: dict,
        *,
        auto_resolution_timing: AutoResolutionTiming | None = None,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> bool:
        identity = self._claim_server_request(request_id, method, params)
        return self.controller.handle_adapter_request(
            identity,
            auto_resolution_timing=auto_resolution_timing,
            routing_mode=routing_mode,
        )

    def _resolve_server_request(
        self,
        request_id: int | str,
        *,
        thread_id: str,
    ) -> None:
        key = jsonrpc_id_key(request_id)
        resolved = self.server_request_registry.settle(
            key,
            thread_id=thread_id,
        )
        self.assertIn(resolved.outcome, {"settled", "already_resolved"})
        self.controller.remove_resolved_server_request(resolved.identity)

    def respond_request(
        self,
        client_id: str,
        request_key: str,
        *,
        action: str,
        answers: dict | None = None,
        connection_generation: int | None = None,
        response_capability: str | None = None,
    ):
        """Call the strict public response API with its projected capability."""

        snapshot = self.interaction_inbox.snapshot(request_key)
        generation = (
            connection_generation
            if connection_generation is not None
            else snapshot.connection_generation
            if snapshot is not None
            else self.server_request_generation
        )
        capability = (
            response_capability
            if response_capability is not None
            else snapshot.response_capability
            if snapshot is not None
            else "missing-test-capability"
        )
        return self.controller.respond_request(
            client_id,
            request_key,
            connection_generation=generation,
            response_capability=capability,
            action=action,
            answers=answers,
        )
