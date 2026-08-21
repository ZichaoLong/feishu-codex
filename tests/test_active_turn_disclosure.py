from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.active_turn_disclosure import ActiveTurnDisclosureComposer
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.stores.interaction_lease_store import (
    InteractionLeaseHolder,
    InteractionLeaseStore,
    InteractionLeaseStoreUnavailable,
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)


class ActiveTurnDisclosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.leases = InteractionLeaseStore(pathlib.Path(self.temp_dir.name))
        self.settings = ThreadEffectiveSettingsRegistry()
        self.subscribers = (
            ("user-b", "chat-b"),
            (GROUP_SHARED_BINDING_OWNER_ID, "group-a"),
        )
        self.composer = ActiveTurnDisclosureComposer(
            interaction_leases=self.leases,
            effective_settings=self.settings,
            thread_subscribers=lambda _thread_id: self.subscribers,
        )

    def _activate(self, holder: InteractionLeaseHolder, turn_id: str) -> None:
        acquired = self.leases.acquire("thread-1", holder)
        self.assertTrue(acquired.granted)
        self.assertIsNotNone(acquired.lease)
        assert acquired.lease is not None
        self.assertIsNotNone(self.leases.activate_turn(acquired.lease, turn_id))

    def test_exact_feishu_initiator_audience_and_inherited_model_are_separate(
        self,
    ) -> None:
        self.settings.record_start_or_resume(
            "thread-1",
            model="gpt-inherited",
            reasoning_effort="ultra",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        self.settings.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self._activate(
            make_feishu_interaction_holder("user-a", "chat-a", owner_pid=0),
            "turn-1",
        )

        context = self.composer.compose("thread-1", "turn-1")

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context["turn_id"], "turn-1")
        self.assertEqual(
            context["initiator"],
            {"kind": "feishu", "binding_id": "p2p:user-a:chat-a"},
        )
        self.assertEqual(
            context["feishu_audience"],
            ["group:group-a", "p2p:user-b:chat-b"],
        )
        self.assertEqual(
            context["settings"]["model"],
            {"value": "gpt-inherited", "source": "inherited"},
        )
        self.assertEqual(
            context["settings"]["reasoning_effort"],
            {"value": "ultra", "source": "inherited"},
        )
        self.assertEqual(
            context["settings"]["approval_policy"],
            {"value": "never", "source": "inherited"},
        )
        self.assertEqual(
            context["settings"]["permissions_profile_id"],
            {"value": ":workspace", "source": "inherited"},
        )

    def test_initiator_requires_exact_turn_and_local_kinds_do_not_expose_ids(
        self,
    ) -> None:
        self._activate(
            make_web_interaction_holder("tab-secret", owner_pid=0),
            "turn-old",
        )

        stale = self.composer.compose("thread-1", "turn-new")
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertEqual(
            stale["initiator"],
            {"kind": "autonomous_or_unknown", "binding_id": ""},
        )

        exact = self.composer.compose("thread-1", "turn-old")
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(
            exact["initiator"],
            {"kind": "web", "binding_id": ""},
        )

        self.leases.clear_thread("thread-1")
        self._activate(
            make_fcodex_interaction_holder(
                "participant-secret",
                connection_id="connection-secret",
                owner_pid=0,
            ),
            "turn-fcodex",
        )
        fcodex = self.composer.compose("thread-1", "turn-fcodex")
        self.assertIsNotNone(fcodex)
        assert fcodex is not None
        self.assertEqual(
            fcodex["initiator"],
            {"kind": "fcodex", "binding_id": ""},
        )

    def test_matching_reroute_is_the_only_active_model_source(self) -> None:
        self.settings.record_start_or_resume(
            "thread-1",
            model="base-model",
            reasoning_effort=None,
            approval_policy=None,
            permissions_profile_id=None,
            source="thread_start",
        )
        self.settings.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.settings.observe_notification(
            "model/rerouted",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "toModel": "active-model",
            },
        )

        context = self.composer.compose("thread-1", "turn-1")

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context["settings"]["model"],
            {"value": "active-model", "source": "active_reroute"},
        )

    def test_no_lease_is_unknown_without_hiding_an_active_turn(self) -> None:
        context = self.composer.compose("thread-1", "turn-1")

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context["initiator"],
            {"kind": "autonomous_or_unknown", "binding_id": ""},
        )
        self.assertEqual(
            context["settings"]["model"],
            {"value": "", "source": "unknown"},
        )

    def test_unavailable_lease_store_degrades_only_the_initiator(self) -> None:
        self.settings.record_start_or_resume(
            "thread-1",
            model="known-model",
            reasoning_effort=None,
            approval_policy=None,
            permissions_profile_id=None,
            source="thread_resume",
        )
        self.settings.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        unavailable = InteractionLeaseStoreUnavailable(
            pathlib.Path(self.temp_dir.name) / "interaction_leases.json",
            "test unavailable",
        )

        with patch.object(self.leases, "load", side_effect=unavailable):
            context = self.composer.compose("thread-1", "turn-1")

        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(
            context["initiator"],
            {"kind": "autonomous_or_unknown", "binding_id": ""},
        )
        self.assertEqual(
            context["settings"]["model"],
            {"value": "known-model", "source": "inherited"},
        )
        self.assertEqual(
            context["feishu_audience"],
            ["group:group-a", "p2p:user-b:chat-b"],
        )

    def test_missing_active_turn_has_no_context(self) -> None:
        self.assertIsNone(self.composer.compose("thread-1", ""))


if __name__ == "__main__":
    unittest.main()
