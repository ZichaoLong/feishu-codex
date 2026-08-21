"""Thin authenticated HTTP handlers for bounded thread inspection."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from bot.web_runtime import gateway_request_decoder as request_decoder
from bot.web_runtime.thread_inspection_wire import encode_thread_inspection_json


def _inspection_json_response(payload: Any) -> web.Response:
    return web.Response(
        body=encode_thread_inspection_json(payload),
        content_type="application/json",
        charset="utf-8",
    )


class WebGatewayThreadInspectionMixin:
    """Route admitted inspection reads through the Gateway document barrier."""

    _ports: Any

    async def _handle_thread_tool_detail(self, request: web.Request) -> web.Response:
        client_id = self._required_client_id(request)
        view, change_index, cursor = request_decoder.decode_tool_detail_query(
            request.query
        )
        return _inspection_json_response(
            await self._staged_document_request_to_thread(
                self._ports.prepare_tool_detail,
                request,
                client_id,
                request.match_info["thread_id"],
                request.match_info["turn_id"],
                request.match_info["item_id"],
                view=view,
                change_index=change_index,
                cursor=cursor,
            )
        )

    async def _handle_thread_conversation_search(
        self,
        request: web.Request,
    ) -> web.Response:
        client_id = self._required_client_id(request)
        query, cursor = request_decoder.decode_conversation_search_query(request.query)
        return _inspection_json_response(
            await self._staged_document_request_to_thread(
                self._ports.prepare_conversation_search,
                request,
                client_id,
                request.match_info["thread_id"],
                query=query,
                cursor=cursor,
            )
        )
