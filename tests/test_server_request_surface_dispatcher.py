from __future__ import annotations

import unittest

from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.server_request_contract import ServerRequestIdentity
from bot.server_request_dispatch import (
    ServerRequestDispatchKnownNotCommitted,
    ServerRequestSurfaceClaim,
    ServerRequestSurfaceIdentityConflict,
)
from bot.server_request_surface_dispatcher import (
    ServerRequestSurfaceDispatcher,
    ServerRequestSurfaceDispatcherPorts,
    ServerRequestSurfaceOffer,
)


class _Harness:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.offers: list[tuple[str, ServerRequestSurfaceOffer]] = []
        self.fcodex_handled = False
        self.web_handled = False
        self.web_pending = False
        self.feishu_pending = False
        self.dispatch_error: Exception | None = None
        self.web_error: Exception | None = None
        self.timing: AutoResolutionTiming | None = None
        self.shared_approval = True
        self.shared_interaction = False
        self.dispatcher = ServerRequestSurfaceDispatcher(
            ServerRequestSurfaceDispatcherPorts(
                share_approval=lambda _identity: self.shared_approval,
                share_desktop_interaction=(
                    lambda _identity: self.shared_interaction
                ),
                route_fcodex=self.route_fcodex,
                schedule_auto_resolution=self.schedule,
                route_web=self.route_web,
                route_feishu=self.route_feishu,
                web_has_pending=lambda _key: self.web_pending,
                feishu_has_pending=lambda _key: self.feishu_pending,
                cancel_auto_resolution=(
                    lambda key, _timing: self.events.append(f"cancel:{key}") or True
                ),
            )
        )

    @staticmethod
    def identity(
        request_id: str = "request-1",
        *,
        method: str = "item/tool/requestUserInput",
    ) -> ServerRequestIdentity:
        return ServerRequestIdentity(
            request_id=request_id,
            connection_generation=1,
            method=method,
            params={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [],
                "autoResolutionMs": 1000,
            },
        )

    def route_fcodex(
        self,
        offer: ServerRequestSurfaceOffer,
    ) -> ServerRequestSurfaceClaim:
        self.events.append("fcodex")
        self.offers.append(("fcodex", offer))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return (
            ServerRequestSurfaceClaim.claimed()
            if self.fcodex_handled
            else ServerRequestSurfaceClaim.declined()
        )

    def schedule(self, request_key: str, enabled: bool) -> AutoResolutionTiming | None:
        self.events.append(f"schedule:{request_key}:{enabled}")
        return self.timing

    def route_web(
        self,
        offer: ServerRequestSurfaceOffer,
    ) -> ServerRequestSurfaceClaim:
        self.events.append("web")
        self.offers.append(("web", offer))
        if self.web_error is not None:
            raise self.web_error
        return (
            ServerRequestSurfaceClaim.claimed()
            if self.web_handled
            else ServerRequestSurfaceClaim.declined()
        )

    def route_feishu(
        self,
        offer: ServerRequestSurfaceOffer,
    ) -> ServerRequestSurfaceClaim:
        self.events.append("feishu")
        self.offers.append(("feishu", offer))
        return (
            ServerRequestSurfaceClaim.claimed()
            if self.feishu_pending
            else ServerRequestSurfaceClaim.declined()
        )


class ServerRequestSurfaceDispatcherTest(unittest.TestCase):
    def test_surface_claim_requires_literal_retained_receipt(self) -> None:
        self.assertEqual(ServerRequestSurfaceClaim.from_retained(True).outcome, "claimed")
        for value in (False, None, 1, "yes"):
            with self.subTest(value=value):
                self.assertEqual(
                    ServerRequestSurfaceClaim.from_retained(value).outcome,
                    "declined",
                )

    def test_fcodex_claim_short_circuits_every_other_surface(self) -> None:
        harness = _Harness()
        harness.fcodex_handled = True

        receipt = harness.dispatcher.dispatch(harness.identity())

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(harness.events, ["fcodex"])

    def test_shared_interaction_fcodex_claim_still_offers_web(self) -> None:
        harness = _Harness()
        harness.shared_interaction = True
        harness.fcodex_handled = True
        harness.timing = AutoResolutionTiming(1, 1, 10, 20)
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(
            harness.events,
            [
                f"schedule:{identity.request_key}:True",
                "fcodex",
                "web",
                f"cancel:{identity.request_key}",
            ],
        )
        self.assertEqual(
            [offer.mode for _surface, offer in harness.offers],
            ["shared_interaction", "shared_interaction"],
        )
        self.assertNotIn("feishu", harness.events)

    def test_shared_interaction_web_claim_preserves_exact_timer(self) -> None:
        harness = _Harness()
        harness.shared_interaction = True
        harness.web_handled = True
        harness.web_pending = True
        harness.timing = AutoResolutionTiming(1, 2, 10, 20)
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(
            harness.events,
            [f"schedule:{identity.request_key}:True", "fcodex", "web"],
        )

    def test_shared_interaction_unknown_preserves_only_a_retained_timer(self) -> None:
        cases = (
            ("fcodex", True, True, False),
            ("web-before-retain", False, False, True),
            ("web-after-retain", False, True, False),
        )
        for surface, fcodex_unknown, web_pending, timer_cancelled in cases:
            with self.subTest(surface=surface):
                harness = _Harness()
                harness.shared_interaction = True
                harness.timing = AutoResolutionTiming(1, 3, 10, 20)
                if fcodex_unknown:
                    harness.dispatch_error = RuntimeError("possibly retained")
                    harness.web_handled = True
                    harness.web_pending = web_pending
                else:
                    harness.web_error = RuntimeError("possibly retained")
                    harness.fcodex_handled = True
                    harness.web_pending = web_pending
                identity = harness.identity()

                receipt = harness.dispatcher.dispatch(identity)

                self.assertEqual(receipt.outcome, "outcome_unknown")
                self.assertIn("fcodex", harness.events)
                self.assertIn("web", harness.events)
                self.assertNotIn("feishu", harness.events)
                self.assertEqual(
                    f"cancel:{identity.request_key}" in harness.events,
                    timer_cancelled,
                )

    def test_shared_interaction_falls_back_only_after_two_declines(self) -> None:
        harness = _Harness()
        harness.shared_interaction = True
        harness.feishu_pending = True
        harness.timing = AutoResolutionTiming(1, 4, 10, 20)
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(
            harness.events,
            [
                f"schedule:{identity.request_key}:True",
                "fcodex",
                "web",
                "feishu",
            ],
        )
        self.assertEqual(harness.offers[-1][1].mode, "single_surface")
        self.assertIs(harness.offers[-1][1].auto_resolution_timing, harness.timing)

    def test_shared_approval_routes_to_every_surface_without_timer(self) -> None:
        harness = _Harness()
        harness.fcodex_handled = True
        harness.web_handled = True
        harness.web_pending = True
        harness.feishu_pending = True

        receipt = harness.dispatcher.dispatch(
            harness.identity(method="item/commandExecution/requestApproval")
        )

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(harness.events, ["fcodex", "web", "feishu"])
        self.assertEqual(
            [offer.mode for _surface, offer in harness.offers],
            ["shared_approval", "shared_approval", "shared_approval"],
        )
        self.assertTrue(
            all(
                offer.auto_resolution_timing is None
                for _surface, offer in harness.offers
            )
        )

    def test_one_shared_approval_claim_commits_after_all_surfaces_run(self) -> None:
        harness = _Harness()
        harness.web_handled = True

        receipt = harness.dispatcher.dispatch(
            harness.identity(method="item/fileChange/requestApproval")
        )

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(harness.events, ["fcodex", "web", "feishu"])

    def test_unclaimed_shared_approval_is_known_not_committed(self) -> None:
        harness = _Harness()

        receipt = harness.dispatcher.dispatch(
            harness.identity(method="item/permissions/requestApproval")
        )

        self.assertEqual(receipt.outcome, "known_not_committed")
        self.assertEqual(harness.events, ["fcodex", "web", "feishu"])

    def test_unqualified_approval_keeps_single_surface_routing(self) -> None:
        harness = _Harness()
        harness.shared_approval = False
        harness.fcodex_handled = True

        receipt = harness.dispatcher.dispatch(
            harness.identity(method="item/commandExecution/requestApproval")
        )

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(harness.events, ["fcodex"])
        self.assertEqual(harness.offers[0][1].mode, "single_surface")

    def test_shared_surface_failure_does_not_skip_remaining_surfaces(self) -> None:
        harness = _Harness()
        harness.dispatch_error = RuntimeError("fcodex projection failed")
        harness.web_handled = True

        receipt = harness.dispatcher.dispatch(
            harness.identity(method="item/commandExecution/requestApproval")
        )

        self.assertEqual(receipt.outcome, "outcome_unknown")
        self.assertEqual(harness.events, ["fcodex", "web", "feishu"])

    def test_web_delivery_owns_request_and_preserves_live_timer(self) -> None:
        harness = _Harness()
        harness.web_handled = True
        harness.web_pending = True
        harness.timing = AutoResolutionTiming(1, 1, 10, 20)
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(
            harness.events,
            [
                "fcodex",
                f"schedule:{identity.request_key}:True",
                "web",
            ],
        )

    def test_declined_web_delivery_falls_through_to_feishu(self) -> None:
        harness = _Harness()
        harness.feishu_pending = True
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "committed")
        self.assertEqual(
            harness.events,
            [
                "fcodex",
                f"schedule:{identity.request_key}:True",
                "web",
                "feishu",
            ],
        )

    def test_no_surface_claim_is_known_not_committed_and_cancels_timer(self) -> None:
        harness = _Harness()
        harness.timing = AutoResolutionTiming(1, 1, 10, 20)
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "known_not_committed")
        self.assertEqual(harness.events[-2:], ["feishu", f"cancel:{identity.request_key}"])

    def test_explicit_no_effect_exception_is_retry_authority(self) -> None:
        harness = _Harness()
        harness.dispatch_error = ServerRequestDispatchKnownNotCommitted(
            "before any effect"
        )

        receipt = harness.dispatcher.dispatch(harness.identity())

        self.assertEqual(receipt.outcome, "known_not_committed")
        self.assertEqual(receipt.reason, "before any effect")

    def test_unclassified_exception_is_non_replayable_unknown(self) -> None:
        harness = _Harness()
        harness.dispatch_error = RuntimeError("possibly after effect")

        receipt = harness.dispatcher.dispatch(harness.identity())

        self.assertEqual(receipt.outcome, "outcome_unknown")
        self.assertEqual(receipt.reason, "possibly after effect")

    def test_unknown_web_effect_cancels_only_its_exact_timer_capability(self) -> None:
        harness = _Harness()
        harness.timing = AutoResolutionTiming(1, 7, 10, 20)
        harness.web_error = RuntimeError("possibly after Web effect")
        identity = harness.identity()

        receipt = harness.dispatcher.dispatch(identity)

        self.assertEqual(receipt.outcome, "outcome_unknown")
        self.assertEqual(
            harness.events[-2:],
            ["web", f"cancel:{identity.request_key}"],
        )

    def test_surface_identity_conflict_is_non_replayable_unknown(self) -> None:
        harness = _Harness()
        harness.dispatch_error = ServerRequestSurfaceIdentityConflict(
            "fcodex retained another capability"
        )

        receipt = harness.dispatcher.dispatch(harness.identity())

        self.assertEqual(receipt.outcome, "outcome_unknown")
        self.assertEqual(
            receipt.reason,
            "fcodex retained another capability",
        )
        self.assertEqual(harness.events, ["fcodex"])


if __name__ == "__main__":
    unittest.main()
