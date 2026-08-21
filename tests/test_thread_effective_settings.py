import unittest

from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry


def _record_response(
    registry: ThreadEffectiveSettingsRegistry,
    *,
    model: str = "base-a",
    reasoning_effort: str | None = "high",
    approval_policy: str | None = "never",
    permissions_profile_id: str | None = ":workspace",
    source: str = "thread_resume",
) -> None:
    registry.record_start_or_resume(
        "thread-1",
        model=model,
        reasoning_effort=reasoning_effort,
        approval_policy=approval_policy,
        permissions_profile_id=permissions_profile_id,
        source=source,  # type: ignore[arg-type]
    )


def _start(registry: ThreadEffectiveSettingsRegistry, turn_id: str = "turn-1") -> None:
    registry.observe_notification(
        "turn/started",
        {"threadId": "thread-1", "turn": {"id": turn_id}},
    )


class ThreadEffectiveSettingsRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ThreadEffectiveSettingsRegistry()

    def test_turn_start_freezes_complete_base_and_preserves_known_null(self) -> None:
        _record_response(
            self.registry,
            reasoning_effort=None,
            approval_policy=None,
            permissions_profile_id=None,
        )
        _start(self.registry)

        disclosure = self.registry.disclosure_for_active_turn("thread-1", "turn-1")

        self.assertEqual((disclosure.model.value, disclosure.model.source), ("base-a", "inherited"))
        self.assertEqual(
            (disclosure.reasoning_effort.value, disclosure.reasoning_effort.source),
            ("", "inherited"),
        )
        self.assertEqual(disclosure.approval_policy.source, "unknown")
        self.assertEqual(disclosure.permissions_profile_id.source, "unknown")

    def test_disclosure_requires_exact_observed_active_turn(self) -> None:
        _record_response(self.registry)

        before_start = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        stale = self.registry.disclosure_for_active_turn("thread-1", "turn-other")

        self.assertEqual(before_start.model.source, "unknown")
        self.assertEqual(stale.model.source, "unknown")

    def test_complete_settings_event_replaces_base_not_active_snapshot(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.observe_notification(
            "thread/settings/updated",
            {
                "threadId": "thread-1",
                "threadSettings": {
                    "model": "base-b",
                    "effort": "ultra",
                    "approvalPolicy": "on-request",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(active.model.value, "base-a")
        self.assertEqual(active.reasoning_effort.value, "high")
        self.registry.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        _start(self.registry, "turn-2")
        next_turn = self.registry.disclosure_for_active_turn("thread-1", "turn-2")
        self.assertEqual(next_turn.model.value, "base-b")
        self.assertEqual(next_turn.reasoning_effort.value, "ultra")
        self.assertEqual(next_turn.approval_policy.value, "on-request")
        self.assertEqual(next_turn.permissions_profile_id.value, ":danger-full-access")

    def test_response_replaces_base_without_clearing_active_or_reroute(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute"},
        )

        _record_response(self.registry, model="base-from-response", source="thread_start")

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual((active.model.value, active.model.source), ("reroute", "active_reroute"))
        self.registry.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertEqual(
            self.registry.resolve_model_for_request("thread-1"),
            "base-from-response",
        )

    def test_unknown_frozen_turn_is_not_backfilled_and_reroute_restores_only_model(
        self,
    ) -> None:
        _start(self.registry)
        _record_response(self.registry, model="response-base")
        self.registry.observe_notification(
            "thread/settings/updated",
            {
                "threadId": "thread-1",
                "threadSettings": {
                    "model": "event-base",
                    "effort": "ultra",
                    "approvalPolicy": "on-request",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )

        frozen = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(frozen.model.source, "unknown")
        self.assertEqual(frozen.reasoning_effort.source, "unknown")
        self.assertEqual(frozen.approval_policy.source, "unknown")
        self.assertEqual(frozen.permissions_profile_id.source, "unknown")

        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute"},
        )
        rerouted = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(
            (rerouted.model.value, rerouted.model.source),
            ("reroute", "active_reroute"),
        )
        self.assertEqual(rerouted.reasoning_effort.source, "unknown")

        self.registry.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        _start(self.registry, "turn-2")
        next_turn = self.registry.disclosure_for_active_turn("thread-1", "turn-2")
        self.assertEqual(next_turn.model.value, "event-base")
        self.assertEqual(next_turn.reasoning_effort.value, "ultra")
        self.assertEqual(next_turn.approval_policy.value, "on-request")
        self.assertEqual(next_turn.permissions_profile_id.value, ":danger-full-access")

    def test_stale_reroute_and_completion_do_not_disrupt_current_turn(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute"},
        )

        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-old", "toModel": "stale"},
        )
        self.registry.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-old"}},
        )

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual((active.model.value, active.model.source), ("reroute", "active_reroute"))

    def test_matching_malformed_reroute_invalidates_only_active_model(self) -> None:
        for malformed_model in (None, "", " untrimmed ", 7):
            with self.subTest(malformed_model=malformed_model):
                self.registry.clear_all()
                _record_response(self.registry)
                _start(self.registry)
                self.registry.observe_notification(
                    "model/rerouted",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "toModel": "reroute",
                    },
                )

                self.registry.observe_notification(
                    "model/rerouted",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "toModel": malformed_model,
                    },
                )

                active = self.registry.disclosure_for_active_turn(
                    "thread-1",
                    "turn-1",
                )
                self.assertEqual(active.model.source, "unknown")
                self.assertEqual(active.reasoning_effort.value, "high")
                self.assertEqual(active.approval_policy.value, "never")
                self.assertEqual(active.permissions_profile_id.value, ":workspace")

    def test_reroute_without_usable_turn_identity_retires_active_model(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute"},
        )

        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": " untrimmed ", "toModel": "new"},
        )

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(active.model.source, "unknown")
        self.assertEqual(active.reasoning_effort.value, "high")
        self.assertEqual(active.approval_policy.value, "never")
        self.assertEqual(active.permissions_profile_id.value, ":workspace")

    def test_duplicate_turn_started_does_not_refreeze_or_clear_reroute(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute"},
        )
        _record_response(self.registry, model="base-b")

        _start(self.registry)

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual((active.model.value, active.model.source), ("reroute", "active_reroute"))

    def test_partial_or_malformed_settings_event_invalidates_stale_base(self) -> None:
        for index, settings in enumerate(
            (
                {"model": "partial"},
                {
                    "model": "bad",
                    "effort": None,
                    "approvalPolicy": "never",
                    "activePermissionProfile": {"id": ""},
                },
            )
        ):
            self.registry.clear_all()
            _record_response(self.registry)
            active_turn_id = f"turn-active-{index}"
            next_turn_id = f"turn-next-{index}"
            _start(self.registry, active_turn_id)
            self.registry.observe_notification(
                "thread/settings/updated",
                {"threadId": "thread-1", "threadSettings": settings},
            )
            self.assertEqual(
                self.registry.resolve_model_for_request("thread-1"),
                "base-a",
            )
            active = self.registry.disclosure_for_active_turn(
                "thread-1",
                active_turn_id,
            )
            self.assertEqual(active.model.value, "base-a")
            self.assertEqual(active.reasoning_effort.value, "high")
            self.assertEqual(active.approval_policy.value, "never")
            self.assertEqual(active.permissions_profile_id.value, ":workspace")

            self.registry.observe_notification(
                "turn/completed",
                {"threadId": "thread-1", "turn": {"id": active_turn_id}},
            )
            self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))
            _start(self.registry, next_turn_id)
            unknown = self.registry.disclosure_for_active_turn(
                "thread-1",
                next_turn_id,
            )
            self.assertEqual(unknown.model.source, "unknown")
            self.assertEqual(unknown.reasoning_effort.source, "unknown")
            self.assertEqual(unknown.approval_policy.source, "unknown")
            self.assertEqual(unknown.permissions_profile_id.source, "unknown")

    def test_permission_profile_null_is_unknown_but_effort_null_is_known(self) -> None:
        self.registry.observe_notification(
            "thread/settings/updated",
            {
                "threadId": "thread-1",
                "threadSettings": {
                    "model": "base",
                    "effort": None,
                    "approvalPolicy": "never",
                    "activePermissionProfile": None,
                },
            },
        )
        _start(self.registry)

        disclosure = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(disclosure.reasoning_effort.source, "inherited")
        self.assertEqual(disclosure.reasoning_effort.value, "")
        self.assertEqual(disclosure.permissions_profile_id.source, "unknown")

    def test_model_invalidation_is_scoped_and_query_is_pure(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.assertIsNone(
            self.registry.resolve_model_for_request(
                "thread-1",
                requested_model="different",
            )
        )
        self.assertEqual(self.registry.resolve_model_for_request("thread-1"), "base-a")

        self.assertTrue(
            self.registry.invalidate_requested_settings_if_different(
                "thread-1",
                model="different",
            )
        )
        self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))
        disclosure = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(disclosure.model.source, "unknown")
        self.assertEqual(disclosure.reasoning_effort.value, "high")

    def test_settings_ack_invalidation_does_not_change_active_snapshot(self) -> None:
        _record_response(self.registry)
        _start(self.registry)

        self.assertTrue(
            self.registry.invalidate_thread_base_if_requested_settings_differ(
                "thread-1",
                model="base-b",
            )
        )
        self.assertEqual(self.registry.resolve_model_for_request("thread-1"), "base-a")
        self.registry.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))

    def test_turn_intent_invalidates_only_differing_explicit_fields(self) -> None:
        _record_response(self.registry)
        _start(self.registry)

        self.assertTrue(
            self.registry.invalidate_requested_settings_if_different(
                "thread-1",
                model="base-a",
                reasoning_effort="ultra",
                approval_policy="on-request",
                permissions_profile_id=":workspace",
            )
        )

        disclosure = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(disclosure.model.source, "inherited")
        self.assertEqual(disclosure.reasoning_effort.source, "unknown")
        self.assertEqual(disclosure.approval_policy.source, "unknown")
        self.assertEqual(disclosure.permissions_profile_id.source, "inherited")

    def test_turn_intent_does_not_reveal_frozen_model_beneath_different_reroute(
        self,
    ) -> None:
        _record_response(self.registry, model="base-a")
        _start(self.registry)
        self.registry.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute-b"},
        )

        self.assertTrue(
            self.registry.invalidate_requested_settings_if_different(
                "thread-1",
                model="base-a",
            )
        )

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(active.model.source, "unknown")
        self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))

    def test_settings_intent_invalidates_base_without_touching_active_snapshot(self) -> None:
        _record_response(self.registry)
        _start(self.registry)

        self.assertTrue(
            self.registry.invalidate_thread_base_if_requested_settings_differ(
                "thread-1",
                model="base-b",
                reasoning_effort="ultra",
                approval_policy="on-request",
                permissions_profile_id=":danger-full-access",
            )
        )

        active = self.registry.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(active.model.value, "base-a")
        self.assertEqual(active.reasoning_effort.value, "high")
        self.registry.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))

    def test_nonactive_clears_active_only_and_unload_clears_thread(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.observe_notification(
            "thread/status/changed",
            {"threadId": "thread-1", "status": {"type": "idle"}},
        )
        self.assertEqual(self.registry.resolve_model_for_request("thread-1"), "base-a")
        self.assertEqual(
            self.registry.disclosure_for_active_turn("thread-1", "turn-1").model.source,
            "unknown",
        )

        self.registry.observe_notification(
            "thread/status/changed",
            {"threadId": "thread-1", "status": {"type": "notLoaded"}},
        )
        self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))

    def test_malformed_lifecycle_retires_old_active_snapshot(self) -> None:
        for event, payload in (
            ("turn/started", {"threadId": "thread-1", "turn": {}}),
            ("turn/completed", {"threadId": "thread-1", "turn": {}}),
            ("thread/status/changed", {"threadId": "thread-1", "status": {}}),
        ):
            with self.subTest(event=event):
                self.registry.clear_all()
                _record_response(self.registry)
                _start(self.registry)

                self.registry.observe_notification(event, payload)

                disclosure = self.registry.disclosure_for_active_turn(
                    "thread-1",
                    "turn-1",
                )
                self.assertEqual(disclosure.model.source, "unknown")
                self.assertEqual(disclosure.reasoning_effort.source, "unknown")
                self.assertEqual(
                    self.registry.resolve_model_for_request("thread-1"),
                    "base-a",
                )

    def test_terminal_thread_lifecycle_clears_all_facts(self) -> None:
        for event in ("thread/archived", "thread/closed", "thread/deleted"):
            with self.subTest(event=event):
                _record_response(self.registry)
                _start(self.registry)
                self.registry.observe_notification(event, {"threadId": "thread-1"})
                self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))
                self.assertEqual(
                    self.registry.disclosure_for_active_turn(
                        "thread-1",
                        "turn-1",
                    ).model.source,
                    "unknown",
                )

    def test_external_unknown_rejects_all_value_ingress_until_epoch_reset(self) -> None:
        _record_response(self.registry)
        _start(self.registry)
        self.registry.record_start_or_resume(
            "thread-2",
            model="isolated-model",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )

        self.assertTrue(self.registry.mark_external_unknown("thread-1"))
        self.registry.clear_thread("thread-1")
        self.registry.observe_notification(
            "thread/archived",
            {"threadId": "thread-1"},
        )
        self.registry.observe_notification(
            "thread/settings/updated",
            {
                "threadId": "thread-1",
                "threadSettings": {
                    "model": "event-model",
                    "effort": "ultra",
                    "approvalPolicy": "on-request",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )
        _record_response(self.registry, model="response-model")
        _start(self.registry, "turn-after-response")

        self.assertIsNone(self.registry.resolve_model_for_request("thread-1"))
        self.assertEqual(
            self.registry.resolve_model_for_request("thread-2"),
            "isolated-model",
        )
        disclosure = self.registry.disclosure_for_active_turn(
            "thread-1",
            "turn-after-response",
        )
        self.assertEqual(disclosure.model.source, "unknown")
        self.assertEqual(disclosure.reasoning_effort.source, "unknown")

        self.registry.clear_all()
        _record_response(self.registry, model="after-reset")
        self.assertEqual(
            self.registry.resolve_model_for_request("thread-1"),
            "after-reset",
        )


if __name__ == "__main__":
    unittest.main()
