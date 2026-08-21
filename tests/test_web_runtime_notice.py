import unittest

from bot.web_runtime.runtime_notice import (
    RUNTIME_NOTICE_FIELD_LIMIT_BYTES,
    project_runtime_notice,
)


class WebRuntimeNoticeTests(unittest.TestCase):
    def test_error_preserves_typed_fields_without_text_interpretation(self) -> None:
        notice = project_runtime_notice(
            "error",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": False,
                "error": {
                    "message": " warning: do not infer retry ",
                    "additionalDetails": "Reconnecting...",
                    "codexErrorInfo": {"kind": "rateLimit"},
                },
                "futureField": "ignored",
            },
        )

        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertEqual(notice.thread_id, "thread-1")
        self.assertEqual(
            notice.detail,
            {
                "method": "error",
                "message": " warning: do not infer retry ",
                "additional_details": "Reconnecting...",
                "will_retry": False,
                "turn_id": "turn-1",
            },
        )

    def test_error_maps_absent_or_null_additional_details_to_empty_text(self) -> None:
        for error in ({"message": "failed"}, {"message": "failed", "additionalDetails": None}):
            with self.subTest(error=error):
                notice = project_runtime_notice(
                    "error",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "willRetry": True,
                        "error": error,
                    },
                )

                self.assertIsNotNone(notice)
                assert notice is not None
                self.assertEqual(notice.detail["additional_details"], "")

    def test_warning_supports_optional_thread_without_parsing_message(self) -> None:
        for params, expected_thread_id in (
            ({"message": "error willRetry=true"}, ""),
            ({"threadId": None, "message": "error willRetry=true"}, ""),
            (
                {"threadId": "thread-1", "message": "error willRetry=true"},
                "thread-1",
            ),
        ):
            with self.subTest(params=params):
                notice = project_runtime_notice("warning", params)

                self.assertIsNotNone(notice)
                assert notice is not None
                self.assertEqual(notice.thread_id, expected_thread_id)
                self.assertEqual(
                    notice.detail,
                    {"method": "warning", "message": "error willRetry=true"},
                )

    def test_malformed_typed_fields_fail_closed(self) -> None:
        malformed = (
            (
                "error",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": 1,
                    "error": {"message": "failed"},
                },
            ),
            (
                "error",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {"message": 7},
                },
            ),
            (
                "error",
                {
                    "threadId": "",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {"message": "failed"},
                },
            ),
            (
                "error",
                {
                    "threadId": "thread-1",
                    "turnId": "",
                    "willRetry": False,
                    "error": {"message": "failed"},
                },
            ),
            ("warning", {"threadId": 7, "message": "warning"}),
            ("warning", {"threadId": "", "message": "warning"}),
            ("warning", {"threadId": " thread-1 ", "message": "warning"}),
            ("warning", {"message": None}),
        )
        for method, params in malformed:
            with self.subTest(method=method, params=params):
                self.assertIsNone(project_runtime_notice(method, params))

    def test_oversized_text_is_dropped_instead_of_truncated(self) -> None:
        admitted = "x" * RUNTIME_NOTICE_FIELD_LIMIT_BYTES
        oversized = admitted + "x"

        notice = project_runtime_notice("warning", {"message": admitted})

        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertEqual(notice.detail["message"], admitted)
        self.assertIsNone(
            project_runtime_notice("warning", {"message": oversized})
        )


if __name__ == "__main__":
    unittest.main()
