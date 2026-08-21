from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from multidict import MultiDict

from bot.web_runtime import gateway_request_decoder as request_decoder
from bot.web_runtime.contract import WebRuntimeError


_MAX_SAFE_INTEGER = 9_007_199_254_740_991


class WebGatewayJsonObjectDecoderTests(unittest.IsolatedAsyncioTestCase):
    def assert_runtime_error(
        self,
        error: WebRuntimeError,
        *,
        code: str,
        message: str,
        status: int = 400,
    ) -> None:
        self.assertEqual(error.code, code)
        self.assertEqual(str(error), message)
        self.assertEqual(error.status, status)

    async def test_json_object_returns_the_exact_mapping(self) -> None:
        body = {"value": [1, 2, 3]}
        request = SimpleNamespace(json=AsyncMock(return_value=body))

        decoded = await request_decoder.decode_json_object(request)

        self.assertIs(decoded, body)

    async def test_json_object_rejects_parser_failure_and_non_mappings(self) -> None:
        requests = [
            SimpleNamespace(json=AsyncMock(side_effect=ValueError("bad JSON"))),
            *(
                SimpleNamespace(json=AsyncMock(return_value=value))
                for value in (None, [], "text", 1, True)
            ),
        ]
        for request in requests:
            with self.subTest(request=request):
                with self.assertRaises(WebRuntimeError) as caught:
                    await request_decoder.decode_json_object(request)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_json",
                    message="Request body must be a JSON object.",
                )


class WebGatewayRequestFieldDecoderTests(unittest.TestCase):
    def assert_runtime_error(
        self,
        error: WebRuntimeError,
        *,
        code: str,
        message: str,
        status: int = 400,
    ) -> None:
        self.assertEqual(error.code, code)
        self.assertEqual(str(error), message)
        self.assertEqual(error.status, status)

    def test_attachment_ids_preserve_existing_normalization(self) -> None:
        self.assertEqual(request_decoder.decode_attachment_ids({}), [])
        self.assertEqual(
            request_decoder.decode_attachment_ids(
                {"attachment_ids": [" first ", "", "first"]}
            ),
            ["first", "", "first"],
        )

        for value in (None, "attachment", ("attachment",), [1], ["ok", None]):
            with self.subTest(value=value):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_attachment_ids({"attachment_ids": value})
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_attachment",
                    message="attachment_ids must be an array of strings.",
                )

    def test_exact_text_preserves_whitespace_contract(self) -> None:
        for value in ("text", "internal space"):
            with self.subTest(value=value):
                self.assertTrue(request_decoder.is_exact_text(value))
        for value in (None, 1, "", " ", " leading", "trailing "):
            with self.subTest(value=value):
                self.assertFalse(request_decoder.is_exact_text(value))

    def test_safe_integer_accepts_exact_javascript_range(self) -> None:
        self.assertEqual(
            request_decoder.decode_safe_integer_field(
                {"generation": 1},
                "generation",
                positive=True,
            ),
            1,
        )
        self.assertEqual(
            request_decoder.decode_safe_integer_field(
                {"generation": 0},
                "generation",
                positive=False,
            ),
            0,
        )
        for positive in (False, True):
            with self.subTest(positive=positive):
                self.assertEqual(
                    request_decoder.decode_safe_integer_field(
                        {"generation": _MAX_SAFE_INTEGER},
                        "generation",
                        positive=positive,
                    ),
                    _MAX_SAFE_INTEGER,
                )

    def test_safe_integer_rejects_wrong_type_range_and_missing(self) -> None:
        cases = [
            ({}, True, "positive"),
            ({"generation": True}, True, "positive"),
            ({"generation": 0}, True, "positive"),
            ({"generation": -1}, False, "non-negative"),
            ({"generation": 1.0}, True, "positive"),
            ({"generation": "1"}, True, "positive"),
            ({"generation": _MAX_SAFE_INTEGER + 1}, False, "non-negative"),
        ]
        for body, positive, qualifier in cases:
            with self.subTest(body=body, positive=positive):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_safe_integer_field(
                        body,
                        "generation",
                        positive=positive,
                    )
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_submission_scope",
                    message=f"generation must be a {qualifier} safe integer.",
                )

    def test_intent_generation_preserves_existing_integer_lexing(self) -> None:
        cases = {
            None: 0,
            "": 0,
            "   ": 0,
            0: 0,
            "0": 0,
            "+7": 7,
            "01": 1,
            str(_MAX_SAFE_INTEGER): _MAX_SAFE_INTEGER,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    request_decoder.decode_intent_generation(raw),
                    expected,
                )

        for raw in (-1, str(_MAX_SAFE_INTEGER + 1), "1.5", "not-an-int"):
            with self.subTest(raw=raw):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_intent_generation(raw)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_intent",
                    message="Invalid browser intent generation.",
                )

    def test_request_connection_generation_is_exact_positive_safe_integer(self) -> None:
        for value in (1, _MAX_SAFE_INTEGER):
            with self.subTest(value=value):
                self.assertEqual(
                    request_decoder.decode_request_connection_generation(
                        {"connection_generation": value}
                    ),
                    value,
                )

        for value in (None, True, 0, -1, 1.0, "1", _MAX_SAFE_INTEGER + 1):
            with self.subTest(value=value):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_request_connection_generation(
                        {"connection_generation": value}
                    )
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_request_generation",
                    message="A positive Codex connection generation is required.",
                )

    def test_backend_reset_body_is_exact_bool_and_positive_safe_generation(self) -> None:
        self.assertEqual(
            request_decoder.decode_backend_reset_request(
                {
                    "force": True,
                    "expected_connection_generation": _MAX_SAFE_INTEGER,
                }
            ),
            (True, _MAX_SAFE_INTEGER),
        )
        invalid = (
            {},
            {"force": False},
            {"expected_connection_generation": 7},
            {"force": False, "expected_connection_generation": 7, "instance": "default"},
            {"force": 0, "expected_connection_generation": 7},
            {"force": "false", "expected_connection_generation": 7},
            {"force": False, "expected_connection_generation": True},
            {"force": False, "expected_connection_generation": 0},
            {"force": False, "expected_connection_generation": 1.0},
            {
                "force": False,
                "expected_connection_generation": _MAX_SAFE_INTEGER + 1,
            },
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_backend_reset_request(body)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_backend_reset_request",
                    message=(
                        "Backend reset body must contain only an exact boolean force "
                        "and a positive safe expected_connection_generation."
                    ),
                )

    def test_response_capability_preserves_exact_text_and_length(self) -> None:
        for value in ("x", "internal space", "x" * 256):
            with self.subTest(value=value):
                self.assertEqual(
                    request_decoder.decode_request_response_capability(
                        {"response_capability": value}
                    ),
                    value,
                )

        for value in (None, 1, "", " leading", "trailing ", "x" * 257):
            with self.subTest(value=value):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_request_response_capability(
                        {"response_capability": value}
                    )
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_response_capability",
                    message="An exact response capability is required.",
                )

    def test_client_id_hint_preserves_existing_coercion(self) -> None:
        for value in (None, "", "   ", 0, False):
            with self.subTest(value=value):
                self.assertEqual(request_decoder.decode_client_id_hint(value), "")
        self.assertEqual(request_decoder.decode_client_id_hint(" client "), "client")
        self.assertEqual(request_decoder.decode_client_id_hint(7), "7")

        for value in ("internal space", "tab\tvalue", "x" * 129):
            with self.subTest(value=value):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_client_id_hint(value)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_client",
                    message="Invalid browser client id.",
                )

    def test_document_incarnation_preserves_existing_coercion(self) -> None:
        self.assertEqual(
            request_decoder.decode_document_incarnation(" document "),
            "document",
        )
        self.assertEqual(request_decoder.decode_document_incarnation(7), "7")

        for value in (None, "", "   ", 0, False, "internal space", "x" * 129):
            with self.subTest(value=value):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_document_incarnation(value)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_document",
                    message="Invalid browser document incarnation.",
                )

    def test_tool_detail_query_requires_a_closed_view_and_exact_cursor(self) -> None:
        self.assertEqual(
            request_decoder.decode_tool_detail_query(MultiDict({"view": "preview"})),
            ("preview", None, None),
        )
        self.assertEqual(
            request_decoder.decode_tool_detail_query(
                MultiDict(
                    {
                        "view": "full",
                        "change_index": "3",
                        "cursor": "opaque-page",
                    }
                )
            ),
            ("full", 3, "opaque-page"),
        )
        for query in (
            MultiDict(),
            MultiDict({"view": "raw"}),
            MultiDict([("view", "preview"), ("view", "full")]),
            MultiDict({"view": "preview", "other": "0"}),
            MultiDict(
                [("view", "preview"), ("change_index", "0"), ("change_index", "1")]
            ),
            *(
                MultiDict({"view": "preview", "change_index": raw})
                for raw in (
                    "",
                    "00",
                    "01",
                    "+1",
                    "-1",
                    " 1",
                    "1 ",
                    "１",
                    "4294967296",
                )
            ),
            MultiDict({"view": "preview", "cursor": ""}),
            MultiDict({"view": "preview", "cursor": " padded "}),
            MultiDict(
                [("view", "preview"), ("cursor", "one"), ("cursor", "two")]
            ),
            MultiDict({"view": "preview", "cursor": "x" * 4097}),
        ):
            with self.subTest(query=query):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_tool_detail_query(query)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_tool_detail_query",
                    message=(
                        "Tool detail requires view=preview or view=full, one canonical unsigned "
                        "32-bit change_index at most, and at most one exact cursor."
                    ),
                )

    def test_conversation_search_query_is_bounded_and_closed(self) -> None:
        self.assertEqual(
            request_decoder.decode_conversation_search_query(
                MultiDict({"query": "  needle  "})
            ),
            ("needle", None),
        )
        self.assertEqual(
            request_decoder.decode_conversation_search_query(
                MultiDict({"query": "😀", "cursor": "opaque cursor"})
            ),
            ("😀", "opaque cursor"),
        )
        self.assertEqual(
            request_decoder.decode_conversation_search_query(
                MultiDict({"query": "界" * 256, "cursor": "x" * 4096})
            ),
            ("界" * 256, "x" * 4096),
        )

        invalid = (
            MultiDict(),
            MultiDict({"query": "needle", "other": "value"}),
            MultiDict([("query", "one"), ("query", "two")]),
            MultiDict([("query", "needle"), ("cursor", "one"), ("cursor", "two")]),
            MultiDict({"query": ""}),
            MultiDict({"query": "   "}),
            MultiDict({"query": "界" * 257}),
            MultiDict({"query": "needle", "cursor": ""}),
            MultiDict({"query": "needle", "cursor": " cursor"}),
            MultiDict({"query": "needle", "cursor": "cursor "}),
            MultiDict({"query": "needle", "cursor": "x" * 4097}),
        )
        for query in invalid:
            with self.subTest(query=query):
                with self.assertRaises(WebRuntimeError) as caught:
                    request_decoder.decode_conversation_search_query(query)
                self.assert_runtime_error(
                    caught.exception,
                    code="invalid_conversation_search_query",
                    message=(
                        "Conversation search requires one query of 1..256 Unicode "
                        "characters and at most one exact cursor."
                    ),
                )


if __name__ == "__main__":
    unittest.main()
