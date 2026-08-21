"""Bounded typed projection of app-server notices for Focus Web."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, TypedDict

from bot.focus_web_wire_catalog import FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES


RUNTIME_NOTICE_FIELD_LIMIT_BYTES = FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES


class WebRuntimeErrorNoticeDetail(TypedDict):
    method: Literal["error"]
    message: str
    additional_details: str
    will_retry: bool
    turn_id: str


class WebRuntimeWarningNoticeDetail(TypedDict):
    method: Literal["warning"]
    message: str


WebRuntimeNoticeDetail: TypeAlias = (
    WebRuntimeErrorNoticeDetail | WebRuntimeWarningNoticeDetail
)


@dataclass(frozen=True, slots=True)
class WebRuntimeNoticeProjection:
    """One ephemeral notice admitted for the browser projection stream."""

    thread_id: str
    detail: WebRuntimeNoticeDetail


def _bounded_text(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > RUNTIME_NOTICE_FIELD_LIMIT_BYTES:
        return None
    return value


def _bounded_identifier(value: object) -> str | None:
    admitted = _bounded_text(value)
    if admitted is None or not admitted or admitted.strip() != admitted:
        return None
    return admitted


def project_runtime_notice(
    method: str,
    params: dict[str, object],
) -> WebRuntimeNoticeProjection | None:
    """Admit official typed fields without parsing or rewriting notice text.

    Oversized or malformed input is dropped as one unit. Accepted strings are
    forwarded byte-for-byte instead of being truncated into a different
    message.
    """

    if method == "error":
        error = params.get("error")
        if not isinstance(error, dict):
            return None
        message = _bounded_text(error.get("message"))
        raw_additional_details = error.get("additionalDetails")
        additional_details = (
            ""
            if raw_additional_details is None
            else _bounded_text(raw_additional_details)
        )
        thread_id = _bounded_identifier(params.get("threadId"))
        turn_id = _bounded_identifier(params.get("turnId"))
        will_retry = params.get("willRetry")
        if (
            message is None
            or additional_details is None
            or thread_id is None
            or turn_id is None
            or type(will_retry) is not bool
        ):
            return None
        return WebRuntimeNoticeProjection(
            thread_id=thread_id,
            detail={
                "method": "error",
                "message": message,
                "additional_details": additional_details,
                "will_retry": will_retry,
                "turn_id": turn_id,
            },
        )

    if method == "warning":
        message = _bounded_text(params.get("message"))
        raw_thread_id = params.get("threadId")
        thread_id = (
            ""
            if raw_thread_id is None
            else _bounded_identifier(raw_thread_id)
        )
        if message is None or thread_id is None:
            return None
        return WebRuntimeNoticeProjection(
            thread_id=thread_id,
            detail={
                "method": "warning",
                "message": message,
            },
        )

    return None
