from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from bot.binding_runtime_state_factory import BindingRuntimeStateFactory
from bot.execution_pages import ExecutionPageLedger
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import RuntimeStateDict


class BindingRuntimeStateFactoryTests(unittest.TestCase):
    @staticmethod
    def _factory() -> BindingRuntimeStateFactory:
        return BindingRuntimeStateFactory(
            default_working_dir=" /tmp/default ",
            default_approval_policy=" on-request ",
            default_permissions_profile_id="workspace-write",
            default_model=" gpt-default ",
            default_reasoning_effort=" medium ",
        )

    def test_default_factories_cover_the_canonical_schemas(self) -> None:
        factory = self._factory()

        runtime = factory.build_default_runtime_state()
        stored = factory.build_default_stored_binding()

        self.assertEqual(frozenset(runtime), RuntimeStateDict.__required_keys__)
        self.assertIs(type(runtime["execution_pages"]), ExecutionPageLedger)
        self.assertEqual(runtime["execution_pages"].pages, ())
        self.assertEqual(
            {key: value for key, value in runtime.items() if key != "execution_pages"},
            {
                "active": False,
                "working_dir": "/tmp/default",
                "current_thread_id": "",
                "current_thread_title": "",
                "feishu_runtime_state": "",
                "goal_objective": "",
                "goal_status": "",
                "goal_token_budget": None,
                "goal_tokens_used": 0,
                "goal_time_used_seconds": 0,
                "goal_created_at": 0,
                "goal_updated_at": 0,
                "current_turn_id": "",
                "running": False,
                "cancelled": False,
                "pending_cancel": False,
                "current_execution_kind": "",
                "current_prompt_message_id": "",
                "current_prompt_reply_in_thread": False,
                "current_actor_open_id": "",
                "execution_transcript": ExecutionTranscript(),
                "runtime_channel_state": "live",
                "started_at": 0.0,
                "last_runtime_event_at": 0.0,
                "last_patch_at": 0.0,
                "patch_timer_registration": None,
                "mirror_watchdog_registration": None,
                "followup_sent": False,
                "followup_text": "",
                "terminal_result_text": "",
                "awaiting_local_turn_started": False,
                "awaiting_attach_status_settle": False,
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
                "model": "gpt-default",
                "reasoning_effort": "medium",
                "configured_settings": [],
                "plan_message_id": "",
                "plan_turn_id": "",
                "plan_explanation": "",
                "plan_steps": [],
                "plan_text": "",
            },
        )
        self.assertEqual(
            stored,
            {
                "working_dir": "",
                "current_thread_id": "",
                "current_thread_title": "",
                "feishu_runtime_state": "",
                "approval_policy": "",
                "permissions_profile_id": "",
                "model": "",
                "reasoning_effort": "",
                "configured_settings": [],
            },
        )
        with self.assertRaises(FrozenInstanceError):
            factory.default_model = "replacement"  # type: ignore[misc]

    def test_runtime_stored_roundtrip_uses_canonical_normalizers(self) -> None:
        factory = self._factory()
        runtime = factory.build_default_runtime_state()
        runtime.update(
            working_dir=" /tmp/project ",
            current_thread_id=" thread-1 ",
            current_thread_title=" Demo ",
            feishu_runtime_state=" detached ",
            approval_policy=" on-failure ",
            permissions_profile_id=" workspace-write ",
            model=" gpt-explicit ",
            reasoning_effort=" high ",
            configured_settings=[
                "approval_policy",
                "permissions_profile_id",
                "model",
                "reasoning_effort",
            ],
        )

        stored = factory.stored_binding_from_runtime(runtime)

        self.assertEqual(
            stored,
            {
                "working_dir": "/tmp/project",
                "current_thread_id": "thread-1",
                "current_thread_title": "Demo",
                "feishu_runtime_state": "detached",
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
                "model": "gpt-explicit",
                "reasoning_effort": "high",
                "configured_settings": [
                    "approval_policy",
                    "permissions_profile_id",
                    "model",
                    "reasoning_effort",
                ],
            },
        )
        hydrated = factory.build_default_runtime_state()
        self.assertFalse(factory.hydrate_stored_binding(hydrated, stored))
        self.assertEqual(factory.stored_binding_from_runtime(hydrated), stored)

    def test_hydration_uses_defaults_and_downgrades_persisted_attached(self) -> None:
        factory = self._factory()
        state = factory.build_default_runtime_state()
        stored = factory.build_default_stored_binding()
        stored.update(
            current_thread_id="thread-1",
            current_thread_title="Demo",
            feishu_runtime_state="attached",
            approval_policy="",
            permissions_profile_id="",
            model=" explicit-model ",
            reasoning_effort=" high ",
            configured_settings=["model", "reasoning_effort"],
        )

        downgraded = factory.hydrate_stored_binding(state, stored)

        self.assertTrue(downgraded)
        self.assertEqual(state["working_dir"], "/tmp/default")
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertEqual(state["current_thread_title"], "Demo")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(state["approval_policy"], "on-request")
        self.assertEqual(state["permissions_profile_id"], ":workspace")
        self.assertEqual(state["model"], "explicit-model")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertEqual(state["configured_settings"], ["model", "reasoning_effort"])

    def test_empty_stored_predicate_preserves_explicit_override_semantics(self) -> None:
        factory = self._factory()
        stored = factory.build_default_stored_binding()
        self.assertTrue(factory.is_empty_stored_binding(stored))

        stored.update(
            approval_policy="never",
            permissions_profile_id=":workspace",
            model="gpt-default",
            reasoning_effort="medium",
        )
        self.assertTrue(factory.is_empty_stored_binding(stored))

        for field, value in (
            ("working_dir", "/tmp/project"),
            ("current_thread_id", "thread-1"),
            ("current_thread_title", "Demo"),
            ("feishu_runtime_state", "detached"),
            ("configured_settings", ["model"]),
        ):
            with self.subTest(field=field):
                candidate = factory.build_default_stored_binding()
                candidate[field] = value  # type: ignore[literal-required,typeddict-item]
                self.assertFalse(factory.is_empty_stored_binding(candidate))

    def test_factories_and_projections_do_not_share_mutable_containers(self) -> None:
        factory = self._factory()
        first = factory.build_default_runtime_state()
        second = factory.build_default_runtime_state()
        first_stored = factory.build_default_stored_binding()
        second_stored = factory.build_default_stored_binding()

        self.assertIsNot(first["execution_transcript"], second["execution_transcript"])
        self.assertIsNot(first["configured_settings"], second["configured_settings"])
        self.assertIsNot(first["plan_steps"], second["plan_steps"])
        self.assertIsNot(
            first_stored["configured_settings"],
            second_stored["configured_settings"],
        )

        first["configured_settings"].append("model")
        projected = factory.stored_binding_from_runtime(first)
        first["configured_settings"].append("reasoning_effort")
        self.assertEqual(projected["configured_settings"], ["model"])

        hydrated = factory.build_default_runtime_state()
        factory.hydrate_stored_binding(hydrated, projected)
        projected["configured_settings"].append("approval_policy")
        self.assertEqual(hydrated["configured_settings"], ["model"])


if __name__ == "__main__":
    unittest.main()
