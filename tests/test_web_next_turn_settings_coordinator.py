from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from bot.adapters.base import RuntimeModelSummary, RuntimeReasoningEffortOption
from bot.stores.web_next_turn_settings_store import (
    WebNextTurnSettings,
    WebNextTurnSettingsStore,
)
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.next_turn_settings_coordinator import (
    WebNextTurnSettingsCoordinator,
    WebNextTurnSettingsPorts,
)
from bot.web_runtime.projection import FocusWebProjection


class WebNextTurnSettingsCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.settings = WebNextTurnSettingsStore(
            pathlib.Path(self.temp_dir.name),
            initial=WebNextTurnSettings(
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
                model="gpt-test",
                reasoning_effort="high",
            ),
        )
        self.documents = WebDocumentRegistry(runtime_context_guard=lambda: None)
        self.documents.mark_connected("tab-1")
        self.documents.mark_connected("tab-2")
        self.projection = FocusWebProjection()
        self.list_models = Mock(
            return_value=[
                RuntimeModelSummary(
                    model="gpt-test",
                    supported_reasoning_efforts=[
                        RuntimeReasoningEffortOption("medium"),
                        RuntimeReasoningEffortOption("high"),
                    ],
                ),
                RuntimeModelSummary(
                    model="gpt-small",
                    supported_reasoning_efforts=[
                        RuntimeReasoningEffortOption("low")
                    ],
                ),
            ]
        )
        self.coordinator = WebNextTurnSettingsCoordinator(
            settings=self.settings,
            documents=self.documents,
            projection=self.projection,
            ports=WebNextTurnSettingsPorts(list_models=self.list_models),
            runtime_context_guard=lambda: None,
        )

    def test_update_is_instance_wide_and_returns_one_complete_snapshot(self) -> None:
        result = self.coordinator.update(
            "tab-1",
            changes={
                "model": "gpt-small",
                "reasoning_effort": "low",
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
            },
        )

        self.assertEqual(
            set(result),
            {"runtime_epoch", "revision", "next_turn_settings"},
        )
        expected = {
            "generation": 2,
            "model": "gpt-small",
            "reasoning_effort": "low",
            "approval_policy": "on-request",
            "permissions_profile_id": ":workspace",
        }
        self.assertEqual(result["next_turn_settings"], expected)
        self.assertEqual(self.coordinator.payload(), expected)
        self.assertEqual(self.settings.load().generation, 2)

    def test_model_and_effort_auto_are_explicit_empty_values(self) -> None:
        self.coordinator.update(
            "tab-1",
            changes={"model": "", "reasoning_effort": ""},
        )

        self.assertEqual(
            self.coordinator.payload(),
            {
                "generation": 2,
                "model": "",
                "reasoning_effort": "",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )

    def test_two_browsers_share_generation_and_server_commit_last_write_wins(self) -> None:
        first = self.coordinator.update(
            "tab-1",
            changes={"reasoning_effort": "medium"},
        )
        second = self.coordinator.update(
            "tab-2",
            changes={"reasoning_effort": "high"},
        )

        self.assertEqual(first["next_turn_settings"]["generation"], 2)
        self.assertEqual(second["next_turn_settings"]["generation"], 3)
        self.assertEqual(self.coordinator.snapshot().generation, 3)
        self.assertEqual(self.coordinator.snapshot().reasoning_effort, "high")

    def test_snapshot_remains_immutable_after_a_later_update(self) -> None:
        captured = self.coordinator.snapshot()

        self.coordinator.update(
            "tab-1",
            changes={"model": "gpt-small", "reasoning_effort": "low"},
        )

        self.assertEqual(captured.model, "gpt-test")
        self.assertEqual(captured.generation, 1)
        self.assertEqual(self.coordinator.snapshot().model, "gpt-small")
        self.assertEqual(self.coordinator.snapshot().generation, 2)

    def test_security_only_update_does_not_consult_model_catalog(self) -> None:
        hidden_settings = WebNextTurnSettingsStore(
            pathlib.Path(self.temp_dir.name) / "hidden",
            initial=WebNextTurnSettings(
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
                model="hidden-config-model",
                reasoning_effort="future-effort",
            ),
        )
        coordinator = WebNextTurnSettingsCoordinator(
            settings=hidden_settings,
            documents=self.documents,
            projection=self.projection,
            ports=WebNextTurnSettingsPorts(list_models=self.list_models),
            runtime_context_guard=lambda: None,
        )

        result = coordinator.update(
            "tab-1",
            changes={
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
            },
        )

        self.assertEqual(result["next_turn_settings"]["model"], "hidden-config-model")
        self.list_models.assert_not_called()

    def test_invalid_setting_values_fail_before_store_mutation(self) -> None:
        invalid = (
            ({"approval_policy": ""}, "invalid_settings"),
            ({"approval_policy": 1}, "invalid_settings"),
            ({"permissions_profile_id": ""}, "invalid_settings"),
            ({"permissions_profile_id": None}, "invalid_settings"),
            ({"model": "missing-model"}, "invalid_model"),
            ({"model": "gpt-small"}, "invalid_effort"),
        )
        for changes, code in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(WebRuntimeError) as caught:
                    self.coordinator.update("tab-1", changes=changes)
                self.assertEqual(caught.exception.code, code)

        self.assertEqual(self.settings.load().generation, 1)

    def test_interleaved_partial_updates_validate_the_latest_locked_merge(self) -> None:
        settings = WebNextTurnSettingsStore(
            pathlib.Path(self.temp_dir.name) / "interleaved",
            initial=WebNextTurnSettings(
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
                model="",
                reasoning_effort="",
            ),
        )
        coordinator = WebNextTurnSettingsCoordinator(
            settings=settings,
            documents=self.documents,
            projection=self.projection,
            ports=WebNextTurnSettingsPorts(list_models=self.list_models),
            runtime_context_guard=lambda: None,
        )
        update_under_lock = settings.update

        def commit_effort_before_model(changes, *, validate_merged=None):
            update_under_lock({"reasoning_effort": "high"})
            return update_under_lock(
                changes,
                validate_merged=validate_merged,
            )

        with patch.object(
            settings,
            "update",
            side_effect=commit_effort_before_model,
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                coordinator.update("tab-1", changes={"model": "gpt-small"})

        self.assertEqual(caught.exception.code, "invalid_effort")
        self.assertEqual(settings.load().model, "")
        self.assertEqual(settings.load().reasoning_effort, "high")
        self.assertEqual(settings.load().generation, 2)

    def test_projection_failure_does_not_undo_committed_settings(self) -> None:
        with patch.object(
            self.projection,
            "publish",
            side_effect=RuntimeError("projection failed"),
        ):
            result = self.coordinator.update(
                "tab-1",
                changes={"approval_policy": "on-request"},
            )

        self.assertEqual(result["next_turn_settings"]["generation"], 2)
        self.assertEqual(self.settings.load().approval_policy, "on-request")

    def test_disconnected_document_cannot_update_global_settings(self) -> None:
        with self.assertRaises(WebRuntimeError):
            self.coordinator.update(
                "missing-tab",
                changes={"approval_policy": "on-request"},
            )

        self.assertEqual(self.settings.load().generation, 1)

    def test_runtime_guard_runs_before_store_or_port_access(self) -> None:
        guard = Mock(side_effect=RuntimeError("wrong context"))
        settings = Mock(spec=WebNextTurnSettingsStore)
        ports = WebNextTurnSettingsPorts(list_models=Mock())
        coordinator = WebNextTurnSettingsCoordinator(
            settings=settings,
            documents=self.documents,
            projection=self.projection,
            ports=ports,
            runtime_context_guard=guard,
        )

        with self.assertRaisesRegex(RuntimeError, "wrong context"):
            coordinator.snapshot()

        settings.load.assert_not_called()
        ports.list_models.assert_not_called()

    def test_external_snapshot_reads_only_the_durable_owner(self) -> None:
        guard = Mock(side_effect=RuntimeError("wrong context"))
        settings = Mock(spec=WebNextTurnSettingsStore)
        expected = Mock()
        settings.load.return_value = expected
        coordinator = WebNextTurnSettingsCoordinator(
            settings=settings,
            documents=self.documents,
            projection=self.projection,
            ports=WebNextTurnSettingsPorts(list_models=Mock()),
            runtime_context_guard=guard,
        )

        self.assertIs(coordinator.load_external_snapshot(), expected)
        guard.assert_not_called()
        settings.load.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
