from __future__ import annotations

import unittest

from bot.adapter_notification_pipeline import AdapterNotificationPipeline


class AdapterNotificationPipelineTests(unittest.TestCase):
    def _pipeline(self, calls: list[str]) -> AdapterNotificationPipeline:
        return AdapterNotificationPipeline(
            stages={
                name: lambda _method, _params, name=name: calls.append(name)
                for name in AdapterNotificationPipeline.STAGE_ORDER
            },
            assert_runtime_context=lambda: calls.append("context"),
        )

    def test_dispatch_uses_canonical_stage_order(self) -> None:
        calls: list[str] = []
        pipeline = self._pipeline(calls)

        pipeline.dispatch("turn/completed", {"threadId": "thread-1"})

        self.assertEqual(
            calls,
            ["context", *AdapterNotificationPipeline.STAGE_ORDER],
        )

    def test_failed_stage_stops_later_runtime_stages(self) -> None:
        calls: list[str] = []
        stages = {
            name: lambda _method, _params, name=name: calls.append(name)
            for name in AdapterNotificationPipeline.STAGE_ORDER
        }

        def fail(_method: str, _params: dict) -> None:
            calls.append("web_runtime")
            raise RuntimeError("web runtime failed")

        stages["web_runtime"] = fail
        pipeline = AdapterNotificationPipeline(
            stages=stages,
            assert_runtime_context=lambda: calls.append("context"),
        )

        with self.assertRaisesRegex(RuntimeError, "web runtime failed"):
            pipeline.dispatch("turn/started", {})

        self.assertEqual(
            calls,
            [
                "context",
                "effective_settings_facts",
                "active_turn_owner",
                "server_requests",
                "web_runtime",
            ],
        )

    def test_constructor_rejects_missing_or_unknown_stage(self) -> None:
        stages = {
            name: lambda _method, _params: None
            for name in AdapterNotificationPipeline.STAGE_ORDER
        }
        stages.pop("operation_owner")
        stages["legacy_side_effect"] = lambda _method, _params: None

        with self.assertRaisesRegex(
            ValueError,
            "missing stages: operation_owner; unknown stages: legacy_side_effect",
        ):
            AdapterNotificationPipeline(
                stages=stages,
                assert_runtime_context=lambda: None,
            )


if __name__ == "__main__":
    unittest.main()
