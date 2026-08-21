from __future__ import annotations

import unittest

from bot.backend_reset.contract import BackendResetPreview, BackendResetResult
from bot.backend_reset.presenter import (
    BackendResetPresenter,
    BackendResetPresenterPorts,
)


def _format_ids(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "（无）"


class BackendResetPresenterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.presenter = BackendResetPresenter(
            BackendResetPresenterPorts(
                instance_name=lambda: "default",
                format_binding_ids=_format_ids,
                short_thread_ids=_format_ids,
            )
        )

    def test_preview_renders_policy_facts_without_runtime_reads(self) -> None:
        preview = BackendResetPreview(
            status="force-only",
            reason_code="pending",
            reason_text="需要显式确认",
            running_binding_ids=("p2p:user:chat",),
            blocking_pending_request_count=1,
            collateral_loaded_thread_count=2,
        )

        diagnostics = self.presenter.flat_diagnostics(preview)
        card = self.presenter.build_preview_card(preview)

        self.assertIn("hard blocker：待处理审批/输入请求：`1`", diagnostics)
        content = card["elements"][0]["content"]
        self.assertIn("**Hard Blockers**", content)
        self.assertIn("**Collateral Impact**", content)
        self.assertEqual(card["elements"][2]["actions"][0]["value"]["force"], True)

    def test_result_preserves_projection_warnings_and_attach_scope(self) -> None:
        card = self.presenter.build_result_card(
            BackendResetResult(
                force=False,
                detached_binding_ids=("p2p:user:chat",),
                interrupted_binding_ids=(),
                retired_request_count=4,
                purged_thread_ids=("thread-2",),
                projection_warnings=("detach thread-1: store unavailable",),
                app_server_url="ws://127.0.0.1:8765",
            ),
            current_thread_id="thread-1",
        )

        content = card["elements"][0]["content"]
        self.assertIn(
            "**局部投影警告（backend 已完成重置）**",
            content,
        )
        self.assertIn(
            "已退休旧 backend epoch 的审批/输入请求：`4`",
            content,
        )
        self.assertNotIn("stop 前 fail-close 响应写入", content)
        self.assertIn("执行方式：`safe`", content)
        actions = card["elements"][-1]["actions"]
        self.assertEqual(
            [action["text"]["content"] for action in actions],
            ["附着当前线程", "附着当前实例", "保持 detached"],
        )

    def test_unknown_card_has_no_success_or_reset_action(self) -> None:
        card = self.presenter.build_outcome_unknown_card()

        content = "\n".join(
            str(element.get("content", "")) for element in card["elements"]
        )
        self.assertIn("不声明成功或失败", content)
        self.assertIn("请勿立即再次重置", content)
        self.assertIn("focusctl --instance default service status", content)
        self.assertNotIn("已重置当前实例 backend", content)
        self.assertNotIn("action", [element["tag"] for element in card["elements"]])


if __name__ == "__main__":
    unittest.main()
