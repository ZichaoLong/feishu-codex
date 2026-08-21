from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HANDLER_PATH = _REPO_ROOT / "bot" / "focus_runtime" / "runtime.py"
_CONTINUATION_PATH = _REPO_ROOT / "bot" / "feishu_continuation_controller.py"
_RESUME_ENTRY_METHODS = {
    _CONTINUATION_PATH: frozenset(
        {"attach_binding_for_control", "resume_goal", "resume_thread"}
    ),
}
_REMOVED_HANDLER_CONTINUATION_METHODS = frozenset(
    {
        "_attach_binding_for_control",
        "_clear_feishu_goal",
        "_clear_direct_thread_goal",
        "_extract_history_rounds",
        "_feishu_resume_may_autostart",
        "_get_direct_thread_goal",
        "_get_thread_goal_if_available",
        "_is_goals_feature_disabled_error",
        "_mutate_feishu_goal",
        "_refresh_threads_card_message",
        "_require_direct_thread_summary",
        "_resolve_resume_target",
        "_restore_paused_goal_after_failed_resume",
        "_resume_goal_on_runtime",
        "_resume_thread_on_runtime",
        "_set_direct_thread_goal",
        "_update_thread_settings_with_model_fence",
        "_update_runtime_goal_projection",
    }
)
_CLOSED_ROOT_SETTLEMENT_CALLS = frozenset(
    {
        "acknowledge_continuing",
        "mark_outcome_unknown",
        "settle_continuation_failure",
        "settle_known_failure",
        "settle_known_mutation",
        "settle_noncontinuing",
    }
)


class FeishuResumeSettlementBoundaryTests(unittest.TestCase):
    def test_resume_entries_use_only_the_typed_settlement_service(self) -> None:
        trees = {
            path: ast.parse(path.read_text(encoding="utf-8"))
            for path in _RESUME_ENTRY_METHODS
        }
        methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for path, expected_names in _RESUME_ENTRY_METHODS.items():
            found = {
                node.name: node
                for node in ast.walk(trees[path])
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in expected_names
            }
            self.assertEqual(set(found), set(expected_names))
            methods.update(found)
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_thread_resume_was_acknowledged"
                for tree in trees.values()
                for node in ast.walk(tree)
            ),
            "a resume owner restored its local resume-ACK classifier",
        )

        violations: list[str] = []
        for method_name, method in methods.items():
            settlement_calls: set[str] = set()
            for node in ast.walk(method):
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func,
                    ast.Attribute,
                ):
                    continue
                if node.func.attr in {"settle_failure", "settle_success"}:
                    settlement_calls.add(node.func.attr)
                if node.func.attr == "_operation_start_outcome_unknown":
                    violations.append(
                        f"line {node.lineno}: {method_name} classifies outcome"
                    )
                owner = node.func.value
                if (
                    isinstance(owner, ast.Attribute)
                    and owner.attr
                    in {"_feishu_root_operations", "_root_operations"}
                    and node.func.attr in _CLOSED_ROOT_SETTLEMENT_CALLS
                ):
                    violations.append(
                        f"line {node.lineno}: {method_name} directly calls "
                        f"{node.func.attr}"
                    )
            if settlement_calls != {"settle_failure", "settle_success"}:
                violations.append(
                    f"{method_name} must use failure and success settlement"
                )

        self.assertEqual(violations, [])

    def test_handler_does_not_restore_continuation_paths(self) -> None:
        tree = ast.parse(_HANDLER_PATH.read_text(encoding="utf-8"))
        restored = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _REMOVED_HANDLER_CONTINUATION_METHODS
        }
        self.assertEqual(restored, set())

    def test_continuation_owner_keeps_private_restore_and_no_root_backref(self) -> None:
        source = _CONTINUATION_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("_restore_paused_goal_after_failed_resume", methods)
        self.assertNotIn("restore_paused_goal_after_failed_resume", methods)
        self.assertNotIn("FocusRuntime", source)

        assigned_self_attrs = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
        }
        self.assertFalse(
            any("callback" in name for name in assigned_self_attrs),
            assigned_self_attrs,
        )
        self.assertTrue(
            assigned_self_attrs.isdisjoint(
                {"_bot", "_focus_runtime", "_handler", "_runtime"}
            ),
            assigned_self_attrs,
        )


if __name__ == "__main__":
    unittest.main()
