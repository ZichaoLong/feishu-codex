from __future__ import annotations

import unittest

from bot.backend_reset.contract import BackendResetPreview
from bot.focus_runtime.runtime import FocusRuntime


class _Lifecycle:
    def __init__(self) -> None:
        self.offline_maintenance_prepared = False
        self.events: list[str] = []

    def prepare_offline_maintenance(self, verify_idle):
        self.events.append("prepare")
        result = verify_idle()
        self.offline_maintenance_prepared = True
        return result

    def cancel_offline_maintenance(self) -> None:
        self.events.append("cancel")
        if not self.offline_maintenance_prepared:
            raise RuntimeError("no cancellable maintenance")
        self.offline_maintenance_prepared = False


class _RuntimeAdmin:
    def __init__(self, preview: BackendResetPreview) -> None:
        self.preview = preview
        self.calls: list[tuple[str, dict]] = []

    def backend_reset_preview(self) -> BackendResetPreview:
        return self.preview

    def handle_service_control_request(self, method: str, params: dict):
        self.calls.append((method, params))
        return {"method": method}


class FocusRuntimeMaintenanceControlTests(unittest.TestCase):
    def runtime(self, preview: BackendResetPreview) -> FocusRuntime:
        runtime = object.__new__(FocusRuntime)
        runtime._instance_name = "corp-a"
        runtime._service_runtime_lifecycle = _Lifecycle()
        runtime._runtime_admin = _RuntimeAdmin(preview)
        return runtime

    def test_available_preview_returns_matching_prepared_proof(self) -> None:
        runtime = self.runtime(
            BackendResetPreview(
                status="available",
                reason_code="",
                reason_text="idle",
                loaded_thread_ids=("thread-idle",),
            )
        )

        result = runtime._handle_service_control_request_impl(
            "service/prepare-offline-maintenance",
            {},
        )

        self.assertEqual(result["instance_name"], "corp-a")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["loaded_thread_count"], 1)
        self.assertEqual(result["active_loaded_thread_count"], 0)
        self.assertTrue(runtime._service_runtime_lifecycle.offline_maintenance_prepared)

    def test_active_or_unverified_preview_rejects_preparation(self) -> None:
        cases = (
            BackendResetPreview(
                status="force-only",
                reason_code="active",
                reason_text="仍有 active turn",
                active_loaded_thread_ids=("thread-active",),
            ),
            BackendResetPreview(
                status="force-only",
                reason_code="unverified",
                reason_text="运行态无法完整核验",
                runtime_verification_failed=True,
            ),
        )
        for preview in cases:
            with self.subTest(reason=preview.reason_code):
                runtime = self.runtime(preview)
                with self.assertRaisesRegex(RuntimeError, preview.reason_text):
                    runtime._handle_service_control_request_impl(
                        "service/prepare-offline-maintenance",
                        {},
                    )
                self.assertFalse(
                    runtime._service_runtime_lifecycle.offline_maintenance_prepared
                )

    def test_prepared_runtime_allows_status_and_cancel_but_rejects_mutation(self) -> None:
        runtime = self.runtime(
            BackendResetPreview(
                status="available",
                reason_code="",
                reason_text="idle",
            )
        )
        runtime._handle_service_control_request_impl(
            "service/prepare-offline-maintenance",
            {},
        )

        status = runtime._handle_service_control_request_impl("service/status", {})
        self.assertEqual(status, {"method": "service/status"})
        with self.assertRaisesRegex(RuntimeError, "除 status/cancel 外"):
            runtime._handle_service_control_request_impl(
                "binding/submit-prompt",
                {"text": "too late"},
            )

        cancelled = runtime._handle_service_control_request_impl(
            "service/cancel-offline-maintenance",
            {},
        )
        self.assertEqual(cancelled, {"instance_name": "corp-a", "status": "cancelled"})
        self.assertFalse(runtime._service_runtime_lifecycle.offline_maintenance_prepared)


if __name__ == "__main__":
    unittest.main()
