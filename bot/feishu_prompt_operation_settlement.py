"""Exact owner settlement for one failed Feishu prompt-start transaction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from bot.feishu_execution_start_contract import FeishuOperationSettlement
from bot.feishu_root_operation_contract import FeishuRootOperationToken


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeishuPromptOperationSettlementPorts:
    operation_outcome_unknown: Callable[[Exception], bool]
    settle_known_failure: Callable[..., None]
    settle_known_mutation: Callable[..., None]
    acknowledge_continuing: Callable[[FeishuRootOperationToken], None]
    mark_outcome_unknown: Callable[..., None]


class FeishuPromptOperationSettlementService:
    """Classify an outbound failure and settle only its exact owner token."""

    def __init__(self, ports: FeishuPromptOperationSettlementPorts) -> None:
        if any(
            not callable(getattr(ports, name))
            for name in ports.__dataclass_fields__
        ):
            raise TypeError("Feishu prompt settlement port 必须可调用。")
        self._ports = ports

    def settle_after_failure(
        self,
        token: FeishuRootOperationToken,
        exc: Exception,
        *,
        reason: str,
        known_mutation: bool,
        retain_continuing: bool,
    ) -> FeishuOperationSettlement:
        try:
            outcome_unknown = self._ports.operation_outcome_unknown(exc)
        except Exception:
            logger.exception("无法分类 Feishu mutation 的上游结果")
            return FeishuOperationSettlement(False, "blocked_unsettled")
        if outcome_unknown:
            try:
                self._ports.mark_outcome_unknown(
                    token,
                    reason=f"{reason}_outcome_unknown",
                )
            except Exception:
                logger.exception("无法记录 Feishu mutation 的进程内未知上游结果")
                return FeishuOperationSettlement(False, "blocked_unsettled")
            return FeishuOperationSettlement(True, "blocked_unsettled")
        return self.finish_without_turn(
            token,
            reason=reason,
            known_mutation=known_mutation,
            retain_continuing=retain_continuing,
        )

    def finish_without_turn(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
        known_mutation: bool,
        retain_continuing: bool,
    ) -> FeishuOperationSettlement:
        try:
            if retain_continuing:
                self._ports.acknowledge_continuing(token)
            elif known_mutation:
                self._ports.settle_known_mutation(token, reason=reason)
            else:
                self._ports.settle_known_failure(token, reason=reason)
        except Exception:
            logger.exception(
                "无法结算 exact Feishu prompt operation token: reason=%s",
                reason,
            )
            return FeishuOperationSettlement(False, "blocked_unsettled")
        return FeishuOperationSettlement(
            True,
            (
                "blocked_unsettled"
                if retain_continuing
                else "known_no_effect_settled"
            ),
        )

    def settle_known_failure(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> FeishuOperationSettlement:
        return self.finish_without_turn(
            token,
            reason=reason,
            known_mutation=False,
            retain_continuing=False,
        )
