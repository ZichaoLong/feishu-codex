from __future__ import annotations

import copy
import unittest

from bot.backend_reset.contract import (
    BACKEND_RESET_MAX_SAFE_CONNECTION_GENERATION,
    BackendResetResult,
    BackendResetResultContractError,
    decode_backend_reset_result,
    require_backend_reset_connection_generation,
)


def _valid_result(*, force: bool = False) -> dict[str, object]:
    return {
        "force": force,
        "detached_binding_ids": ["p2p:user:chat"],
        "interrupted_binding_ids": [],
        "retired_request_count": 0,
        "purged_thread_ids": ["thread-1"],
        "projection_warnings": ["detach projection unavailable"],
        "app_server_url": "ws://127.0.0.1:8765",
    }


class BackendResetResultContractTest(unittest.TestCase):
    def test_web_generation_is_an_exact_positive_safe_integer(self) -> None:
        for value in (1, BACKEND_RESET_MAX_SAFE_CONNECTION_GENERATION):
            with self.subTest(value=value):
                self.assertEqual(
                    require_backend_reset_connection_generation(value),
                    value,
                )
        for value in (
            None,
            True,
            False,
            0,
            -1,
            1.0,
            "1",
            BACKEND_RESET_MAX_SAFE_CONNECTION_GENERATION + 1,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    require_backend_reset_connection_generation(value)

    def assert_invalid(self, raw: object, *, expected_force: bool = False) -> None:
        with self.assertRaises(BackendResetResultContractError):
            decode_backend_reset_result(raw, expected_force=expected_force)

    def test_decodes_complete_result_into_immutable_values(self) -> None:
        raw = _valid_result(force=True)
        raw["detached_binding_ids"] = [" first ", "first"]
        raw["app_server_url"] = " ws://127.0.0.1:8765 "

        result = decode_backend_reset_result(raw, expected_force=True)

        self.assertIs(type(result), BackendResetResult)
        self.assertTrue(result.force)
        self.assertEqual(result.detached_binding_ids, ("first", "first"))
        self.assertEqual(result.interrupted_binding_ids, ())
        self.assertEqual(result.app_server_url, "ws://127.0.0.1:8765")

    def test_expected_force_is_a_local_exact_bool_precondition(self) -> None:
        for value in (None, 0, 1, "false"):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    decode_backend_reset_result(  # type: ignore[arg-type]
                        _valid_result(),
                        expected_force=value,
                    )

    def test_rejects_non_object_missing_and_extra_fields(self) -> None:
        for raw in (None, [], "result", 1):
            with self.subTest(raw=raw):
                self.assert_invalid(raw)

        valid = _valid_result()
        for field in tuple(valid):
            with self.subTest(missing=field):
                raw = copy.deepcopy(valid)
                del raw[field]
                self.assert_invalid(raw)

        for field in ("extra", "response_submitted_count"):
            with self.subTest(extra=field):
                raw = copy.deepcopy(valid)
                raw[field] = 0
                self.assert_invalid(raw)

    def test_rejects_invalid_or_mismatched_force(self) -> None:
        for value in (None, 0, 1, 0.0, "false", [], {}):
            with self.subTest(value=value):
                raw = _valid_result()
                raw["force"] = value
                self.assert_invalid(raw)

        self.assert_invalid(_valid_result(force=True), expected_force=False)
        self.assert_invalid(_valid_result(force=False), expected_force=True)

    def test_rejects_invalid_retired_request_count(self) -> None:
        for value in (True, False, -1, 0.0, "0", None):
            with self.subTest(value=value):
                raw = _valid_result()
                raw["retired_request_count"] = value
                self.assert_invalid(raw)

    def test_rejects_invalid_typed_lists_and_items(self) -> None:
        fields = (
            "detached_binding_ids",
            "interrupted_binding_ids",
            "purged_thread_ids",
            "projection_warnings",
        )
        for field in fields:
            for value in (None, "item", (), {}):
                with self.subTest(field=field, value=value):
                    raw = _valid_result()
                    raw[field] = value
                    self.assert_invalid(raw)
            for item in (None, 0, True, "", "  "):
                with self.subTest(field=field, item=item):
                    raw = _valid_result()
                    raw[field] = [item]
                    self.assert_invalid(raw)

    def test_rejects_blank_or_non_string_app_server_url(self) -> None:
        for value in (None, 0, True, [], "", "   "):
            with self.subTest(value=value):
                raw = _valid_result()
                raw["app_server_url"] = value
                self.assert_invalid(raw)


if __name__ == "__main__":
    unittest.main()
