from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot.backend_reset.cli import reset_service_backend
from bot.backend_reset.contract import BackendResetResultContractError
from bot.service_control_plane import ServiceControlOutcomeUnknownError


def _valid_result(*, force: bool = False) -> dict[str, object]:
    return {
        "force": force,
        "app_server_url": "ws://127.0.0.1:9001",
        "detached_binding_ids": [],
        "interrupted_binding_ids": [],
        "retired_request_count": 4,
        "purged_thread_ids": ["transient-root"],
        "projection_warnings": ["detach thread-1: store unavailable"],
    }


class BackendResetCliTest(unittest.TestCase):
    def test_prints_committed_result_and_projection_warnings(self) -> None:
        result = _valid_result()
        stdout = io.StringIO()

        with (
            patch(
                "bot.backend_reset.cli.control_request",
                return_value=result,
            ) as control_request,
            redirect_stdout(stdout),
        ):
            exit_code = reset_service_backend(
                Path("/tmp/instance"),
                force=False,
                instance_name="corp-b",
            )

        self.assertEqual(exit_code, 0)
        control_request.assert_called_once_with(
            Path("/tmp/instance"),
            "service/reset-backend",
            {"force": False},
            timeout_seconds=30.0,
        )
        rendered = stdout.getvalue()
        self.assertIn("retired old-epoch requests: 4", rendered)
        self.assertNotIn("fail-close response writes", rendered)
        self.assertIn("cleared transient runtime leases: transient-root", rendered)
        self.assertIn(
            "projection warning: detach thread-1: store unavailable",
            rendered,
        )

    def test_malformed_result_is_unknown_before_any_success_output(self) -> None:
        malformed_results = [
            {},
            _valid_result(force=True),
            {**_valid_result(), "projection_warnings": [1]},
            {**_valid_result(), "response_submitted_count": 0},
        ]

        for result in malformed_results:
            with self.subTest(result=result):
                stdout = io.StringIO()
                with (
                    patch(
                        "bot.backend_reset.cli.control_request",
                        return_value=result,
                    ) as control_request,
                    redirect_stdout(stdout),
                    self.assertRaises(ServiceControlOutcomeUnknownError) as caught,
                ):
                    reset_service_backend(
                        Path("/tmp/instance"),
                        force=False,
                        instance_name="corp-b",
                    )

                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(
                    "focusctl --instance corp-b service status",
                    str(caught.exception),
                )
                self.assertIsInstance(
                    caught.exception.__cause__,
                    BackendResetResultContractError,
                )
                control_request.assert_called_once_with(
                    Path("/tmp/instance"),
                    "service/reset-backend",
                    {"force": False},
                    timeout_seconds=30.0,
                )

    def test_force_is_an_exact_bool_before_request(self) -> None:
        with patch("bot.backend_reset.cli.control_request") as control_request:
            with self.assertRaises(TypeError):
                reset_service_backend(  # type: ignore[arg-type]
                    Path("/tmp/instance"),
                    force=1,
                    instance_name="corp-b",
                )

        control_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
