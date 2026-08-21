"""Feishu card projection for backend-reset policy and results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bot.backend_reset.contract import (
    BACKEND_RESET_STATUS_AVAILABLE,
    BACKEND_RESET_STATUS_FORCE_ONLY,
    BackendResetPreview,
    BackendResetResult,
)
from bot.cards import build_backend_reset_card


@dataclass(frozen=True, slots=True)
class BackendResetPresenterPorts:
    """Small presentation-only capabilities retained by RuntimeAdmin."""

    instance_name: Callable[[], str]
    format_binding_ids: Callable[[tuple[str, ...] | list[str]], str]
    short_thread_ids: Callable[[tuple[str, ...] | list[str]], str]


class BackendResetPresenter:
    """Render reset facts without reading or mutating runtime state."""

    def __init__(self, ports: BackendResetPresenterPorts) -> None:
        self._ports = ports

    def hard_blocker_lines(self, preview: BackendResetPreview) -> list[str]:
        lines: list[str] = []
        if preview.blocking_active_turn_count:
            line = f"backend active threads：`{preview.blocking_active_turn_count}`"
            if preview.active_loaded_thread_preview:
                line += (
                    f" ({self._ports.short_thread_ids(preview.active_loaded_thread_preview)})"
                )
            lines.append(line)
        if preview.blocking_pending_request_count:
            lines.append(
                f"待处理审批/输入请求：`{preview.blocking_pending_request_count}`"
            )
        if preview.running_binding_ids:
            lines.append(
                "运行中的 Feishu bindings："
                + self._ports.format_binding_ids(preview.running_binding_ids)
            )
        if preview.runtime_verification_failed:
            lines.append("backend loaded thread 校验：`unverified`")
        return lines

    def collateral_lines(self, preview: BackendResetPreview) -> list[str]:
        lines = [
            f"当前实例 loaded threads：`{preview.collateral_loaded_thread_count}`",
        ]
        if preview.attached_binding_ids:
            lines.append(
                "attached Feishu bindings："
                + self._ports.format_binding_ids(preview.attached_binding_ids)
            )
        if preview.blocking_holder_labels:
            lines.append(
                "live runtime holders："
                + self._format_holder_labels(preview.blocking_holder_labels)
            )
        if preview.collateral_active_loaded_thread_count:
            lines.append(
                "其中 active threads："
                f"`{preview.collateral_active_loaded_thread_count}`"
            )
        if preview.loaded_thread_preview:
            lines.append(
                "preview：" + self._ports.short_thread_ids(preview.loaded_thread_preview)
            )
        return lines

    def flat_diagnostics(self, preview: BackendResetPreview) -> tuple[str, ...]:
        lines = [f"当前实例：`{self._ports.instance_name()}`"]
        lines.extend(f"hard blocker：{item}" for item in self.hard_blocker_lines(preview))
        lines.extend(f"collateral impact：{item}" for item in self.collateral_lines(preview))
        return tuple(lines)

    def build_preview_card(
        self,
        preview: BackendResetPreview,
        *,
        leading_lines: list[str] | None = None,
    ) -> dict:
        lines = list(leading_lines or [])
        lines.extend(
            [
                "作用对象：当前实例 backend；这是实例级管理动作，不是当前线程命令。",
                "不会覆盖 binding bookmark、其他用户配置或数据。",
                "",
                f"当前结论：{preview.reason_text}",
            ]
        )
        if preview.status == BACKEND_RESET_STATUS_FORCE_ONLY:
            lines.append("当前只能显式确认强制重置；这会打断当前实例内尚未完成的工作。")
        hard_blockers = self.hard_blocker_lines(preview)
        collateral = self.collateral_lines(preview)
        if hard_blockers:
            lines.extend(["", "**Hard Blockers**"])
            lines.extend(f"- {line}" for line in hard_blockers)
        if collateral:
            lines.extend(["", "**Collateral Impact**"])
            lines.extend(f"- {line}" for line in collateral)
        template = {
            BACKEND_RESET_STATUS_AVAILABLE: "green",
            BACKEND_RESET_STATUS_FORCE_ONLY: "yellow",
        }.get(preview.status, "blue")
        force = None
        if preview.status == BACKEND_RESET_STATUS_AVAILABLE:
            force = False
        elif preview.status == BACKEND_RESET_STATUS_FORCE_ONLY:
            force = True
        return build_backend_reset_card(
            content="\n".join(lines),
            force=force,
            template=template,
        )

    def build_outcome_unknown_card(self) -> dict:
        instance_name = self._ports.instance_name()
        return build_backend_reset_card(
            content=(
                "backend reset 的执行结果无法确认。本卡不声明成功或失败。\n\n"
                f"当前实例：`{instance_name}`\n\n"
                "请勿立即再次重置；如使用 CLI，请先运行 "
                f"`focusctl --instance {instance_name} service status` "
                "检查同一目标实例。"
            ),
            force=None,
            template="yellow",
        )

    def build_result_card(
        self,
        result: BackendResetResult,
        *,
        current_thread_id: str = "",
    ) -> dict:
        if type(result) is not BackendResetResult:
            raise TypeError("backend reset result card requires a typed result")
        lines = [
            "已重置当前实例 backend。",
            f"当前实例：`{self._ports.instance_name()}`",
            f"执行方式：`{'force' if result.force else 'safe'}`",
            "已中断运行中的 binding："
            + self._ports.format_binding_ids(result.interrupted_binding_ids),
            "已 detach 的 binding："
            + self._ports.format_binding_ids(result.detached_binding_ids),
            "已退休旧 backend epoch 的审批/输入请求："
            f"`{result.retired_request_count}`",
            "已清理 live runtime lease thread："
            + self._ports.short_thread_ids(result.purged_thread_ids),
            "当前 backend 地址："
            f"`{result.app_server_url}`",
            "",
            "不会覆盖 binding bookmark、其他用户配置或数据。",
        ]
        if result.detached_binding_ids:
            lines.extend(
                [
                    "",
                    "当前所有相关 Feishu binding 已变为 `detached`；"
                    "若要继续接收推送，可直接在此卡片选择 attach 范围。",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "如需确认飞书侧继续接收本地 `fcodex` / backend 推送，"
                    "可直接在此卡片选择 attach 范围。",
                ]
            )
        if result.projection_warnings:
            lines.extend(
                [
                    "",
                    "**局部投影警告（backend 已完成重置）**",
                    *(f"- {warning}" for warning in result.projection_warnings),
                ]
            )
        normalized_thread_id = str(current_thread_id or "").strip()
        return build_backend_reset_card(
            content="\n".join(lines),
            force=None,
            extra_action_rows=self._attach_action_rows(
                include_thread=bool(normalized_thread_id),
                include_service=True,
                thread_id=normalized_thread_id,
            ),
            template="green",
        )

    @staticmethod
    def _format_holder_labels(holder_labels: tuple[str, ...] | list[str]) -> str:
        normalized = [
            str(label or "").strip()
            for label in holder_labels
            if str(label or "").strip()
        ]
        if not normalized:
            return "（无）"
        return ", ".join(f"`{label}`" for label in normalized)

    @staticmethod
    def _attach_action_rows(
        *,
        include_thread: bool,
        include_service: bool,
        thread_id: str = "",
    ) -> list[dict]:
        actions: list[dict] = []
        if include_thread and str(thread_id or "").strip():
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "附着当前线程"},
                    "type": "primary",
                    "value": {
                        "action": "attach_runtime",
                        "scope": "thread",
                        "thread_id": str(thread_id or "").strip(),
                    },
                }
            )
        if include_service:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "附着当前实例"},
                    "type": "default",
                    "value": {"action": "attach_runtime", "scope": "service"},
                }
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "保持 detached"},
                "type": "default",
                "value": {"action": "dismiss_attach"},
            }
        )
        return [
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "如需继续收到本地 `fcodex` / backend 的推送，可选择 attach 范围：",
            },
            {"tag": "action", "actions": actions},
        ]
