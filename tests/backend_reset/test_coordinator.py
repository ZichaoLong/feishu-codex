from __future__ import annotations

import unittest

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.backend_reset.contract import (
    BackendResetGenerationStaleError,
    BackendResetLocalProjectionReceipt,
    BackendResetUnavailableError,
)
from bot.backend_reset.coordinator import BackendResetCoordinator


class _Adapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.generation = 7
        self.endpoint = "ws://127.0.0.1:8765"
        self.failure = ""
        self.owns_backend_lifecycle = True

    def require_owned_backend_lifecycle(self) -> None:
        if not self.owns_backend_lifecycle:
            raise RuntimeError("attached endpoint owns only its websocket")

    def stop(self) -> None:
        self.events.append("adapter.stop")
        self._fail("stop")

    def rotate_server_request_authority_after_backend_stop(self) -> object:
        self.events.append("adapter.rotate")
        self._fail("transport retirement")
        return "transport-receipt"

    def start(self) -> None:
        self.events.append("adapter.start")
        self._fail("start")

    def connection_generation(
        self,
        *,
        timeout: float,
        require_existing_connection: bool,
    ) -> int:
        self.events.append(
            f"adapter.generation:{timeout}:{require_existing_connection}"
        )
        self._fail("generation")
        return self.generation

    def fence_backend_reset_generation(
        self,
        *,
        expected_connection_generation: int,
        fence_ingress,
        timeout: float,
    ) -> None:
        self.events.append(
            f"adapter.fence:{expected_connection_generation}:{timeout}"
        )
        self._fail("fence")
        if self.generation != expected_connection_generation:
            raise BackendResetGenerationStaleError(
                expected_generation=expected_connection_generation,
                observed_generation=self.generation,
                source="physical",
            )
        fence_ingress()

    def current_app_server_url(self) -> str:
        self.events.append("adapter.endpoint")
        return self.endpoint

    def _fail(self, stage: str) -> None:
        if self.failure == stage:
            raise RuntimeError(f"{stage} failed")


class _OperationOwner:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.failure = ""

    def settle_backend_epoch_after_stop(self) -> object:
        self.events.append("owner.settle")
        if self.failure == "settle":
            raise RuntimeError("fcodex retirement failed")
        return "fcodex-receipt"

    def close_backend_epoch_after_machine_replace(self) -> object:
        self.events.append("owner.close")
        if self.failure == "close":
            raise RuntimeError("owner close failed")
        return "owner-close-receipt"


class _InteractionLeaseStore:
    def __init__(
        self,
        events: list[str],
        ingress_gate: AdapterIngressGate,
    ) -> None:
        self.events = events
        self.ingress_gate = ingress_gate
        self.failure = ""

    def capture_current_process_for_backend_stop(self) -> object:
        if not self.ingress_gate.snapshot().backend_reset_blocked:
            raise AssertionError("interaction lease capture requires fenced ingress")
        self.events.append("leases.capture")
        if self.failure == "capture":
            raise RuntimeError("interaction lease capture failed")
        return "lease-capture"

    def retire_after_backend_stop(self, capture: object) -> object:
        if capture != "lease-capture":
            raise AssertionError("unexpected interaction lease capture")
        self.events.append("leases.retire")
        if self.failure == "retire":
            raise RuntimeError("interaction lease retirement failed")
        return "lease-retirement-receipt"


class _RuntimeLeaseStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.failure = False

    def purge_all_for_instance(self, *, instance_name: str) -> list[str]:
        self.events.append(f"runtime.purge:{instance_name}")
        if self.failure:
            raise RuntimeError("runtime purge failed")
        return ["thread-z", "thread-a"]


class _RuntimeAuthority:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.failure = False

    def confirm_backend_reset(self) -> None:
        self.events.append("authority.confirm")
        if self.failure:
            raise RuntimeError("authority confirmation failed")


class _Harness:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.guard_error: BaseException | None = None
        self.publish_error: BaseException | None = None
        self.retirement_failure_at = ""
        self.projection_dispatch_fails = False
        self.local_projection_failure = False
        self.adapter = _Adapter(self.events)
        self.owner = _OperationOwner(self.events)
        self.runtime_leases = _RuntimeLeaseStore(self.events)
        self.runtime_authority = _RuntimeAuthority(self.events)
        self.gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: self.events.append("gate.invalidate"),
            activate_connection_epoch=lambda generation: self.events.append(
                f"gate.activate:{generation}"
            ),
        )
        self.interaction_leases = _InteractionLeaseStore(self.events, self.gate)
        self.coordinator = BackendResetCoordinator(
            ingress_gate=self.gate,
            adapter=self.adapter,
            operation_owner=self.owner,
            interaction_lease_store=self.interaction_leases,
            runtime_lease_store=self.runtime_leases,
            instance_name="default",
            runtime_authority=self.runtime_authority,
            retire_server_requests_after_stop=lambda: self._retire("registry"),
            retire_web_after_stop=lambda: self._retire("web"),
            retire_feishu_after_stop=lambda: self._retire("feishu"),
            retire_feishu_root_operations_after_stop=lambda: self._retire(
                "feishu-root"
            ),
            dispatch_feishu_card_projection_best_effort=(
                self._dispatch_feishu_projection
            ),
            connect_timeout_seconds=3.5,
            publish_replacement=self._publish,
            runtime_context_guard=self._guard,
        )

    def replace_owned_backend(self):
        return self.coordinator.replace_owned_backend(
            retire_local_projection_after_stop=self._retire_local_projection,
        )

    def _guard(self) -> None:
        self.events.append("guard")
        if self.guard_error is not None:
            raise self.guard_error

    def _publish(self, app_server_url: str) -> None:
        self.events.append(f"publish:{app_server_url}")
        if self.publish_error is not None:
            raise self.publish_error

    def _retire(self, surface: str) -> str:
        self.events.append(f"{surface}.retire")
        if self.retirement_failure_at == surface:
            raise RuntimeError(f"{surface} retirement failed")
        return f"{surface}-receipt"

    def _retire_local_projection(self) -> BackendResetLocalProjectionReceipt:
        self.events.append("local.retire")
        if self.local_projection_failure:
            raise RuntimeError("local projection retirement failed")
        return BackendResetLocalProjectionReceipt(
            detached_binding_ids=("p2p:user:chat",),
            interrupted_binding_ids=("p2p:user:chat",),
            projection_warnings=(),
        )

    def _dispatch_feishu_projection(self) -> None:
        self.events.append("feishu.project.dispatch")
        if self.projection_dispatch_fails:
            raise RuntimeError("projection dispatch failed")


class BackendResetCoordinatorTest(unittest.TestCase):
    def test_composition_rejects_an_attached_backend_client(self) -> None:
        events: list[str] = []
        adapter = _Adapter(events)
        adapter.owns_backend_lifecycle = False
        ingress_gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )

        with self.assertRaisesRegex(RuntimeError, "attached endpoint"):
            BackendResetCoordinator(
                ingress_gate=ingress_gate,
                adapter=adapter,
                operation_owner=_OperationOwner(events),
                interaction_lease_store=_InteractionLeaseStore(
                    events,
                    ingress_gate,
                ),
                runtime_lease_store=_RuntimeLeaseStore(events),
                instance_name="default",
                runtime_authority=_RuntimeAuthority(events),
                retire_server_requests_after_stop=lambda: None,
                retire_web_after_stop=lambda: None,
                retire_feishu_after_stop=lambda: None,
                retire_feishu_root_operations_after_stop=lambda: None,
                dispatch_feishu_card_projection_best_effort=lambda: None,
                connect_timeout_seconds=1.0,
                publish_replacement=lambda _endpoint: None,
                runtime_context_guard=lambda: None,
            )

        self.assertEqual(events, [])

    def test_success_clears_process_runtime_holders_before_replacement(self) -> None:
        harness = _Harness()

        receipt = harness.replace_owned_backend()

        self.assertEqual(receipt.connection_generation, 7)
        self.assertEqual(receipt.machine_cleared_thread_ids, ("thread-a", "thread-z"))
        self.assertEqual(
            receipt.retirement.interaction_leases,
            "lease-retirement-receipt",
        )
        self.assertEqual(
            receipt.retirement.server_request_registry,
            "registry-receipt",
        )
        self.assertEqual(receipt.retirement.fcodex, "fcodex-receipt")
        self.assertEqual(receipt.retirement.web, "web-receipt")
        self.assertEqual(
            receipt.retirement.local_projection.detached_binding_ids,
            ("p2p:user:chat",),
        )
        self.assertEqual(
            receipt.retirement.feishu_root_operations,
            "feishu-root-receipt",
        )
        self.assertEqual(receipt.retirement.feishu_requests, "feishu-receipt")
        self.assertEqual(receipt.retirement.transport, "transport-receipt")
        self.assertEqual(
            harness.events,
            [
                "guard",
                "leases.capture",
                "adapter.stop",
                "leases.retire",
                "registry.retire",
                "owner.settle",
                "web.retire",
                "local.retire",
                "feishu-root.retire",
                "feishu.retire",
                "feishu.project.dispatch",
                "adapter.rotate",
                "gate.invalidate",
                "runtime.purge:default",
                "owner.close",
                "authority.confirm",
                "adapter.start",
                "adapter.generation:3.5:True",
                "adapter.endpoint",
                "publish:ws://127.0.0.1:8765",
            ],
        )
        snapshot = harness.gate.snapshot()
        self.assertFalse(snapshot.backend_reset_blocked)
        self.assertFalse(snapshot.cleanup_required)
        self.assertEqual(snapshot.latest_generation, 7)

    def test_preview_generation_requires_matching_open_physical_and_gate_epochs(self) -> None:
        harness = _Harness()
        self.assertTrue(harness.gate.accept(7))
        harness.events.clear()

        self.assertEqual(harness.coordinator.preview_connection_generation(), 7)
        self.assertEqual(
            harness.events,
            ["guard", "adapter.generation:3.5:False"],
        )

        for name, configure in (
            ("physical zero", lambda h: setattr(h.adapter, "generation", 0)),
            ("gate zero", lambda h: setattr(h.gate, "_latest_generation", 0)),
            ("mismatch", lambda h: setattr(h.adapter, "generation", 8)),
            ("blocked", lambda h: h.gate.fence_backend_reset()),
            ("cleanup", lambda h: setattr(h.gate, "_cleanup_required", True)),
            (
                "disconnect pending",
                lambda h: setattr(h.gate, "_disconnect_cleanup_generation", 7),
            ),
        ):
            with self.subTest(name=name):
                candidate = _Harness()
                self.assertTrue(candidate.gate.accept(7))
                configure(candidate)
                before = candidate.gate.snapshot()
                with self.assertRaises(BackendResetUnavailableError):
                    candidate.coordinator.preview_connection_generation()
                self.assertEqual(candidate.gate.snapshot(), before)
                self.assertNotIn("adapter.stop", candidate.events)

    def test_expected_fence_uses_physical_then_gate_order(self) -> None:
        harness = _Harness()
        self.assertTrue(harness.gate.accept(7))
        harness.events.clear()

        harness.coordinator.fence_ingress(expected_connection_generation=7)

        self.assertEqual(
            harness.events,
            ["guard", "adapter.fence:7:3.5"],
        )
        self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

    def test_stale_expected_fence_has_no_gate_or_backend_effect(self) -> None:
        harness = _Harness()
        self.assertTrue(harness.gate.accept(7))
        harness.adapter.generation = 8
        harness.events.clear()
        before = harness.gate.snapshot()

        with self.assertRaises(BackendResetGenerationStaleError):
            harness.coordinator.fence_ingress(expected_connection_generation=7)

        self.assertEqual(harness.gate.snapshot(), before)
        self.assertEqual(
            harness.events,
            ["guard", "adapter.fence:7:3.5"],
        )
        self.assertNotIn("adapter.stop", harness.events)

    def test_legacy_fence_does_not_require_physical_generation(self) -> None:
        harness = _Harness()
        harness.adapter.generation = 0

        harness.coordinator.fence_ingress()

        self.assertEqual(harness.events, ["guard"])
        self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

    def test_runtime_guard_rejects_before_the_reset_fence(self) -> None:
        harness = _Harness()
        harness.guard_error = RuntimeError("wrong RuntimeLoop context")

        with self.assertRaisesRegex(RuntimeError, "wrong RuntimeLoop context"):
            harness.replace_owned_backend()

        self.assertEqual(harness.events, ["guard"])
        self.assertFalse(harness.gate.snapshot().backend_reset_blocked)

    def test_stop_failure_keeps_ingress_closed(self) -> None:
        harness = _Harness()
        harness.adapter.failure = "stop"

        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            harness.replace_owned_backend()

        self.assertEqual(
            harness.events,
            ["guard", "leases.capture", "adapter.stop"],
        )
        self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

    def test_lease_capture_failure_does_not_stop_or_retire(self) -> None:
        harness = _Harness()
        harness.interaction_leases.failure = "capture"

        with self.assertRaisesRegex(RuntimeError, "lease capture failed"):
            harness.replace_owned_backend()

        self.assertEqual(harness.events, ["guard", "leases.capture"])
        self.assertNotIn("adapter.stop", harness.events)
        self.assertNotIn("adapter.start", harness.events)
        self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

    def test_each_structural_retirement_failure_blocks_replacement(self) -> None:
        for surface in (
            "leases",
            "registry",
            "fcodex",
            "web",
            "local",
            "feishu-root",
            "feishu",
            "transport",
        ):
            with self.subTest(surface=surface):
                harness = _Harness()
                if surface == "leases":
                    harness.interaction_leases.failure = "retire"
                elif surface == "fcodex":
                    harness.owner.failure = "settle"
                elif surface == "local":
                    harness.local_projection_failure = True
                elif surface == "transport":
                    harness.adapter.failure = "transport retirement"
                else:
                    harness.retirement_failure_at = surface

                with self.assertRaisesRegex(RuntimeError, "retirement failed"):
                    harness.replace_owned_backend()

                self.assertNotIn("adapter.start", harness.events)
                self.assertNotIn("gate.invalidate", harness.events)
                self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

    def test_late_retirement_failure_retries_from_the_fence_idempotently(self) -> None:
        harness = _Harness()
        harness.adapter.failure = "transport retirement"

        with self.assertRaisesRegex(RuntimeError, "transport retirement failed"):
            harness.replace_owned_backend()

        first_attempt_events = tuple(harness.events)
        self.assertNotIn("adapter.start", first_attempt_events)
        self.assertFalse(
            any(event.startswith("publish:") for event in first_attempt_events)
        )
        self.assertNotIn("gate.invalidate", first_attempt_events)
        self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

        harness.adapter.failure = ""
        receipt = harness.replace_owned_backend()

        self.assertEqual(receipt.connection_generation, 7)
        for idempotent_step in (
            "leases.capture",
            "adapter.stop",
            "leases.retire",
            "registry.retire",
            "owner.settle",
            "web.retire",
            "local.retire",
            "feishu-root.retire",
            "feishu.retire",
            "adapter.rotate",
        ):
            with self.subTest(step=idempotent_step):
                self.assertEqual(harness.events.count(idempotent_step), 2)
        self.assertEqual(harness.events.count("adapter.start"), 1)
        self.assertEqual(
            harness.events.count("publish:ws://127.0.0.1:8765"),
            1,
        )
        self.assertFalse(harness.gate.snapshot().backend_reset_blocked)

    def test_card_projection_failure_is_best_effort(self) -> None:
        harness = _Harness()
        harness.projection_dispatch_fails = True

        receipt = harness.replace_owned_backend()

        self.assertEqual(receipt.connection_generation, 7)
        self.assertIn("adapter.start", harness.events)
        self.assertFalse(harness.gate.snapshot().backend_reset_blocked)

    def test_each_post_stop_replacement_failure_keeps_ingress_closed(self) -> None:
        cases = (
            ("runtime purge", lambda h: setattr(h.runtime_leases, "failure", True), "runtime purge failed"),
            ("owner close", lambda h: setattr(h.owner, "failure", "close"), "owner close failed"),
            ("authority", lambda h: setattr(h.runtime_authority, "failure", True), "authority confirmation failed"),
            ("adapter start", lambda h: setattr(h.adapter, "failure", "start"), "start failed"),
            ("generation", lambda h: setattr(h.adapter, "failure", "generation"), "generation failed"),
            ("endpoint", lambda h: setattr(h.adapter, "endpoint", ""), "usable endpoint"),
            ("publication", lambda h: setattr(h, "publish_error", RuntimeError("publish failed")), "publish failed"),
        )

        for name, configure, message in cases:
            with self.subTest(name=name):
                harness = _Harness()
                configure(harness)

                with self.assertRaisesRegex(RuntimeError, message):
                    harness.replace_owned_backend()

                self.assertTrue(harness.gate.snapshot().backend_reset_blocked)

    def test_non_positive_replacement_generation_keeps_ingress_closed(self) -> None:
        harness = _Harness()
        harness.adapter.generation = 0

        with self.assertRaisesRegex(RuntimeError, "valid websocket generation"):
            harness.replace_owned_backend()

        self.assertTrue(harness.gate.snapshot().backend_reset_blocked)
        self.assertNotIn("adapter.endpoint", harness.events)


if __name__ == "__main__":
    unittest.main()
