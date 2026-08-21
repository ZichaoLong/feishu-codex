from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HANDLER_PATH = _REPO_ROOT / "bot" / "focus_runtime" / "runtime.py"
_REMOVED_HANDLER_METHODS = frozenset(
    {
        "_begin_resume_snapshot_by_id",
        "_commit_resolved_thread_binding_owner",
        "_create_and_bind_feishu_thread",
        "_finish_thread_binding",
        "_preflight_replaced_binding_owner",
        "_prepare_resume_snapshot_by_id",
        "_reattach_bound_feishu_thread",
        "_resume_and_commit_feishu_binding",
        "_resume_and_commit_feishu_operation_owner",
    }
)
_CLOSED_RECEIPT_CALLS = frozenset(
    {
        "begin_resume_thread",
        "commit_local_state",
        "commit_resume_owner",
        "create_and_commit_thread",
    }
)


class FeishuThreadSessionBoundaryTests(unittest.TestCase):
    def test_handler_cannot_rebuild_create_resume_receipt_transaction(self) -> None:
        tree = ast.parse(_HANDLER_PATH.read_text(encoding="utf-8"))
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in _REMOVED_HANDLER_METHODS:
                    violations.append(
                        f"line {node.lineno}: restored Handler method {node.name}"
                    )
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in _CLOSED_RECEIPT_CALLS:
                violations.append(
                    f"line {node.lineno}: direct receipt call {node.func.attr}"
                )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
