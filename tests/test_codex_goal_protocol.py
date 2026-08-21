from __future__ import annotations

import copy
import unittest

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcProtocolError


def _goal() -> dict[str, object]:
    return {
        "threadId": "thread-1",
        "objective": "ship strict goal decoding",
        "status": "active",
        "tokenBudget": 1000,
        "tokensUsed": 25,
        "timeUsedSeconds": 3,
        "createdAt": 10,
        "updatedAt": 11,
    }


class _GoalRpc:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, dict | None]] = []

    def request(self, method: str, params: dict | None = None, **_kwargs):
        self.calls.append((method, params))
        return copy.deepcopy(self.response)


class CodexGoalProtocolTests(unittest.TestCase):
    @staticmethod
    def _adapter(response: object) -> CodexAppServerAdapter:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        adapter._rpc = _GoalRpc(response)
        return adapter

    def test_get_accepts_explicit_null_as_no_goal(self):
        adapter = self._adapter({"goal": None})

        self.assertIsNone(adapter.get_thread_goal("thread-1"))

    def test_get_rejects_missing_or_wrong_typed_goal(self):
        for response in (None, [], {}, {"goal": []}, {"goal": "none"}):
            with self.subTest(response=response):
                adapter = self._adapter(response)
                with self.assertRaises(CodexRpcProtocolError) as caught:
                    adapter.get_thread_goal("thread-1")
                self.assertEqual(caught.exception.method, "thread/goal/get")

    def test_get_rejects_missing_required_goal_fields(self):
        for field in (
            "threadId",
            "objective",
            "status",
            "tokenBudget",
            "tokensUsed",
            "timeUsedSeconds",
            "createdAt",
            "updatedAt",
        ):
            with self.subTest(field=field):
                goal = _goal()
                goal.pop(field)
                adapter = self._adapter({"goal": goal})
                with self.assertRaises(CodexRpcProtocolError):
                    adapter.get_thread_goal("thread-1")

    def test_get_rejects_wrong_field_types_and_mismatched_thread(self):
        cases: tuple[tuple[str, object], ...] = (
            ("threadId", "thread-2"),
            ("objective", None),
            ("status", ""),
            ("tokenBudget", True),
            ("tokensUsed", "25"),
            ("timeUsedSeconds", False),
            ("createdAt", 1.5),
            ("updatedAt", None),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                goal = _goal()
                goal[field] = value
                adapter = self._adapter({"goal": goal})
                with self.assertRaises(CodexRpcProtocolError):
                    adapter.get_thread_goal("thread-1")

    def test_set_requires_a_matching_non_null_goal(self):
        for response in ({}, {"goal": None}, {"goal": {**_goal(), "threadId": "other"}}):
            with self.subTest(response=response):
                adapter = self._adapter(response)
                with self.assertRaises(CodexRpcProtocolError) as caught:
                    adapter.set_thread_goal("thread-1", status="paused")
                self.assertEqual(caught.exception.method, "thread/goal/set")

    def test_clear_accepts_exact_false_without_coercion(self):
        adapter = self._adapter({"cleared": False})

        self.assertFalse(adapter.clear_thread_goal("thread-1"))

    def test_clear_rejects_missing_or_non_boolean_result(self):
        for response in (None, [], {}, {"cleared": None}, {"cleared": 0}, {"cleared": "false"}):
            with self.subTest(response=response):
                adapter = self._adapter(response)
                with self.assertRaises(CodexRpcProtocolError) as caught:
                    adapter.clear_thread_goal("thread-1")
                self.assertEqual(caught.exception.method, "thread/goal/clear")


if __name__ == "__main__":
    unittest.main()
