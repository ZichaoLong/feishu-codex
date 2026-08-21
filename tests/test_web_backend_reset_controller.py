from __future__ import annotations

import unittest

from bot.backend_reset.contract import (
    BackendResetPreview,
    BackendResetResultContractError,
    BackendResetUnavailableError,
)
from bot.web_runtime.backend_reset_controller import (
    WebBackendResetController,
    WebBackendResetControllerPorts,
)


def _preview(*, status: str = "available") -> BackendResetPreview:
    return BackendResetPreview(
        status=status,
        reason_code="reason-code" if status != "available" else "",
        reason_text="policy reason",
        pending_request_count=2,
        running_binding_ids=("binding-a",),
        attached_binding_ids=("binding-a", "binding-b"),
        active_loaded_thread_ids=("thread-a",),
        loaded_thread_ids=("thread-a", "thread-b"),
        runtime_verification_failed=False,
    )


def _result(*, force: bool = False) -> dict[str, object]:
    return {
        "force": force,
        "detached_binding_ids": ["binding-a", "binding-b"],
        "interrupted_binding_ids": ["binding-a"],
        "retired_request_count": 3,
        "purged_thread_ids": ["thread-a"],
        "projection_warnings": ["card projection unavailable"],
        "app_server_url": "ws://127.0.0.1:8765",
    }


class WebBackendResetControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.preview_value = _preview()
        self.generation = 7
        self.generation_error: BaseException | None = None
        self.reset_calls: list[tuple[bool, int]] = []
        self.reset_result: object = _result()
        self.controller = WebBackendResetController(
            instance_name="Default",
            ports=WebBackendResetControllerPorts(
                backend_reset_preview=lambda: self.preview_value,
                preview_connection_generation=self._connection_generation,
                reset_current_instance=self._reset,
            ),
        )

    def _connection_generation(self) -> int:
        if self.generation_error is not None:
            raise self.generation_error
        return self.generation

    def _reset(
        self,
        *,
        force: bool,
        expected_connection_generation: int,
    ) -> object:
        self.reset_calls.append((force, expected_connection_generation))
        return self.reset_result

    def test_preview_projects_only_web_counts_and_exact_generation(self) -> None:
        self.assertEqual(
            self.controller.preview(),
            {
                "instance": "default",
                "status": "available",
                "reason_code": "",
                "reason_text": "policy reason",
                "expected_connection_generation": 7,
                "pending_request_count": 2,
                "running_binding_count": 1,
                "attached_binding_count": 2,
                "active_loaded_thread_count": 1,
                "loaded_thread_count": 2,
                "runtime_verification_failed": False,
            },
        )

    def test_unavailable_generation_cannot_be_reused_as_execute_authority(self) -> None:
        self.generation_error = BackendResetUnavailableError("not jointly available")

        preview = self.controller.preview()

        self.assertEqual(preview["status"], "unavailable")
        self.assertEqual(preview["expected_connection_generation"], 0)
        self.assertEqual(preview["reason_code"], "backend_generation_unavailable")
        self.assertNotIn("not jointly available", preview["reason_text"])

    def test_blocked_policy_is_projected_as_unavailable_without_generation(self) -> None:
        self.preview_value = _preview(status="blocked")

        preview = self.controller.preview()

        self.assertEqual(preview["status"], "unavailable")
        self.assertEqual(preview["expected_connection_generation"], 0)

    def test_execute_decodes_complete_br1_result_before_web_projection(self) -> None:
        self.reset_result = _result(force=True)

        result = self.controller.execute(
            force=True,
            expected_connection_generation=7,
        )

        self.assertEqual(self.reset_calls, [(True, 7)])
        self.assertEqual(
            result,
            {
                "force": True,
                "detached_binding_count": 2,
                "interrupted_binding_count": 1,
                "retired_request_count": 3,
                "purged_thread_count": 1,
                "projection_warnings": ["card projection unavailable"],
            },
        )
        self.assertNotIn("app_server_url", result)

    def test_malformed_or_force_mismatched_result_never_projects_success(self) -> None:
        for result in (
            {**_result(), "extra": True},
            {key: value for key, value in _result().items() if key != "app_server_url"},
            _result(force=True),
        ):
            with self.subTest(result=result):
                self.reset_result = result
                with self.assertRaises(BackendResetResultContractError):
                    self.controller.execute(
                        force=False,
                        expected_connection_generation=7,
                    )

    def test_execute_validates_exact_input_before_service_dispatch(self) -> None:
        for force, generation in (
            (1, 7),
            (False, True),
            (False, 0),
            (False, 1.0),
        ):
            with self.subTest(force=force, generation=generation):
                with self.assertRaises((TypeError, ValueError)):
                    self.controller.execute(
                        force=force,  # type: ignore[arg-type]
                        expected_connection_generation=generation,  # type: ignore[arg-type]
                    )
        self.assertEqual(self.reset_calls, [])


if __name__ == "__main__":
    unittest.main()
