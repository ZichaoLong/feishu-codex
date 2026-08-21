"""Closed Feishu outbound effect and destination-liveness outcomes."""

from __future__ import annotations

import math
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
)


logger = logging.getLogger(__name__)
_PATCH_MESSAGE_RETRY_SECONDS = 2.0


class FeishuOutboundOperation(str, Enum):
    CREATE_MESSAGE = "create_message"
    REPLY_MESSAGE = "reply_message"
    PATCH_MESSAGE = "patch_message"


class FeishuOutboundEffect(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class FeishuDestinationLiveness(str, Enum):
    REACHABLE = "reachable"
    PROVEN_UNREACHABLE = "proven_unreachable"
    UNKNOWN = "unknown"


_PERMANENT_DESTINATION_CODES = {
    "230002": FeishuDestinationLossProofType.OUTBOUND_BOT_OUTSIDE_CHAT,
    "232009": FeishuDestinationLossProofType.OUTBOUND_CHAT_DISSOLVED,
}
_KNOWN_REJECTED_CODES = {
    FeishuOutboundOperation.CREATE_MESSAGE: frozenset(
        {
            "230001",
            "230002",
            "230006",
            "230013",
            "230015",
            "230017",
            "230018",
            "230019",
            "230020",
            "230022",
            "230025",
            "230027",
            "230028",
            "230029",
            "230034",
            "230035",
            "230036",
            "230038",
            "230053",
            "230054",
            "230055",
            "230075",
            "230099",
            "232009",
        }
    ),
    FeishuOutboundOperation.REPLY_MESSAGE: frozenset(
        {
            "230001",
            "230002",
            "230006",
            "230011",
            "230013",
            "230015",
            "230017",
            "230018",
            "230019",
            "230020",
            "230022",
            "230025",
            "230027",
            "230028",
            "230035",
            "230038",
            "230050",
            "230054",
            "230055",
            "230071",
            "230072",
            "230075",
            "230099",
            "232009",
        }
    ),
    FeishuOutboundOperation.PATCH_MESSAGE: frozenset(
        {
            "230001",
            "230002",
            "230006",
            "230011",
            "230013",
            "230018",
            "230020",
            "230022",
            "230025",
            "230027",
            "230028",
            "230035",
            "230038",
            "230050",
            "230054",
            "230055",
            "230071",
            "230072",
            "230073",
            "230074",
            "230075",
            "230099",
            "230110",
            "232009",
        }
    ),
}


def _required_text(value: object, *, field: str, maximum: int = 1024) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} cannot be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{field} is too long")
    return normalized


@dataclass(frozen=True, slots=True)
class FeishuOutboundResult:
    """One outbound effect outcome and its independent liveness evidence."""

    operation: FeishuOutboundOperation
    effect: FeishuOutboundEffect
    destination_liveness: FeishuDestinationLiveness
    chat_id: str
    attempt_id: str
    message_id: str = ""
    error_code: str = ""
    error_message: str = ""
    retry_after_seconds: float = 0.0
    content_rejected: bool = False

    def __post_init__(self) -> None:
        if type(self.operation) is not FeishuOutboundOperation:
            raise TypeError("operation must be FeishuOutboundOperation")
        if type(self.effect) is not FeishuOutboundEffect:
            raise TypeError("effect must be FeishuOutboundEffect")
        if type(self.destination_liveness) is not FeishuDestinationLiveness:
            raise TypeError("destination_liveness must be FeishuDestinationLiveness")
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, field="chat_id"),
        )
        object.__setattr__(
            self,
            "attempt_id",
            _required_text(self.attempt_id, field="attempt_id", maximum=50),
        )
        for field in ("message_id", "error_code", "error_message"):
            value = getattr(self, field)
            if type(value) is not str:
                raise TypeError(f"{field} must be an exact string")
            object.__setattr__(self, field, value.strip())
        retry_after = float(self.retry_after_seconds)
        if not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError("retry_after_seconds must be finite and non-negative")
        object.__setattr__(self, "retry_after_seconds", retry_after)
        if type(self.content_rejected) is not bool:
            raise TypeError("content_rejected must be bool")
        if self.effect is FeishuOutboundEffect.CONFIRMED:
            if self.destination_liveness is not FeishuDestinationLiveness.REACHABLE:
                raise ValueError("confirmed outbound requires reachable liveness")
            if not self.message_id:
                raise ValueError("confirmed outbound requires message_id")
            if self.error_code or self.error_message or self.content_rejected:
                raise ValueError("confirmed outbound cannot carry failure fields")
        if self.destination_liveness is FeishuDestinationLiveness.PROVEN_UNREACHABLE:
            if self.effect is not FeishuOutboundEffect.REJECTED:
                raise ValueError("proven unreachable outbound must be rejected")
            if self.error_code not in _PERMANENT_DESTINATION_CODES:
                raise ValueError("unreviewed code cannot prove destination loss")
        if self.content_rejected and self.effect is not FeishuOutboundEffect.REJECTED:
            raise ValueError("content rejection requires a rejected effect")
        if self.retry_after_seconds and self.effect is FeishuOutboundEffect.CONFIRMED:
            raise ValueError("confirmed outbound cannot request retry")

    @property
    def ok(self) -> bool:
        return self.effect is FeishuOutboundEffect.CONFIRMED

    @property
    def retryable(self) -> bool:
        return self.retry_after_seconds > 0

    @property
    def safe_to_fallback(self) -> bool:
        return (
            self.effect is FeishuOutboundEffect.REJECTED
            and self.destination_liveness
            is not FeishuDestinationLiveness.PROVEN_UNREACHABLE
        )

    def destination_loss_proof(self) -> FeishuDestinationLossProof | None:
        if (
            self.destination_liveness
            is not FeishuDestinationLiveness.PROVEN_UNREACHABLE
        ):
            return None
        proof_type = _PERMANENT_DESTINATION_CODES[self.error_code]
        return FeishuDestinationLossProof(
            source_id=self.attempt_id,
            chat_id=self.chat_id,
            proof_type=proof_type,
        )


def classify_feishu_api_failure(
    *,
    operation: FeishuOutboundOperation,
    chat_id: str,
    attempt_id: str,
    error_code: object,
    error_message: object,
    allow_destination_proof: bool,
    retry_after_seconds: float = 0.0,
    content_rejected: bool = False,
) -> FeishuOutboundResult:
    """Classify only reviewed official codes; everything else stays unknown."""

    code = str(error_code or "").strip()
    message = str(error_message or "").strip()
    if allow_destination_proof and code in _PERMANENT_DESTINATION_CODES:
        effect = FeishuOutboundEffect.REJECTED
        liveness = FeishuDestinationLiveness.PROVEN_UNREACHABLE
    elif code in _KNOWN_REJECTED_CODES[operation]:
        effect = FeishuOutboundEffect.REJECTED
        liveness = FeishuDestinationLiveness.UNKNOWN
    else:
        effect = FeishuOutboundEffect.UNKNOWN
        liveness = FeishuDestinationLiveness.UNKNOWN
    return FeishuOutboundResult(
        operation=operation,
        effect=effect,
        destination_liveness=liveness,
        chat_id=chat_id,
        attempt_id=attempt_id,
        error_code=code,
        error_message=message,
        retry_after_seconds=retry_after_seconds,
        content_rejected=content_rejected,
    )


class FeishuOutboundGateway:
    """Own Feishu message effects, outcome classification, and loss proofs."""

    def __init__(
        self,
        *,
        client: Callable[[], Any],
        publish_destination_loss: Callable[[FeishuDestinationLossProof], None],
        request_timeout_seconds: float,
    ) -> None:
        self._client = client
        self._publish_destination_loss = publish_destination_loss
        self._request_timeout_seconds = float(request_timeout_seconds)

    @staticmethod
    def _receive_id_type(receive_id: str) -> str:
        return "open_id" if receive_id.startswith("ou_") else "chat_id"

    @staticmethod
    def _attempt_id(value: str) -> str:
        normalized = str(value or "").strip() or uuid.uuid4().hex
        if len(normalized) > 50:
            raise ValueError("Feishu outbound attempt_id exceeds 50 characters")
        return normalized

    def _publish_result(
        self,
        result: FeishuOutboundResult,
    ) -> FeishuOutboundResult:
        proof = result.destination_loss_proof()
        if proof is not None:
            self._publish_destination_loss(proof)
        return result

    @staticmethod
    def _unknown_result(
        *,
        operation: FeishuOutboundOperation,
        chat_id: str,
        attempt_id: str,
        error: object,
        retry_after_seconds: float = 0.0,
    ) -> FeishuOutboundResult:
        return FeishuOutboundResult(
            operation=operation,
            effect=FeishuOutboundEffect.UNKNOWN,
            destination_liveness=FeishuDestinationLiveness.UNKNOWN,
            chat_id=chat_id,
            attempt_id=attempt_id,
            error_message=str(error or "").strip(),
            retry_after_seconds=retry_after_seconds,
        )

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        outbound_attempt_id = self._attempt_id(attempt_id)
        id_type = self._receive_id_type(chat_id)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .uuid(outbound_attempt_id)
                .build()
            )
            .build()
        )
        logger.info(
            "发送消息: receive_id=%s, receive_id_type=%s, msg_type=%s, timeout=%.1fs",
            chat_id,
            id_type,
            msg_type,
            self._request_timeout_seconds,
        )
        try:
            response = self._client().im.v1.message.create(request)
        except Exception as exc:
            logger.exception("发送消息失败(SDK异常): %s", exc)
            return self._unknown_result(
                operation=FeishuOutboundOperation.CREATE_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error=exc,
            )
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)
            result = classify_feishu_api_failure(
                operation=FeishuOutboundOperation.CREATE_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error_code=getattr(response, "code", ""),
                error_message=getattr(response, "msg", ""),
                allow_destination_proof=id_type == "chat_id",
                retry_after_seconds=(
                    _PATCH_MESSAGE_RETRY_SECONDS
                    if str(getattr(response, "code", "") or "").strip() == "230020"
                    else 0.0
                ),
            )
            return self._publish_result(result)
        message_id = str(
            getattr(getattr(response, "data", None), "message_id", "") or ""
        ).strip()
        if not message_id:
            logger.error("发送消息成功响应缺少 message_id: receive_id=%s", chat_id)
            return self._unknown_result(
                operation=FeishuOutboundOperation.CREATE_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error="successful Feishu response omitted message_id",
            )
        logger.info(
            "发送消息成功: receive_id=%s, message_id=%s, msg_type=%s",
            chat_id,
            message_id,
            msg_type,
        )
        return FeishuOutboundResult(
            operation=FeishuOutboundOperation.CREATE_MESSAGE,
            effect=FeishuOutboundEffect.CONFIRMED,
            destination_liveness=FeishuDestinationLiveness.REACHABLE,
            chat_id=chat_id,
            attempt_id=outbound_attempt_id,
            message_id=message_id,
        )

    @staticmethod
    def _patch_error_ext(response: Any) -> str:
        raw = getattr(response, "raw", None)
        if isinstance(raw, dict):
            return str(raw.get("ext", "") or "")
        return ""

    @staticmethod
    def _is_retryable_patch_exception(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        module_name = type(exc).__module__.lower()
        class_name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "timeout" in class_name or "timeout" in text:
            return True
        return "requests" in module_name and "timeout" in class_name

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        outbound_attempt_id = self._attempt_id(attempt_id)
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(content).build())
            .build()
        )
        try:
            response = self._client().im.v1.message.patch(request)
        except Exception as exc:
            if self._is_retryable_patch_exception(exc):
                logger.warning(
                    "消息更新失败，稍后重试: message_id=%s error=%s",
                    message_id,
                    exc,
                )
                return self._unknown_result(
                    operation=FeishuOutboundOperation.PATCH_MESSAGE,
                    chat_id=chat_id,
                    attempt_id=outbound_attempt_id,
                    error=exc,
                    retry_after_seconds=_PATCH_MESSAGE_RETRY_SECONDS,
                )
            logger.error(
                "消息更新失败(SDK异常): message_id=%s error=%s",
                message_id,
                exc,
            )
            return self._unknown_result(
                operation=FeishuOutboundOperation.PATCH_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error=exc,
            )
        if not response.success():
            code = str(getattr(response, "code", "") or "").strip()
            ext = self._patch_error_ext(response)
            error_message = f"{getattr(response, 'msg', '')} {ext}".strip()
            error_text = error_message.lower()
            if code == "230020":
                logger.warning(
                    "消息更新触发频率限制，稍后重试: message_id=%s code=%s msg=%s ext=%s",
                    message_id,
                    code,
                    response.msg,
                    ext,
                )
                return classify_feishu_api_failure(
                    operation=FeishuOutboundOperation.PATCH_MESSAGE,
                    chat_id=chat_id,
                    attempt_id=outbound_attempt_id,
                    error_code=code,
                    error_message=error_message,
                    allow_destination_proof=True,
                    retry_after_seconds=_PATCH_MESSAGE_RETRY_SECONDS,
                )
            if code == "230099" and (
                "failed to create card content" in error_text
                or "markdown content parse error" in error_text
            ):
                logger.error(
                    "消息更新内容被飞书拒绝: message_id=%s code=%s msg=%s ext=%s",
                    message_id,
                    code,
                    response.msg,
                    ext,
                )
                return classify_feishu_api_failure(
                    operation=FeishuOutboundOperation.PATCH_MESSAGE,
                    chat_id=chat_id,
                    attempt_id=outbound_attempt_id,
                    error_code=code,
                    error_message=error_message,
                    allow_destination_proof=True,
                    content_rejected=True,
                )
            logger.error(
                "消息更新失败: message_id=%s code=%s msg=%s ext=%s",
                message_id,
                code,
                response.msg,
                ext,
            )
            return self._publish_result(
                classify_feishu_api_failure(
                    operation=FeishuOutboundOperation.PATCH_MESSAGE,
                    chat_id=chat_id,
                    attempt_id=outbound_attempt_id,
                    error_code=code,
                    error_message=error_message,
                    allow_destination_proof=True,
                )
            )
        return FeishuOutboundResult(
            operation=FeishuOutboundOperation.PATCH_MESSAGE,
            effect=FeishuOutboundEffect.CONFIRMED,
            destination_liveness=FeishuDestinationLiveness.REACHABLE,
            chat_id=chat_id,
            attempt_id=outbound_attempt_id,
            message_id=str(message_id or "").strip(),
        )

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        outbound_attempt_id = self._attempt_id(attempt_id)
        request = (
            ReplyMessageRequest.builder()
            .message_id(parent_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .reply_in_thread(reply_in_thread)
                .uuid(outbound_attempt_id)
                .build()
            )
            .build()
        )
        try:
            response = self._client().im.v1.message.reply(request)
        except Exception as exc:
            logger.error("引用回复失败(SDK异常): %s", exc)
            return self._unknown_result(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error=exc,
            )
        if not response.success():
            logger.error("引用回复失败: code=%s, msg=%s", response.code, response.msg)
            result = classify_feishu_api_failure(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error_code=getattr(response, "code", ""),
                error_message=getattr(response, "msg", ""),
                allow_destination_proof=True,
                retry_after_seconds=(
                    _PATCH_MESSAGE_RETRY_SECONDS
                    if str(getattr(response, "code", "") or "").strip() == "230020"
                    else 0.0
                ),
            )
            return self._publish_result(result)
        reply_message_id = str(
            getattr(getattr(response, "data", None), "message_id", "") or ""
        ).strip()
        if not reply_message_id:
            logger.error("引用回复成功响应缺少 message_id: parent_id=%s", parent_id)
            return self._unknown_result(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                chat_id=chat_id,
                attempt_id=outbound_attempt_id,
                error="successful Feishu response omitted message_id",
            )
        logger.info(
            "引用回复成功: parent_id=%s message_id=%s msg_type=%s reply_in_thread=%s",
            parent_id,
            reply_message_id,
            msg_type,
            reply_in_thread,
        )
        return FeishuOutboundResult(
            operation=FeishuOutboundOperation.REPLY_MESSAGE,
            effect=FeishuOutboundEffect.CONFIRMED,
            destination_liveness=FeishuDestinationLiveness.REACHABLE,
            chat_id=chat_id,
            attempt_id=outbound_attempt_id,
            message_id=reply_message_id,
        )
