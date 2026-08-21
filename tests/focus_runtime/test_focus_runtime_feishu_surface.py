from __future__ import annotations

import ast
import copy
import hashlib
import pathlib
import unittest

import bot.focus_runtime.feishu_surface as feishu_surface_module
import bot.focus_runtime.runtime as focus_runtime_module

_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_SURFACE_PATH = pathlib.Path(feishu_surface_module.__file__).resolve()

_SURFACE_METHODS = (
    "handle_message_impl",
    "handle_message_recalled_impl",
    "should_bypass_runtime_for_card_action",
    "handle_card_action_impl",
    "seed_help_action_actor_context",
    "handle_help_execute_command_action",
    "handle_help_submit_command_action",
    "handle_help_form_fallback",
    "handle_settings_form_fallback",
    "handle_attachment_message_impl",
    "handle_user_input_form_fallback",
    "validate_group_mode_change",
    "preflight_group_prompt_impl",
    "should_route_group_followup_prompt_impl",
    "is_group_turn_actor",
    "is_group_request_actor_or_admin",
    "deactivate_group_chat",
    "build_command_routes",
    "build_action_routes",
    "build_prefixed_action_routes",
    "handle_prompt",
    "start_or_enqueue_prompt",
    "handle_compact_command",
    "handle_cd_command",
    "handle_new_command",
    "handle_status_command",
    "handle_last_command",
    "handle_goal_command",
    "handle_preflight_command",
    "handle_detach_command",
    "handle_attach_command",
    "submit_prompt_for_control",
    "handle_cancel_action",
    "cancel_current_turn",
    "handle_approval_card_action",
    "handle_user_input_action",
)
_EXTRACTED_ROOT_METHODS = {f"_{name}" for name in _SURFACE_METHODS} | {
    "_drain_feishu_execution_queue"
}
_ROOT_METHODS = {
    "__init__",
    "phase",
    "call",
    "submit",
    "status",
    "_operational_status_snapshot",
    "start",
    "_restore_service_runtime_state",
    "_restore_service_runtime_state_on_runtime",
    "_schedule_fcodex_participant_expiry",
    "_schedule_fcodex_connection_expiry",
    "_schedule_fcodex_proxy_delivery_expiry",
    "_schedule_web_runtime_cleanup",
    "_schedule_web_notification_projection",
    "_schedule_web_attachment_cleanup",
    "_restore_service_thread_runtime_leases",
    "handle_message",
    "handle_message_recalled",
    "handle_card_action",
    "handle_attachment_message",
    "is_sender_active",
    "deactivate_sender",
    "preflight_group_prompt",
    "should_route_group_followup_prompt",
    "accept_destination_loss_proof",
    "shutdown",
    "stop",
    "_mark_compact_start_outcome_unknown_for_session",
    "_mark_compact_start_outcome_unknown",
    "_operation_start_outcome_unknown",
    "_schedule_terminal_execution_reconcile",
    "_deliver_generated_images_from_snapshot",
    "_mark_runtime_degraded",
    "_schedule_mirror_watchdog",
    "_finalize_execution_for_recovery",
    "_finalize_execution_from_terminal_signal",
    "_reconcile_execution_snapshot",
    "_archive_thread_for_control",
    "_handle_service_control_request",
    "_handle_service_control_request_impl",
    "_verify_offline_maintenance_idle",
    "_reset_current_instance_backend",
}
_SURFACE_DEPENDENCIES = {
    "_lock",
    "_platform",
    "_binding_runtime",
    "_binding_runtime_coordinator",
    "_thread_access_policy",
    "_interaction_requests",
    "_feishu_execution_queue_service",
    "_feishu_thread_sessions",
    "_file_message_domain",
    "_prompt_turn_entry",
    "_runtime_admin",
    "_help_domain",
    "_settings_domain",
    "_group_domain",
    "_threads_ui_domain",
    "_goal_domain",
    "_terminal_results",
    "_turn_steer",
    "_inbound_surface",
}

_SURFACE_SIGNATURE_DIGEST = (
    "d9f6ca8e5bf9aedb42e038526b885f4c9c5157b01e7f45cca14b16bc0f482d46"
)
_SURFACE_NORMALIZED_BODY_DIGEST = (
    "6d2777366b16e7009276368c9909be8ca66b62e9e7af2e31b954e9056547bccd"
)
_PUBLIC_INGRESS_NORMALIZED_DIGEST = (
    "8ad5ab21a2a123e00c652be07bd38fdadda80205d42a4786fda2126063e9fc80"
)
_ROUTE_EXPECTATIONS = {
        "build_command_routes": (
        30,
        "bbe7bd786a12ec5c6c4656f2151b5ec65d04c041e8f614063b1ed356ad525575",
        "0ffc43f7c1107bf26a9c47da1b01cffbfe6ea5a55aa3cbc1debef846706365ba",
    ),
    "build_action_routes": (
        30,
        "1c426df709cdd273e499bd52f1218fa10b28d5f75c6a5b2fafad3ea9c210e3df",
        "c1007f13ed4bf8bccd2baccbd5b416eedf751ff7e6332f01145f1b7fa25e5de6",
    ),
    "build_prefixed_action_routes": (
        1,
        "3c3818cccab7e622d53c9255f243e326537ecd2d527436acbc348d3b5be7d688",
        "adbd925d284d146bd11c9944346be0e59b800014a22c3890a064756df5ce7137",
    ),
}
_PUBLIC_INGRESS_METHODS = (
    "handle_message",
    "handle_message_recalled",
    "handle_card_action",
    "handle_attachment_message",
    "preflight_group_prompt",
    "should_route_group_followup_prompt",
)
_PUBLIC_TO_OLD_SURFACE_METHOD = {
    "handle_message_impl": "_handle_message_impl",
    "handle_message_recalled_impl": "_handle_message_recalled_impl",
    "should_bypass_runtime_for_card_action": (
        "_should_bypass_runtime_for_card_action"
    ),
    "handle_card_action_impl": "_handle_card_action_impl",
    "handle_attachment_message_impl": "_handle_attachment_message_impl",
    "preflight_group_prompt_impl": "_preflight_group_prompt_impl",
    "should_route_group_followup_prompt_impl": (
        "_should_route_group_followup_prompt_impl"
    ),
}

# ``ast.dump`` is not a stable cross-version fingerprint: CPython 3.12 added
# ``type_params`` to FunctionDef/ClassDef and newer parsers include that empty
# field even for ordinary Python 3.11-compatible source. CI runs 3.11 while
# local development may use a newer interpreter, so structural baselines must
# serialize semantic AST fields explicitly and ignore only that version-added
# field.
_AST_VERSION_FIELDS = frozenset({"type_params"})


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_ast_dump(value: object) -> str:
    """Serialize semantic AST fields consistently on supported Python versions."""

    if isinstance(value, ast.AST):
        fields = (
            f"{field}={_canonical_ast_dump(getattr(value, field, None))}"
            for field in value._fields
            if field not in _AST_VERSION_FIELDS
        )
        return f"{type(value).__name__}({', '.join(fields)})"
    if isinstance(value, list):
        return f"[{', '.join(_canonical_ast_dump(item) for item in value)}]"
    if isinstance(value, tuple):
        return f"({', '.join(_canonical_ast_dump(item) for item in value)})"
    return repr(value)


def _class_node(path: pathlib.Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


def _methods(owner: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in owner.body
        if isinstance(node, ast.FunctionDef)
    }


def _self_stored_attrs(owner: ast.ClassDef) -> set[str]:
    return {
        node.attr
        for node in ast.walk(owner)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def _is_platform_bot_alias(statement: ast.stmt) -> bool:
    return bool(
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "bot"
        and ast.unparse(statement.value) == "self._platform.bot"
    )


def _platform_bot_attribute() -> ast.Attribute:
    return ast.Attribute(
        value=ast.Attribute(
            value=ast.Name(id="self", ctx=ast.Load()),
            attr="_platform",
            ctx=ast.Load(),
        ),
        attr="bot",
        ctx=ast.Load(),
    )


class _PlatformBotAliasNormalizer(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == "bot" and isinstance(node.ctx, ast.Load):
            return ast.copy_location(_platform_bot_attribute(), node)
        return node


def _normalized_surface_body(method: ast.FunctionDef) -> str:
    body = copy.deepcopy(method.body)
    has_platform_bot_alias = any(
        _is_platform_bot_alias(statement) for statement in body
    )
    if has_platform_bot_alias:
        body = [
            statement
            for statement in body
            if not _is_platform_bot_alias(statement)
        ]
    module = ast.Module(body=body, type_ignores=[])
    if has_platform_bot_alias:
        module = _PlatformBotAliasNormalizer().visit(module)
        ast.fix_missing_locations(module)
    return _canonical_ast_dump(module)


class _PublicIngressNormalizer(ast.NodeTransformer):
    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        node = self.generic_visit(node)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "_feishu_surface"
            and node.attr in _PUBLIC_TO_OLD_SURFACE_METHOD
        ):
            return ast.copy_location(
                ast.Attribute(
                    value=ast.Name(id="self", ctx=ast.Load()),
                    attr=_PUBLIC_TO_OLD_SURFACE_METHOD[node.attr],
                    ctx=node.ctx,
                ),
                node,
            )
        return node


def _normalized_public_ingress(method: ast.FunctionDef) -> str:
    normalized = _PublicIngressNormalizer().visit(copy.deepcopy(method))
    ast.fix_missing_locations(normalized)
    return _canonical_ast_dump(normalized)


def _route_keys(method: ast.FunctionDef) -> list[str]:
    returns = [node for node in method.body if isinstance(node, ast.Return)]
    if len(returns) != 1:
        raise AssertionError(
            f"expected one direct return in {method.name}, found {len(returns)}"
        )
    value = returns[0].value
    if isinstance(value, ast.Dict):
        return [str(ast.literal_eval(key)) for key in value.keys]
    if isinstance(value, ast.List):
        return [str(ast.literal_eval(item.elts[0])) for item in value.elts]
    raise AssertionError(f"unexpected route value: {type(value).__name__}")


class FeishuSurfaceBoundaryTests(unittest.TestCase):
    def test_ast_fingerprint_ignores_only_newer_parser_fields(self) -> None:
        source = "def sample(value):\n    return value\n"
        parsed = ast.parse(source).body[0]
        newer = copy.deepcopy(parsed)
        setattr(newer, "type_params", ["parser-only-placeholder"])

        self.assertEqual(_canonical_ast_dump(parsed), _canonical_ast_dump(newer))
        self.assertNotIn("parser-only-placeholder", _canonical_ast_dump(newer))

    def test_exact_36_methods_leave_root_and_drain_uses_queue_owner(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        surface = _class_node(_SURFACE_PATH, "FeishuSurface")
        root_methods = _methods(root)
        surface_methods = _methods(surface)
        root_self_refs = {
            node.attr
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }

        self.assertEqual(len(_SURFACE_METHODS), 36)
        self.assertEqual(
            set(surface_methods),
            {"__init__", *_SURFACE_METHODS},
        )
        self.assertTrue(_EXTRACTED_ROOT_METHODS.isdisjoint(root_methods))
        self.assertTrue(_EXTRACTED_ROOT_METHODS.isdisjoint(root_self_refs))

        direct_drain_refs = [
            node
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and ast.unparse(node)
            == "self._feishu_execution_queue_service.drain"
        ]
        self.assertEqual(len(direct_drain_refs), 1)
        finalization_ports = [
            node
            for node in ast.walk(root)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "FeishuExecutionFinalizationPorts"
        ]
        self.assertEqual(len(finalization_ports), 1)
        port_values = {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in finalization_ports[0].keywords
            if keyword.arg is not None
        }
        self.assertEqual(
            port_values["drain_execution_queue"],
            "self._feishu_execution_queue_service.drain",
        )

    def test_root_and_surface_stay_within_round_hard_limits(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        surface = _class_node(_SURFACE_PATH, "FeishuSurface")
        root_methods = _methods(root)
        surface_methods = _methods(surface)
        initializer = root_methods["__init__"]

        self.assertEqual(set(root_methods), _ROOT_METHODS)
        self.assertEqual(len(root_methods), 42)
        self.assertLessEqual(
            len(_ROOT_PATH.read_text(encoding="utf-8").splitlines()),
            2_450,
        )
        self.assertIsNotNone(initializer.end_lineno)
        assert initializer.end_lineno is not None
        self.assertLessEqual(
            initializer.end_lineno - initializer.lineno + 1,
            1_501,
        )
        self.assertEqual(len(surface_methods), 37)
        self.assertLess(
            len(_SURFACE_PATH.read_text(encoding="utf-8").splitlines()),
            1_500,
        )

    def test_surface_keeps_only_explicit_dependencies_and_no_root_backref(
        self,
    ) -> None:
        source = _SURFACE_PATH.read_text(encoding="utf-8")
        surface = _class_node(_SURFACE_PATH, "FeishuSurface")

        self.assertEqual(_self_stored_attrs(surface), _SURFACE_DEPENDENCIES)
        self.assertNotIn("FocusRuntime", source)
        self.assertNotIn("bot.focus_runtime.runtime", source)
        self.assertNotIn("self._bot", source)
        self.assertNotIn("self.bot", source)
        self.assertNotIn("threading.", source)

    def test_inbound_controller_is_constructed_and_installed_once(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        surface = _class_node(_SURFACE_PATH, "FeishuSurface")
        initializer = _methods(surface)["__init__"]
        controller_calls = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "InboundSurfaceController"
        ]
        install_calls = [
            node
            for node in ast.walk(initializer)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func)
            == "self._inbound_surface.install_routes"
        ]

        self.assertEqual(len(controller_calls), 1)
        self.assertEqual(len(install_calls), 1)
        self.assertLess(controller_calls[0].lineno, install_calls[0].lineno)
        install_values = {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in install_calls[0].keywords
            if keyword.arg is not None
        }
        self.assertEqual(
            install_values,
            {
                "command_routes": "self.build_command_routes()",
                "action_routes": "self.build_action_routes()",
                "prefixed_action_routes": (
                    "self.build_prefixed_action_routes()"
                ),
            },
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and ast.unparse(node.func)
                in {"InboundSurfaceController", "self._inbound_surface.install_routes"}
                for node in ast.walk(root)
            )
        )

    def test_route_keys_metadata_and_handlers_match_frozen_inventory(
        self,
    ) -> None:
        methods = _methods(_class_node(_SURFACE_PATH, "FeishuSurface"))

        for method_name, (
            expected_count,
            expected_key_digest,
            expected_body_digest,
        ) in _ROUTE_EXPECTATIONS.items():
            with self.subTest(method=method_name):
                method = methods[method_name]
                keys = _route_keys(method)
                body = ast.Module(
                    body=copy.deepcopy(method.body),
                    type_ignores=[],
                )
                self.assertEqual(len(keys), expected_count)
                self.assertEqual(len(set(keys)), expected_count)
                self.assertEqual(
                    _sha256("\0".join(keys)),
                    expected_key_digest,
                )
                self.assertEqual(
                    _sha256(_canonical_ast_dump(body)),
                    expected_body_digest,
                )

    def test_36_signatures_and_normalized_bodies_match_frozen_baseline(
        self,
    ) -> None:
        methods = _methods(_class_node(_SURFACE_PATH, "FeishuSurface"))
        signature_rows = [
            _canonical_ast_dump(methods[name].args)
            for name in _SURFACE_METHODS
        ]
        body_rows = [
            _normalized_surface_body(methods[name])
            for name in _SURFACE_METHODS
        ]

        self.assertEqual(
            _sha256("\n".join(signature_rows)),
            _SURFACE_SIGNATURE_DIGEST,
        )
        self.assertEqual(
            _sha256("\n".join(body_rows)),
            _SURFACE_NORMALIZED_BODY_DIGEST,
        )
        bypass = methods["should_bypass_runtime_for_card_action"]
        self.assertEqual(
            [ast.unparse(decorator) for decorator in bypass.decorator_list],
            ["staticmethod"],
        )

    def test_six_public_ingress_methods_match_pre_extraction_dispatch(
        self,
    ) -> None:
        methods = _methods(_class_node(_ROOT_PATH, "FocusRuntime"))
        normalized_rows = [
            _normalized_public_ingress(methods[name])
            for name in _PUBLIC_INGRESS_METHODS
        ]

        self.assertEqual(
            _sha256("\n".join(normalized_rows)),
            _PUBLIC_INGRESS_NORMALIZED_DIGEST,
        )


if __name__ == "__main__":
    unittest.main()
