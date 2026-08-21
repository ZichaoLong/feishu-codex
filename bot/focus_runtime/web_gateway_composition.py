"""Compose the Web Gateway from explicit Focus runtime capabilities.

The composition boundary owns only the mapping into ``WebGatewayPorts``.  It
stores no runtime facts and has no dependency on the ``FocusRuntime`` root.
See ``docs/architecture/focus-design.zh-CN.md`` for the application-composition
boundary.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

from bot.service_runtime_lifecycle import ServiceRuntimeIngressDispatcher
from bot.web_runtime.backend_reset_controller import WebBackendResetController
from bot.web_runtime.controller import WebRuntimeController
from bot.web_runtime.gateway import WebGateway, WebGatewayConfig, WebGatewayPorts
from bot.web_runtime.projection import FocusWebProjection


def compose_web_gateway(
    *,
    config: WebGatewayConfig,
    data_dir: pathlib.Path,
    projection: FocusWebProjection,
    web_runtime: WebRuntimeController,
    backend_reset: Callable[[], WebBackendResetController],
    ingress: Callable[[], ServiceRuntimeIngressDispatcher],
    runtime_call: Callable[..., Any],
    operator_status: Callable[[], dict[str, Any]],
) -> WebGateway:
    """Build the Gateway without exposing its port catalog to the root."""

    def abandon_prepared_prompt(prepared: Any) -> bool:
        dispatcher = ingress()
        if not dispatcher.abandon_prepared_external_transaction(prepared):
            return False
        # Only a successful ingress CAS proves that no external worker claimed
        # this receipt. A duplicate preparation has no prompt owner token, so
        # its controller abandon is deliberately a no-op.
        return bool(runtime_call(web_runtime.abandon_prompt, prepared.preparation))

    return WebGateway(
        config=config,
        data_dir=data_dir,
        projection=projection,
        ports=WebGatewayPorts(
            meta=lambda client_id: runtime_call(web_runtime.meta, client_id),
            operator_status=operator_status,
            backend_reset_preview=lambda: runtime_call(backend_reset().preview),
            backend_reset_execute=lambda **kwargs: runtime_call(
                backend_reset().execute,
                **kwargs,
            ),
            update_profile=lambda client_id, changes, **kwargs: runtime_call(
                web_runtime.update_profile,
                client_id,
                changes,
                **kwargs,
            ),
            next_turn_settings=lambda: runtime_call(web_runtime.next_turn_settings),
            update_next_turn_settings=lambda client_id, changes: runtime_call(
                web_runtime.update_next_turn_settings,
                client_id,
                changes,
            ),
            stage_attachment=lambda client_id, **kwargs: runtime_call(
                web_runtime.stage_attachment,
                client_id,
                **kwargs,
            ),
            attachment_download=lambda attachment_id: runtime_call(
                web_runtime.attachment_download,
                attachment_id,
            ),
            prepare_list_threads=lambda **kwargs: ingress().prepare_external_transaction(
                web_runtime.prepare_list_threads,
                **kwargs,
            ),
            prepare_read_thread=lambda client_id,
            thread_id,
            **kwargs: ingress().prepare_external_transaction(
                web_runtime.prepare_read_thread,
                client_id,
                thread_id,
                **kwargs,
            ),
            prepare_list_older_turns=lambda client_id,
            thread_id,
            **kwargs: ingress().prepare_external_transaction(
                web_runtime.prepare_list_older_turns,
                client_id,
                thread_id,
                **kwargs,
            ),
            run_prepared_thread_read=lambda prepared: ingress().run_prepared_external_transaction(
                prepared,
                web_runtime.run_prepared_thread_read,
            ),
            abandon_prepared_thread_read=lambda prepared: ingress().abandon_prepared_external_transaction(
                prepared
            ),
            prepare_tool_detail=lambda client_id,
            thread_id,
            turn_id,
            item_id,
            **kwargs: ingress().prepare_external_transaction(
                web_runtime.prepare_tool_detail,
                client_id,
                thread_id,
                turn_id,
                item_id,
                **kwargs,
            ),
            prepare_conversation_search=lambda client_id,
            thread_id,
            **kwargs: ingress().prepare_external_transaction(
                web_runtime.prepare_conversation_search,
                client_id,
                thread_id,
                **kwargs,
            ),
            start_thread=lambda client_id, **kwargs: runtime_call(
                web_runtime.start_thread,
                client_id,
                **kwargs,
            ),
            prepare_prompt=lambda client_id,
            thread_id,
            **kwargs: ingress().prepare_external_transaction(
                web_runtime.prepare_prompt,
                client_id,
                thread_id,
                **kwargs,
            ),
            run_prepared_prompt=lambda prepared: ingress().run_prepared_external_transaction(
                prepared,
                web_runtime.run_prepared_prompt,
            ),
            abandon_prepared_prompt=abandon_prepared_prompt,
            prompt_result=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.prompt_result,
                client_id,
                thread_id,
                **kwargs,
            ),
            interrupt=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.interrupt,
                client_id,
                thread_id,
                **kwargs,
            ),
            resolve_unknown_mutation=lambda client_id,
            thread_id,
            **kwargs: runtime_call(
                web_runtime.resolve_unknown_mutation,
                client_id,
                thread_id,
                **kwargs,
            ),
            rename_thread=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.rename_thread,
                client_id,
                thread_id,
                **kwargs,
            ),
            compact_thread=lambda client_id, thread_id: runtime_call(
                web_runtime.compact_thread,
                client_id,
                thread_id,
            ),
            start_review=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.start_review,
                client_id,
                thread_id,
                **kwargs,
            ),
            goal=lambda client_id, thread_id: runtime_call(
                web_runtime.goal,
                client_id,
                thread_id,
            ),
            set_goal=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.set_goal,
                client_id,
                thread_id,
                **kwargs,
            ),
            clear_goal=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.clear_goal,
                client_id,
                thread_id,
                **kwargs,
            ),
            archive_thread=lambda client_id, thread_id: runtime_call(
                web_runtime.archive_thread,
                client_id,
                thread_id,
            ),
            unarchive_thread=lambda client_id, thread_id: runtime_call(
                web_runtime.unarchive_thread,
                client_id,
                thread_id,
            ),
            delete_thread=lambda client_id, thread_id, **kwargs: runtime_call(
                web_runtime.delete_thread,
                client_id,
                thread_id,
                **kwargs,
            ),
            respond_request=lambda client_id, request_id, **kwargs: runtime_call(
                web_runtime.respond_request,
                client_id,
                request_id,
                **kwargs,
            ),
            document_intent_generation_floor=lambda client_id: runtime_call(
                web_runtime.document_intent_generation_floor,
                client_id,
            ),
            client_connected=lambda client_id: runtime_call(
                web_runtime.client_connected,
                client_id,
            ),
            client_transport_disconnected=lambda client_id: runtime_call(
                web_runtime.client_transport_disconnected,
                client_id,
            ),
            client_document_reissued=lambda client_id: runtime_call(
                web_runtime.client_document_reissued,
                client_id,
            ),
            client_disconnected=lambda client_id: runtime_call(
                web_runtime.client_disconnected,
                client_id,
            ),
        ),
    )
