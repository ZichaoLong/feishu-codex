import unittest

from bot.interaction_contract import (
    COMMAND_APPROVAL,
    CURRENT_TIME_READ,
    FILE_APPROVAL,
    MCP_ELICITATION,
    PERMISSIONS_APPROVAL,
    USER_INPUT,
    automatic_server_request_response,
    fail_closed_interaction_response,
    interaction_response_payload,
    normalize_interaction_request,
)


class InteractionContractTests(unittest.TestCase):
    def test_current_time_is_an_exact_stateless_protocol_utility(self) -> None:
        self.assertEqual(
            automatic_server_request_response(
                CURRENT_TIME_READ,
                {"threadId": "thread-1"},
                current_time_at=1_781_717_655,
            ),
            ({"currentTimeAt": 1_781_717_655}, None),
        )
        for params in (
            {},
            {"threadId": ""},
            {"threadId": " thread-1 "},
            {"threadId": "thread-1", "future": True},
            [],
        ):
            with self.subTest(params=params):
                result, error = automatic_server_request_response(
                    CURRENT_TIME_READ,
                    params,
                    current_time_at=1_781_717_655,
                ) or (None, None)
                self.assertIsNone(result)
                self.assertEqual(error and error["code"], -32602)

        self.assertIsNone(
            automatic_server_request_response(
                "future/read",
                {"threadId": "thread-1"},
                current_time_at=1_781_717_655,
            )
        )

    def test_command_approval_preserves_declared_decisions_and_amendments(self) -> None:
        amendment = {
            "acceptWithExecpolicyAmendment": {
                "execpolicy_amendment": {"command": ["git", "status"]}
            }
        }
        normalized = normalize_interaction_request(
            COMMAND_APPROVAL,
            {
                "command": "git status",
                "availableDecisions": ["accept", amendment, "cancel"],
                "networkApprovalContext": {"host": "example.com"},
                "additionalPermissions": {"network": {"enabled": True}},
            },
        )

        self.assertEqual(
            [action["id"] for action in normalized["actions"]],
            ["approve_once", "approve_execpolicy_amendment", "cancel"],
        )
        result, error = interaction_response_payload(
            COMMAND_APPROVAL,
            {
                "availableDecisions": ["accept", amendment, "cancel"],
            },
            action="approve_execpolicy_amendment",
        )
        self.assertEqual(result, {"decision": amendment})
        self.assertIsNone(error)

    def test_command_network_amendment_uses_exact_upstream_schema_and_label(self) -> None:
        amendment = {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {
                    "host": "example.com",
                    "action": "allow",
                }
            }
        }
        normalized = normalize_interaction_request(
            COMMAND_APPROVAL,
            {"availableDecisions": [amendment, "cancel"]},
        )

        self.assertEqual(
            [action["label"] for action in normalized["actions"]],
            ["Allow network policy for example.com", "Cancel turn"],
        )
        result, error = interaction_response_payload(
            COMMAND_APPROVAL,
            {"availableDecisions": [amendment, "cancel"]},
            action="network_policy_0",
        )
        self.assertEqual(result, {"decision": amendment})
        self.assertIsNone(error)

    def test_approval_contract_exposes_all_upstream_scopes_exactly(self) -> None:
        command = normalize_interaction_request(
            COMMAND_APPROVAL,
            {
                "availableDecisions": [
                    "accept",
                    "acceptForSession",
                    "decline",
                    "cancel",
                ]
            },
        )
        file_change = normalize_interaction_request(FILE_APPROVAL, {})
        permissions = {"network": {"enabled": True}}
        permission_request = normalize_interaction_request(
            PERMISSIONS_APPROVAL,
            {"permissions": permissions},
        )

        self.assertEqual(
            [action["response"] for action in command["actions"]],
            [
                {"decision": "accept"},
                {"decision": "acceptForSession"},
                {"decision": "decline"},
                {"decision": "cancel"},
            ],
        )
        self.assertEqual(
            [action["response"] for action in file_change["actions"]],
            [
                {"decision": "accept"},
                {"decision": "acceptForSession"},
                {"decision": "decline"},
                {"decision": "cancel"},
            ],
        )
        self.assertEqual(
            [action["response"] for action in permission_request["actions"]],
            [
                {"permissions": permissions, "scope": "turn"},
                {
                    "permissions": permissions,
                    "scope": "turn",
                    "strictAutoReview": True,
                },
                {"permissions": permissions, "scope": "session"},
                {"permissions": {}, "scope": "turn"},
            ],
        )

    def test_user_input_preserves_secret_other_and_auto_resolution(self) -> None:
        params = {
            "questions": [
                {
                    "id": "token",
                    "header": "Credential",
                    "question": "Enter token",
                    "isSecret": True,
                    "isOther": True,
                    "options": [{"label": "Use default", "description": ""}],
                }
            ],
            "autoResolutionMs": 15_000,
        }

        normalized = normalize_interaction_request(USER_INPUT, params)

        self.assertTrue(normalized["presentable"])
        self.assertEqual(normalized["params"]["autoResolutionMs"], 15_000)
        self.assertTrue(normalized["params"]["questions"][0]["isSecret"])
        self.assertTrue(normalized["params"]["questions"][0]["isOther"])
        result, error = interaction_response_payload(
            USER_INPUT,
            params,
            action="answer",
            answers={"token": "custom secret"},
        )
        self.assertEqual(result, {"answers": {"token": {"answers": ["custom secret"]}}})
        self.assertIsNone(error)

    def test_user_input_rejects_undeclared_value_without_other(self) -> None:
        params = {
            "questions": [
                {
                    "id": "choice",
                    "header": "Choice",
                    "question": "Pick one",
                    "isOther": False,
                    "options": [{"label": "A", "description": ""}],
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "declared options"):
            interaction_response_payload(
                USER_INPUT,
                params,
                action="answer",
                answers={"choice": "B"},
            )

    def test_supported_mcp_form_is_typed_and_validated(self) -> None:
        params = {
            "mode": "form",
            "message": "Configure",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "confirmed": {"type": "boolean"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                    "mode": {"type": "string", "enum": ["fast", "safe"]},
                    "labels": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["a", "b"]},
                    },
                },
                "required": ["confirmed", "count", "mode"],
            },
        }

        normalized = normalize_interaction_request(MCP_ELICITATION, params)
        self.assertTrue(normalized["presentable"])
        result, error = interaction_response_payload(
            MCP_ELICITATION,
            params,
            action="accept",
            answers={
                "confirmed": "true",
                "count": "2",
                "mode": "safe",
                "labels": ["a", "b"],
            },
        )
        self.assertEqual(
            result,
            {
                "action": "accept",
                "content": {
                    "confirmed": True,
                    "count": 2,
                    "mode": "safe",
                    "labels": ["a", "b"],
                },
                "_meta": None,
            },
        )
        self.assertIsNone(error)

    def test_mcp_form_enforces_format_and_collection_constraints(self) -> None:
        params = {
            "mode": "form",
            "message": "Configure",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "format": "email", "minLength": 6},
                    "when": {"type": "string", "format": "date-time"},
                    "labels": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 2,
                        "items": {"type": "string", "enum": ["a", "b", "c"]},
                    },
                },
                "required": ["email", "when", "labels"],
            },
        }

        invalid_answers = (
            {"email": "bad", "when": "2026-07-28T10:00:00Z", "labels": ["a"]},
            {"email": "a@example.com", "when": "not-a-date", "labels": ["a"]},
            {"email": "a@example.com", "when": "2026-07-28T10:00:00Z", "labels": []},
            {"email": "a@example.com", "when": "2026-07-28T10:00:00Z", "labels": ["a", "b", "c"]},
        )
        for answers in invalid_answers:
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                interaction_response_payload(
                    MCP_ELICITATION,
                    params,
                    action="accept",
                    answers=answers,
                )

        result, error = interaction_response_payload(
            MCP_ELICITATION,
            params,
            action="accept",
            answers={
                "email": "a@example.com",
                "when": "2026-07-28T10:00:00Z",
                "labels": ["a", "b"],
            },
        )
        self.assertEqual(
            result,
            {
                "action": "accept",
                "content": {
                    "email": "a@example.com",
                    "when": "2026-07-28T10:00:00Z",
                    "labels": ["a", "b"],
                },
                "_meta": None,
            },
        )
        self.assertIsNone(error)

    def test_mcp_password_format_is_presentable_and_preserved(self) -> None:
        params = {
            "mode": "form",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "format": "password",
                        "minLength": 4,
                    }
                },
                "required": ["token"],
                "additionalProperties": False,
            },
        }

        self.assertTrue(
            normalize_interaction_request(MCP_ELICITATION, params)["presentable"]
        )
        result, error = interaction_response_payload(
            MCP_ELICITATION,
            params,
            action="accept",
            answers={"token": "secret"},
        )

        self.assertEqual(
            result,
            {"action": "accept", "content": {"token": "secret"}, "_meta": None},
        )
        self.assertIsNone(error)

    def test_mcp_form_with_unvalidated_or_malformed_constraints_fails_closed(self) -> None:
        invalid_fields = (
            {"type": "string", "pattern": "^safe$"},
            {"type": "string", "format": "hostname"},
            {"type": "string", "minLength": "1"},
            {"type": "number", "minimum": "1"},
            {"type": "array", "minItems": 2, "maxItems": 1, "items": {"enum": ["a"]}},
            {"type": "string", "enum": []},
        )

        for field in invalid_fields:
            with self.subTest(field=field):
                normalized = normalize_interaction_request(
                    MCP_ELICITATION,
                    {
                        "mode": "form",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"value": field},
                        },
                    },
                )
                self.assertFalse(normalized["presentable"])

    def test_unsupported_mcp_modes_fail_closed_with_valid_cancel(self) -> None:
        for mode in ("openai/form", "url"):
            with self.subTest(mode=mode):
                params = {"mode": mode, "message": "Need input"}
                self.assertFalse(normalize_interaction_request(MCP_ELICITATION, params)["presentable"])
                result, error = fail_closed_interaction_response(
                    MCP_ELICITATION,
                    params,
                    message="unsupported",
                )
                self.assertEqual(result, {"action": "cancel", "content": None, "_meta": None})
                self.assertIsNone(error)

    def test_command_fail_close_uses_valid_cancel_decision(self) -> None:
        result, error = fail_closed_interaction_response(
            COMMAND_APPROVAL,
            {},
            message="writer unavailable",
        )
        self.assertEqual(result, {"decision": "cancel"})
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
