from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_codex_app_server_drift.py"
SPEC = importlib.util.spec_from_file_location("focus_schema_drift_guard", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


def _request(method: str, params_ref: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "method": {"enum": [method], "type": "string"},
            "params": {"$ref": f"#/definitions/{params_ref}"},
        },
        "required": ["id", "method", "params"],
    }


def _notification(method: str, params_ref: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "method": {"enum": [method], "type": "string"},
            "params": {"$ref": f"#/definitions/{params_ref}"},
        },
        "required": ["method", "params"],
    }


def _schema_documents() -> dict[str, dict]:
    return {
        "ClientRequest.json": {
            "definitions": {
                "ThreadTurnsListParams": {
                    "type": "object",
                    "properties": {
                        "threadId": {"type": "string"},
                        "page": {"$ref": "#/definitions/TurnsPageOptions"},
                    },
                    "required": ["threadId"],
                },
                "TurnsPageOptions": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer"}},
                },
            },
            "oneOf": [_request("thread/turns/list", "ThreadTurnsListParams")],
        },
        "ServerRequest.json": {
            "definitions": {
                "CurrentTimeReadParams": {"type": "object", "properties": {}},
            },
            "oneOf": [_request("currentTime/read", "CurrentTimeReadParams")],
        },
        "ServerNotification.json": {
            "definitions": {
                "ThreadStartedNotification": {
                    "type": "object",
                    "properties": {"threadId": {"type": "string"}},
                    "required": ["threadId"],
                },
                "WarningNotification": {
                    "type": "object",
                    "properties": {
                        "threadId": {"type": ["string", "null"]},
                        "message": {"type": "string"},
                    },
                    "required": ["message"],
                },
            },
            "oneOf": [
                _notification("thread/started", "ThreadStartedNotification"),
                _notification("warning", "WarningNotification"),
            ],
        },
        "codex_app_server_protocol.v2.schemas.json": {
            "definitions": {
                "ThreadItem": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                                "type": {"enum": ["agentMessage"], "type": "string"},
                            },
                            "required": ["id", "text", "type"],
                        }
                    ]
                }
            }
        },
        "codex_app_server_protocol.schemas.json": {"definitions": {}},
    }


def _policy_baseline() -> dict:
    return {
        "format_version": 1,
        "upstream": {"commit": "test", "generator": "test --experimental"},
        "manual_required_methods": {
            "client_request": [],
            "server_request": [],
            "server_notification": [],
        },
        "fcodex_unscoped_client_request_policy": {
            "default_action": "deny",
            "allowed": [],
        },
        "method_classification": {
            "client_request": {
                "shared_operation_mutation": [],
                "observer_read": ["thread/turns/list"],
                "connection_local_request": [],
                "explicit_admin_control_plane": [],
            },
            "server_request": {},
            "server_notification": {"focus_projection": ["thread/started"]},
        },
        "thread_item_classification": {"projection": ["agentMessage"]},
        "response_roots": {"client_request": {}, "server_request": {}},
    }


class CodexAppServerSchemaDriftGuardTests(unittest.TestCase):
    def _write_fixture(self, directory: Path, documents: dict[str, dict]) -> None:
        for name, document in documents.items():
            (directory / name).write_text(json.dumps(document), encoding="utf-8")

    def _write_source(
        self,
        directory: Path,
        text: str = "",
        *,
        adapter_text: str = "class CodexAppServerAdapter:\n    pass\n",
    ) -> None:
        bot_dir = directory / "bot"
        bot_dir.mkdir()
        (bot_dir / "consumer.py").write_text(text, encoding="utf-8")
        adapter_dir = bot_dir / "adapters"
        adapter_dir.mkdir()
        (adapter_dir / "codex_app_server.py").write_text(adapter_text, encoding="utf-8")

    def _write_proxy_source(self, directory: Path, text: str) -> None:
        proxy_dir = directory / "bot" / "fcodex"
        proxy_dir.mkdir(parents=True, exist_ok=True)
        (proxy_dir / "proxy.py").write_text(text, encoding="utf-8")

    def _baseline_with_generated_fields(self, schema_dir: Path) -> dict:
        baseline = _policy_baseline()
        schema_input = GUARD.load_schema_input(schema_dir)
        baseline.update(GUARD.build_generated_baseline_fields(schema_input, baseline))
        return baseline

    def _check(self, schema_dir: Path, baseline: dict, source_root: Path) -> list[str]:
        baseline_path = source_root / "baseline.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        errors, _generated = GUARD.check(
            schema_dir,
            baseline_path,
            source_root=source_root,
        )
        return errors

    def _check_adapter_source(self, adapter_text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            self._write_fixture(schema_dir, _schema_documents())
            self._write_source(source_root, adapter_text=adapter_text)
            baseline = self._baseline_with_generated_fields(schema_dir)
            return self._check(schema_dir, baseline, source_root)

    def test_current_semantic_snapshot_passes_without_upstream_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            self._write_fixture(schema_dir, _schema_documents())
            self._write_source(source_root, 'METHOD = "thread/turns/list"\nITEM = "agentMessage"\n')
            baseline = self._baseline_with_generated_fields(schema_dir)

            self.assertEqual(self._check(schema_dir, baseline, source_root), [])

    def test_adapter_official_literal_is_covered_by_pinned_inventory(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def list_turns(self):\n"
            "        return self._rpc_request('thread/turns/list', {})\n"
        )

        self.assertEqual(errors, [])

    def test_adapter_unsupported_literal_is_rejected_before_schema_intersection(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def start(self):\n"
            "        return self._rpc_request('turn/privateStart', {})\n"
        )

        self.assertTrue(
            any(
                "absent from the pinned official ClientRequest inventory" in error
                and "turn/privateStart" in error
                for error in errors
            ),
            errors,
        )

    def test_adapter_unique_local_literal_assignment_is_checked(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def start(self):\n"
            "        method = 'turn/privateStart'\n"
            "        return self._request_turn_start(method)\n"
        )

        self.assertTrue(
            any("turn/privateStart" in error for error in errors),
            errors,
        )

    def test_adapter_dynamic_method_expression_fails_closed(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def request(self, prefix):\n"
            "        return self._rpc_request(f'{prefix}/read', {})\n"
        )

        self.assertTrue(
            any("cannot prove _rpc_request method" in error for error in errors),
            errors,
        )

    def test_adapter_nested_helper_outbound_method_fails_closed(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def start(self):\n"
            "        def hidden():\n"
            "            return self._rpc_request('turn/privateNested', {})\n"
            "        return hidden()\n"
        )

        self.assertTrue(
            any("nested scope" in error and "_rpc_request" in error for error in errors),
            errors,
        )

    def test_adapter_lambda_outbound_method_fails_closed(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def start(self):\n"
            "        hidden = lambda: self._rpc_request('turn/privateLambda', {})\n"
            "        return hidden()\n"
        )

        self.assertTrue(
            any("nested scope" in error and "_rpc_request" in error for error in errors),
            errors,
        )

    def test_adapter_bound_request_alias_fails_closed(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def start(self):\n"
            "        request = self._rpc_request\n"
            "        return request('turn/privateAlias', {})\n"
        )

        self.assertTrue(
            any("cannot be read or bound indirectly" in error for error in errors),
            errors,
        )

    def test_adapter_getattr_request_sink_fails_closed(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def start(self):\n"
            "        request = getattr(self, '_rpc_request')\n"
            "        return request('turn/privateGetattr', {})\n"
        )

        self.assertTrue(
            any("cannot be obtained through getattr" in error for error in errors),
            errors,
        )

    def test_adapter_parameter_is_not_hidden_by_one_conditional_assignment(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def request(self, method, use_official):\n"
            "        if use_official:\n"
            "            method = 'turn/start'\n"
            "        return self._rpc_request(method, {})\n"
        )

        self.assertTrue(
            any("cannot prove _rpc_request method" in error for error in errors),
            errors,
        )

    def test_reviewed_forwarder_parameter_cannot_be_reassigned(self) -> None:
        errors = self._check_adapter_source(
            "class CodexAppServerAdapter:\n"
            "    def _rpc_request(self, method, prefix):\n"
            "        method = prefix + '/read'\n"
            "        return self._rpc.request(method, {})\n"
        )

        self.assertTrue(
            any("cannot prove _rpc.request method" in error for error in errors),
            errors,
        )

    def test_real_adapter_outbound_methods_are_in_pinned_inventory(self) -> None:
        baseline = GUARD.load_baseline(GUARD.DEFAULT_BASELINE)
        outbound_methods = GUARD._codex_app_server_adapter_outbound_methods(REPO_ROOT)
        pinned_methods = GUARD._pinned_client_request_methods(baseline)

        self.assertTrue(outbound_methods)
        self.assertEqual(outbound_methods - pinned_methods, set())

    def test_new_thread_target_is_not_auto_classified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            baseline_documents = copy.deepcopy(documents)
            self._write_fixture(schema_dir, baseline_documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)

            documents["ClientRequest.json"]["definitions"]["NewMutationParams"] = {
                "type": "object",
                "properties": {"threadId": {"type": "string"}},
                "required": ["threadId"],
            }
            documents["ClientRequest.json"]["oneOf"].append(
                _request("thread/newMutation", "NewMutationParams")
            )
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("thread/newMutation" in error and "lack an explicit classification" in error for error in errors),
                errors,
            )

    def test_new_unscoped_client_method_still_forces_inventory_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)

            documents["ClientRequest.json"]["definitions"]["NewGlobalReadParams"] = {
                "type": "object",
                "properties": {"cursor": {"type": "string"}},
            }
            documents["ClientRequest.json"]["oneOf"].append(
                _request("global/newRead", "NewGlobalReadParams")
            )
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("upstream app-server drift in method_inventory" in error for error in errors),
                errors,
            )

    def test_unscoped_policy_must_default_to_deny(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)
            baseline["fcodex_unscoped_client_request_policy"]["default_action"] = "allow"

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("default_action must be 'deny'" in error for error in errors),
                errors,
            )

    def test_proxy_allowlist_must_match_reviewed_unscoped_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            documents["ClientRequest.json"]["definitions"]["ConfigReadParams"] = {
                "type": "object",
                "properties": {"cwd": {"type": "string"}},
            }
            documents["ClientRequest.json"]["oneOf"].append(
                _request("config/read", "ConfigReadParams")
            )
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = _policy_baseline()
            baseline["method_classification"]["client_request"]["observer_read"].append(
                "config/read"
            )
            baseline["fcodex_unscoped_client_request_policy"]["allowed"] = ["config/read"]
            schema_input = GUARD.load_schema_input(schema_dir)
            baseline.update(GUARD.build_generated_baseline_fields(schema_input, baseline))
            self._write_proxy_source(
                source_root,
                "_FCODEX_UNSCOPED_ALLOWED_CLIENT_REQUEST_METHODS = frozenset(\n"
                "    {\"config/read\", \"thread/turns/list\"}\n"
                ")\n",
            )

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("fcodex proxy unscoped client-request allowlist differs" in error for error in errors),
                errors,
            )

    def test_new_server_request_forces_inventory_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)

            documents["ServerRequest.json"]["definitions"]["NewApprovalParams"] = {
                "type": "object",
                "properties": {"threadId": {"type": "string"}},
                "required": ["threadId"],
            }
            documents["ServerRequest.json"]["oneOf"].append(
                _request("item/newApproval", "NewApprovalParams")
            )
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("upstream app-server drift in method_inventory" in error for error in errors),
                errors,
            )

    def test_new_item_variant_forces_inventory_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)

            documents["codex_app_server_protocol.v2.schemas.json"]["definitions"]["ThreadItem"][
                "oneOf"
            ].append(
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"enum": ["newItem"], "type": "string"},
                    },
                    "required": ["id", "type"],
                }
            )
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("upstream app-server drift in thread_item_inventory" in error for error in errors),
                errors,
            )

    def test_nested_schema_change_for_a_focus_method_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)

            documents["ClientRequest.json"]["definitions"]["TurnsPageOptions"]["properties"][
                "cursor"
            ] = {"type": "string"}
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("upstream app-server drift in focused_schema" in error for error in errors),
                errors,
            )

    def test_warning_schema_change_for_focus_projection_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = _policy_baseline()
            baseline["manual_required_methods"]["server_notification"].append(
                "warning"
            )
            baseline["method_classification"]["server_notification"][
                "focus_projection"
            ].append("warning")
            baseline.update(
                GUARD.build_generated_baseline_fields(
                    GUARD.load_schema_input(schema_dir),
                    baseline,
                )
            )
            self.assertIn(
                "warning",
                baseline["focused_schema"]["server_notification"],
            )

            documents["ServerNotification.json"]["definitions"][
                "WarningNotification"
            ]["properties"]["code"] = {"type": "string"}
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any(
                    "upstream app-server drift in focused_schema" in error
                    and '"warning"' in error
                    for error in errors
                ),
                errors,
            )

    def test_stable_schema_is_rejected_when_focus_requires_experimental_api(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schema_dir = root / "schema"
            source_root = root / "focus"
            schema_dir.mkdir()
            source_root.mkdir()
            documents = _schema_documents()
            self._write_fixture(schema_dir, documents)
            self._write_source(source_root)
            baseline = self._baseline_with_generated_fields(schema_dir)

            documents["ClientRequest.json"]["oneOf"] = []
            self._write_fixture(schema_dir, documents)

            errors = self._check(schema_dir, baseline, source_root)
            self.assertTrue(
                any("not the required experimental surface" in error for error in errors),
                errors,
            )


if __name__ == "__main__":
    unittest.main()
