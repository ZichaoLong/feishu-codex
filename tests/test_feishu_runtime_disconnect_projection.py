from __future__ import annotations

import unittest
from unittest import mock

from bot.feishu_runtime_disconnect_projection import (
    FeishuRuntimeDisconnectProjection,
)


class FeishuRuntimeDisconnectProjectionTest(unittest.TestCase):
    def test_constructor_requires_runtime_guard(self) -> None:
        with self.assertRaisesRegex(TypeError, "RuntimeLoop context guard"):
            FeishuRuntimeDisconnectProjection(
                execution_runtime=mock.Mock(),
                runtime_context_guard=None,  # type: ignore[arg-type]
            )

    def test_wrong_context_rejects_before_runtime_state_access(self) -> None:
        execution_runtime = mock.Mock()
        projection = FeishuRuntimeDisconnectProjection(
            execution_runtime=execution_runtime,
            runtime_context_guard=lambda: (_ for _ in ()).throw(
                RuntimeError("outside RuntimeLoop")
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            projection.prepare()

        execution_runtime.prepare_disconnect.assert_not_called()

    def test_prepare_reports_only_attached_thread_bindings_and_marks_active(
        self,
    ) -> None:
        attached = ("sender-1", "chat-1")
        idle_attached = ("sender-2", "chat-2")
        commands = []
        execution_runtime = mock.Mock()
        execution_runtime.prepare_disconnect.side_effect = lambda command: (
            commands.append(command),
            (attached, idle_attached),
        )[1]
        projection = FeishuRuntimeDisconnectProjection(
            execution_runtime=execution_runtime,
            runtime_context_guard=lambda: None,
        )

        report = projection.prepare()

        self.assertEqual(report.affected_bindings, (attached, idle_attached))
        self.assertEqual(len(commands), 1)
        self.assertEqual(
            commands[0].error_message,
            "Codex websocket disconnected",
        )


if __name__ == "__main__":
    unittest.main()
