import ast
import pathlib
import re
import unittest

from bot.focus_web_wire_catalog import (
    FOCUS_WEB_ENDPOINTS,
    FOCUS_WEB_ENUM_BY_NAME,
    FOCUS_WEB_EVENT_BY_NAME,
    FOCUS_WEB_EVENTS,
    FOCUS_WEB_RECORD_BY_NAME,
    FOCUS_WEB_RECORDS,
    FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES,
    FOCUS_WEB_WIRE_VERSION,
    require_focus_web_event_type,
)
from bot.adapters.base import (
    RuntimeModelServiceTier,
    RuntimeModelSummary,
    RuntimeModelUpgradeInfo,
    RuntimeReasoningEffortOption,
    ThreadGoalSummary,
    ThreadSummary,
)
from bot.web_runtime.gateway import WebGateway
from bot.web_runtime.projection import (
    FocusWebProjection,
    project_goal,
    project_model,
    project_owner,
    project_pending_request,
    project_thread_summary,
)
from bot.web_runtime.runtime_notice import project_runtime_notice
from bot.operational_warnings import OperationalWarningRegistry
from scripts.generate_focus_web_wire import check_output


_INTERFACE_RE = re.compile(
    r"export\s+interface\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s+extends\s+(?P<parents>[^\{]+))?\s*\{"
)
_PROPERTY_RE = re.compile(
    r"^\s*(?:readonly\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<optional>\?)?\s*:"
    r"\s*(?P<type>.*)$"
)


def _matching_brace(source: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unterminated TypeScript interface")


def _typescript_interfaces(
    source: str,
) -> dict[str, tuple[tuple[str, ...], dict[str, tuple[bool, str]]]]:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    interfaces: dict[str, tuple[tuple[str, ...], dict[str, tuple[bool, str]]]] = {}
    for match in _INTERFACE_RE.finditer(source):
        opening = source.index("{", match.start())
        closing = _matching_brace(source, opening)
        fields: dict[str, tuple[bool, str]] = {}
        depth = 0
        for line in source[opening + 1 : closing].splitlines():
            if depth == 0 and (property_match := _PROPERTY_RE.match(line)):
                fields[property_match.group("name")] = (
                    property_match.group("optional") is not None,
                    property_match.group("type").strip().rstrip(";"),
                )
            depth += line.count("{") - line.count("}")
        parents = tuple(
            parent.strip()
            for parent in (match.group("parents") or "").split(",")
            if parent.strip()
        )
        interfaces[match.group("name")] = (parents, fields)
    return interfaces


def _focus_typescript_contract_source(root: pathlib.Path) -> str:
    focus_root = root / "web" / "src" / "focus"
    # ToolCall and the Focus wire DTO both reference inspection identity. Keep
    # that shared type in a cycle-free leaf while checking it as part of the
    # same public Focus TypeScript contract surface.
    return "\n".join(
        (focus_root / relative_path).read_text(encoding="utf-8")
        for relative_path in ("types.ts", "threadInspectionTypes.ts")
    )


def _required_interface_fields(
    interfaces: dict[str, tuple[tuple[str, ...], dict[str, tuple[bool, str]]]],
    name: str,
) -> set[str]:
    parents, fields = interfaces[name]
    required = {field for field, (optional, _type) in fields.items() if not optional}
    for parent in parents:
        required.update(_required_interface_fields(interfaces, parent))
    return required


def _assert_catalog_record(
    testcase: unittest.TestCase,
    name: str,
    value: dict[str, object],
) -> None:
    record = FOCUS_WEB_RECORD_BY_NAME[name]
    testcase.assertLessEqual(set(record.required_fields), set(value), name)
    for field, enum_name in record.enum_fields:
        if field in value:
            testcase.assertIn(value[field], FOCUS_WEB_ENUM_BY_NAME[enum_name].values)


class FocusWebWireCatalogTests(unittest.TestCase):
    def test_internal_interaction_scope_does_not_cross_v14_wire(self) -> None:
        self.assertEqual(FOCUS_WEB_WIRE_VERSION, 14)
        pending = project_pending_request(
            {
                "request_key": "request-1",
                "connection_generation": 3,
                "response_capability": "capability-3",
                "method": "item/tool/requestUserInput",
                "params": {
                    "questions": [{"id": "q1", "question": "Continue?", "options": []}]
                },
                "thread_id": "thread-1",
                "owner_thread_id": "thread-1",
                "turn_id": "turn-1",
                "status": "pending",
                "delivery_scope": "shared_interaction",
            }
        )

        self.assertNotIn("delivery_scope", pending)
        _assert_catalog_record(self, "pending_request", pending)

    def test_runtime_notice_wire_shape_and_limit_have_one_catalog_owner(self) -> None:
        self.assertEqual(FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES, 16 * 1024)
        error_notice = project_runtime_notice(
            "error",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": True,
                "error": {
                    "message": "temporary failure",
                    "additionalDetails": "retry scheduled",
                },
            },
        )
        self.assertIsNotNone(error_notice)
        assert error_notice is not None
        _assert_catalog_record(
            self,
            "runtime_error_notice_detail",
            dict(error_notice.detail),
        )
        warning_notice = project_runtime_notice(
            "warning",
            {"message": "skills trimmed"},
        )
        self.assertIsNotNone(warning_notice)
        assert warning_notice is not None
        _assert_catalog_record(
            self,
            "runtime_warning_notice_detail",
            dict(warning_notice.detail),
        )

    def test_every_endpoint_resolves_one_gateway_handler(self) -> None:
        self.assertEqual(len(FOCUS_WEB_ENDPOINTS), 34)
        for endpoint in FOCUS_WEB_ENDPOINTS:
            with self.subTest(endpoint=endpoint.name):
                self.assertTrue(callable(getattr(WebGateway, endpoint.handler, None)))

    def test_backend_reset_records_are_closed_browser_projections(self) -> None:
        _assert_catalog_record(
            self,
            "backend_reset_preview",
            {
                "instance": "default",
                "status": "available",
                "reason_code": "",
                "reason_text": "safe",
                "expected_connection_generation": 7,
                "pending_request_count": 0,
                "running_binding_count": 0,
                "attached_binding_count": 0,
                "active_loaded_thread_count": 0,
                "loaded_thread_count": 0,
                "runtime_verification_failed": False,
            },
        )
        _assert_catalog_record(
            self,
            "backend_reset_result",
            {
                "force": False,
                "detached_binding_count": 0,
                "interrupted_binding_count": 0,
                "retired_request_count": 0,
                "purged_thread_count": 0,
                "projection_warnings": [],
            },
        )

    def test_event_admission_is_closed_and_thread_scope_is_catalogued(self) -> None:
        self.assertEqual(len(FOCUS_WEB_EVENTS), 15)
        self.assertEqual(
            {event.name for event in FOCUS_WEB_EVENTS if event.thread_scoped},
            {
                "mutation_reconciled",
                "mutation_unknown",
                "mutation_verified",
                "owner_changed",
                "pending_request_changed",
                "thread_delta",
                "thread_invalidated",
            },
        )
        for event_name in FOCUS_WEB_EVENT_BY_NAME:
            self.assertEqual(require_focus_web_event_type(event_name), event_name)
        with self.assertRaisesRegex(ValueError, "unknown Focus Web projection event"):
            require_focus_web_event_type("thread_typo")

        projection = FocusWebProjection()
        with self.assertRaisesRegex(ValueError, "unknown Focus Web projection event"):
            projection.publish("thread_typo")
        self.assertEqual(projection.coordinates()["revision"], 0)

    def test_gateway_has_no_parallel_named_api_route_inventory(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        module = ast.parse(
            (root / "bot" / "web_runtime" / "gateway.py").read_text(encoding="utf-8")
        )
        start = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_async"
        )
        named_api_literals = {
            argument.value
            for node in ast.walk(start)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("add_")
            for argument in node.args[:1]
            if isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and argument.value.startswith("/api/")
            and argument.value != "/api/{path:.*}"
        }
        self.assertEqual(named_api_literals, set())

    def test_typescript_projection_is_fresh(self) -> None:
        self.assertTrue(check_output())

    def test_catalog_required_fields_match_typescript_interfaces(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        types_source = _focus_typescript_contract_source(root)
        interfaces = _typescript_interfaces(types_source)

        for record in FOCUS_WEB_RECORDS:
            if not record.typescript_type:
                continue
            with self.subTest(record=record.name):
                self.assertIn(record.typescript_type, interfaces)
                self.assertEqual(
                    set(record.required_fields),
                    _required_interface_fields(interfaces, record.typescript_type),
                )

    def test_representative_python_producers_satisfy_catalog(self) -> None:
        warning_registry = OperationalWarningRegistry()
        warning_registry.record(
            code="runtime_queue_delay",
            source="RuntimeLoop",
            message="RuntimeLoop task queue delay exceeded its threshold.",
            attention="advisory",
        )
        operator_warning = warning_registry.snapshot()[0]
        _assert_catalog_record(self, "operator_warning", operator_warning)
        self.assertEqual(operator_warning["attention"], "advisory")

        next_turn_settings = {
            "generation": 1,
            "model": "",
            "reasoning_effort": "",
            "approval_policy": "never",
            "permissions_profile_id": ":danger-full-access",
        }
        _assert_catalog_record(self, "next_turn_settings", next_turn_settings)
        _assert_catalog_record(
            self,
            "next_turn_settings_result",
            {
                "runtime_epoch": "epoch-1",
                "revision": 1,
                "next_turn_settings": next_turn_settings,
            },
        )
        model = project_model(
            RuntimeModelSummary(
                model="gpt-test",
                supported_reasoning_efforts=[
                    RuntimeReasoningEffortOption("medium", "Balanced")
                ],
                service_tiers=[RuntimeModelServiceTier("default", "Default", "")],
                upgrade_info=RuntimeModelUpgradeInfo("gpt-next"),
            )
        )
        _assert_catalog_record(self, "model", model)
        _assert_catalog_record(
            self, "reasoning_effort", model["supported_reasoning_efforts"][0]
        )
        _assert_catalog_record(self, "service_tier", model["service_tiers"][0])
        _assert_catalog_record(self, "model_upgrade_info", model["upgrade_info"])

        owner = project_owner(None)
        _assert_catalog_record(self, "owner", owner)
        thread = project_thread_summary(
            ThreadSummary(
                thread_id="thread-1",
                cwd="/work/project",
                name="Thread",
                preview="",
                created_at=1,
                updated_at=2,
                source="appServer",
                status="idle",
                history_mode="paginated",
            ),
            owner=owner,
        )
        _assert_catalog_record(self, "thread_summary", thread)
        self.assertEqual(thread["history_mode"], "paginated")
        _assert_catalog_record(
            self,
            "thread_action_capabilities",
            thread["action_capabilities"],
        )

        goal = project_goal(
            ThreadGoalSummary(
                thread_id="thread-1",
                objective="Ship",
                status="active",
                token_budget=100,
            )
        )
        self.assertIsNotNone(goal)
        assert goal is not None
        _assert_catalog_record(self, "goal", goal)
        _assert_catalog_record(self, "goal_budget", goal["budget"])

        pending = project_pending_request(
            {
                "request_key": "request-1",
                "connection_generation": 3,
                "response_capability": "capability-3",
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "pytest"},
                "thread_id": "thread-1",
                "owner_thread_id": "thread-1",
                "turn_id": "turn-1",
                "status": "pending",
            }
        )
        _assert_catalog_record(self, "pending_request", pending)
        for action in pending["actions"]:
            _assert_catalog_record(self, "interaction_action", action)

        locator = {
            "turn_id": "turn-1",
            "item_id": "item-1",
            "kind": "fileChange",
            "change_index": 0,
        }
        _assert_catalog_record(self, "tool_inspection_locator", locator)
        _assert_catalog_record(
            self,
            "tool_detail_preview",
            {
                "view": "preview",
                "tool": {"id": "item-1", "name": "Edit", "arg": "", "status": "ok"},
            },
        )
        _assert_catalog_record(
            self,
            "tool_detail_full",
            {
                "view": "full",
                "source": {
                    "type": "fileChange",
                    "id": "item-1",
                    "changes": [],
                    "status": "completed",
                },
            },
        )
        _assert_catalog_record(
            self,
            "thread_tool_detail_scan_page",
            {
                **FocusWebProjection().coordinates(),
                "thread_id": "thread-1",
                **locator,
                "status": "scanning",
                "cursor": None,
                "next_cursor": "next",
                "scanned_items": 1,
                "view": "preview",
                "detail": None,
            },
        )
        match_range = {"start": 3, "end": 9}
        _assert_catalog_record(
            self,
            "conversation_search_match_range",
            match_range,
        )
        occurrence = {
            "turn_id": "turn-1",
            "item_id": "final-1",
            "snippet": "😀 needle",
            "snippet_match_range": match_range,
            "turn_cursor": "turn-cursor-1",
        }
        _assert_catalog_record(self, "conversation_search_occurrence", occurrence)
        _assert_catalog_record(
            self,
            "thread_conversation_search_page",
            {
                **FocusWebProjection().coordinates(),
                "thread_id": "thread-1",
                "query": "needle",
                "cursor": None,
                "occurrences": [occurrence],
                "next_cursor": None,
            },
        )

        event = FocusWebProjection().publish(
            "thread_invalidated",
            thread_id="thread-1",
            reason="turn/started",
        )
        _assert_catalog_record(self, "projection_event", event)
        self.assertIn(event["type"], FOCUS_WEB_EVENT_BY_NAME)

    def test_catalog_enum_fields_drive_typescript_types(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        types_source = _focus_typescript_contract_source(root)
        interfaces = _typescript_interfaces(types_source)
        aliases = {
            match.group("alias"): match.group("enum")
            for match in re.finditer(
                r"export\s+type\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                r"FocusWebWireEnum<'(?P<enum>[a-z][a-z0-9_]*)'>\s*;",
                types_source,
                flags=re.DOTALL,
            )
        }
        self.assertRegex(
            types_source,
            r"export\s+interface\s+FocusProjectionEvent[^{]*\{[^}]*"
            r"\btype:\s*FocusWebEventType\s*;",
        )

        for record in FOCUS_WEB_RECORDS:
            if not record.typescript_type:
                continue
            _parents, fields = interfaces[record.typescript_type]
            for field, enum_name in record.enum_fields:
                with self.subTest(record=record.name, field=field):
                    self.assertIn(enum_name, FOCUS_WEB_ENUM_BY_NAME)
                    self.assertIn(field, fields)
                    field_type = fields[field][1]
                    direct_type = f"FocusWebWireEnum<'{enum_name}'>"
                    literal = re.fullmatch(r"['\"](?P<value>[^'\"]+)['\"]", field_type)
                    self.assertTrue(
                        (
                            direct_type in field_type
                            or aliases.get(field_type) == enum_name
                            or (
                                literal is not None
                                and literal.group("value")
                                in FOCUS_WEB_ENUM_BY_NAME[enum_name].values
                            )
                        ),
                        f"{record.typescript_type}.{field} does not consume {enum_name}",
                    )

    def test_browser_decoders_have_no_parallel_top_level_vocabulary(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        focus_root = root / "web" / "src" / "focus"
        http_source = (focus_root / "httpResponseDecoder.ts").read_text(
            encoding="utf-8"
        )
        event_source = (focus_root / "projectionEventDecoder.ts").read_text(
            encoding="utf-8"
        )
        state_source = (focus_root / "client-state" / "thread-mutations.ts").read_text(
            encoding="utf-8"
        )

        for obsolete_name in (
            "CAPABILITY_KEYS",
            "THREAD_ACTION_KEYS",
            "GOAL_STATUSES",
            "TASK_STATES",
            "TASK_EXECUTION_STATES",
            "RECOVERY_PHASES",
        ):
            self.assertNotIn(obsolete_name, http_source + event_source)
        self.assertNotIn(".includes(", http_source)
        self.assertNotIn("['present', 'archived', 'deleted']", state_source)
        self.assertNotIn("['archive', 'unarchive', 'delete']", state_source)
        self.assertIn("hasFocusWebRequiredFields", http_source)
        self.assertIn("hasFocusWebRequiredFields", event_source)
        self.assertIn("isFocusWebWireEnum", http_source)
        self.assertIn("isFocusWebWireEnum", event_source)
        decoder_source = http_source + event_source
        for record in FOCUS_WEB_RECORDS:
            with self.subTest(record=record.name):
                self.assertTrue(
                    f"isRequiredRecord('{record.name}'" in decoder_source
                    or f"hasFocusWebRequiredFields('{record.name}'" in decoder_source
                    or f"isExactRequiredRecord('{record.name}'" in decoder_source
                )
        self.assertIn("const record = FOCUS_WEB_RECORDS[name]", event_source)
        vocabulary_consumers = (
            decoder_source
            + state_source
            + (focus_root / "types.ts").read_text(encoding="utf-8")
        )
        for enum in FOCUS_WEB_ENUM_BY_NAME.values():
            with self.subTest(enum=enum.name):
                self.assertIn(f"'{enum.name}'", vocabulary_consumers)

    def test_browser_api_and_event_decoder_have_no_parallel_inventory(self) -> None:
        root = pathlib.Path(__file__).parents[1]
        api_source = (root / "web" / "src" / "focus" / "api.ts").read_text(
            encoding="utf-8"
        )
        decoder_source = (
            root / "web" / "src" / "focus" / "projectionEventDecoder.ts"
        ).read_text(encoding="utf-8")
        sync_source = (
            root / "web" / "src" / "focus" / "focusProjectionSync.ts"
        ).read_text(encoding="utf-8")
        self.assertNotIn("/api/", api_source)
        self.assertNotIn("const KNOWN_EVENT_TYPES", decoder_source)
        self.assertNotIn("const THREAD_SCOPED_EVENT_TYPES", decoder_source)
        self.assertNotIn(
            "isKnownFocusProjectionEventType", decoder_source + sync_source
        )
        self.assertIn("./focusWire.generated", api_source)
        self.assertIn("./focusWire.generated", decoder_source)
        self.assertIn("isFocusWebEventType", decoder_source)


if __name__ == "__main__":
    unittest.main()
