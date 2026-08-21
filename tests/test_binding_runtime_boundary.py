from __future__ import annotations

import ast
import pathlib
import unittest


_RAW_RUNTIME_TRUST_ZONE = frozenset(
    {
        "bot/adapter_notification_runtime.py",
        "bot/binding_execution_runtime.py",
        "bot/binding_runtime_lifecycle.py",
        "bot/binding_runtime_manager.py",
        "bot/binding_runtime_snapshot.py",
        "bot/binding_runtime_state_factory.py",
        "bot/execution_output_runtime.py",
        "bot/execution_recovery_runtime.py",
        "bot/feishu_execution_finalization_controller.py",
        "bot/runtime_state.py",
        "bot/turn_execution_coordinator.py",
    }
)

_TRUST_ZONE_ONLY_IDENTIFIERS = frozenset(
    {
        "RuntimeStateDict",
        "_apply_persisted_runtime_state_message_locked",
        "_apply_runtime_state_message_locked",
        "_get_or_create_runtime_state_locked",
        "_sync_resident_state_locked",
        "apply_runtime_state_message_locked",
        "resident_runtime_state_locked",
    }
)

_REMOVED_LEGACY_IDENTIFIERS = frozenset(
    {
        "ResolvedRuntimeBinding",
        "RuntimeView",
        "apply_persisted_runtime_state_message_locked",
        "build_runtime_view",
        "get_or_create_runtime_state_locked",
        "get_runtime_state",
        "get_runtime_view",
        "resolve_runtime_binding",
        "sync_stored_binding_locked",
        "visit_runtime_states_locked",
    }
)

_CLOSED_THREAD_TRANSITION_METHODS = frozenset(
    {"bind_thread_locked", "clear_thread_binding_locked"}
)
_CLOSED_THREAD_TRANSITION_OWNER = "bot/feishu_binding_transition.py"


class BindingRuntimeBoundaryTests(unittest.TestCase):
    def test_raw_runtime_trust_zone_is_fail_closed(self) -> None:
        source_root = pathlib.Path(__file__).resolve().parents[1]
        bot_root = source_root / "bot"
        production_files = tuple(sorted(bot_root.rglob("*.py")))
        production_paths = {
            path.relative_to(source_root).as_posix() for path in production_files
        }
        self.assertEqual(
            _RAW_RUNTIME_TRUST_ZONE - production_paths,
            frozenset(),
            "binding runtime trust-zone allowlist contains missing modules",
        )

        violations: set[str] = set()
        for path in production_files:
            relative = path.relative_to(source_root).as_posix()
            tree = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
            in_trust_zone = relative in _RAW_RUNTIME_TRUST_ZONE
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "bot.runtime_view":
                            violations.add(
                                f"{relative}:{node.lineno}: removed runtime_view import"
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "bot.runtime_view" or (
                        node.module == "bot"
                        and any(
                            alias.name == "runtime_view" for alias in node.names
                        )
                    ):
                        violations.add(
                            f"{relative}:{node.lineno}: removed runtime_view import"
                        )

                identifier = self._identifier(node)
                if identifier is None:
                    continue
                if (
                    isinstance(node, ast.Attribute)
                    and identifier in _CLOSED_THREAD_TRANSITION_METHODS
                    and relative != _CLOSED_THREAD_TRANSITION_OWNER
                ):
                    violations.add(
                        f"{relative}:{node.lineno}: direct binding thread transition "
                        f"{identifier}"
                    )
                if identifier in _REMOVED_LEGACY_IDENTIFIERS:
                    violations.add(
                        f"{relative}:{node.lineno}: removed {identifier}"
                    )
                if (
                    not in_trust_zone
                    and identifier in _TRUST_ZONE_ONLY_IDENTIFIERS
                ):
                    violations.add(
                        f"{relative}:{node.lineno}: raw trust-zone identifier "
                        f"{identifier}"
                    )

        self.assertEqual(sorted(violations), [])

    @staticmethod
    def _identifier(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name
        if isinstance(node, ast.alias):
            return node.asname or node.name.rsplit(".", 1)[-1]
        return None


if __name__ == "__main__":
    unittest.main()
