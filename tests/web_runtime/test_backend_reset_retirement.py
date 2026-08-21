from __future__ import annotations

import uuid
from unittest.mock import Mock, call

from bot.jsonrpc_id import jsonrpc_id_key
from bot.web_runtime.contract import WebRuntimeError
from tests.web_runtime.harness import WebRuntimeControllerHarness


class WebRuntimeBackendResetRetirementTests(WebRuntimeControllerHarness):
    def test_confirmed_stop_retires_requests_prompt_locators_and_control_unknowns(
        self,
    ):
        self.controller.read_thread("tab-1", "thread-1")
        prompt_mutation_id = str(uuid.uuid4())
        prompt = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="hello",
            mutation_id=prompt_mutation_id,
        )
        submission = self.operations.acquire_exclusive_turn_submission(
            "tab-1", "thread-1"
        )
        self.operations.activate_turn_submission(submission, "turn-1")
        self.assertTrue(
            self._handle_adapter_request(
                "retired-approval",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "pwd",
                },
            )
        )
        request_key = jsonrpc_id_key("retired-approval")
        control = self.operations.record_unknown_mutation(
            "thread-1",
            operation="archive",
            client_id="tab-1",
        )

        receipt = self.controller.retire_backend_epoch_after_stop()

        self.assertEqual(prompt["status"], "succeeded")
        self.assertEqual(receipt.interaction_requests.count, 1)
        self.assertEqual(receipt.interaction_requests.request_keys, {request_key})
        self.assertEqual(receipt.prompt_results.count, 1)
        self.assertEqual(receipt.mutations.retired_count, 1)
        self.assertEqual(
            receipt.mutations.retired_mutation_ids,
            (control.mutation_id,),
        )
        self.assertIsNone(self.interaction_inbox.snapshot(request_key))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))
        with self.assertRaises(WebRuntimeError) as locator:
            self.controller.prompt_result(
                "tab-1",
                "thread-1",
                mutation_id=prompt_mutation_id,
            )
        self.assertEqual(locator.exception.code, "prompt_result_unavailable")

        retry = self.controller.retire_backend_epoch_after_stop()
        self.assertEqual(retry.interaction_requests.count, 0)
        self.assertEqual(retry.prompt_results.count, 0)
        self.assertEqual(retry.mutations.retired_count, 0)

    def test_aggregate_orders_interaction_before_prompt_and_control_retirement(self):
        order = []
        original_interactions = (
            self.controller._interaction_responses.retire_backend_epoch_after_stop
        )
        original_prompts = (
            self.controller._prompt_submissions.retire_backend_epoch_after_stop
        )
        original_mutations = self.operations.retire_backend_epoch_after_stop
        self.controller._interaction_responses.retire_backend_epoch_after_stop = (  # noqa: SLF001
            Mock(
                side_effect=lambda: (
                    order.append("interactions"),
                    original_interactions(),
                )[1]
            )
        )
        self.controller._prompt_submissions.retire_backend_epoch_after_stop = (  # noqa: SLF001
            Mock(
                side_effect=lambda: (
                    order.append("prompts"),
                    original_prompts(),
                )[1]
            )
        )
        self.operations.retire_backend_epoch_after_stop = Mock(
            side_effect=lambda: (order.append("mutations"), original_mutations())[1]
        )

        receipt = self.controller.retire_backend_epoch_after_stop()

        self.assertEqual(order, ["interactions", "prompts", "mutations"])
        self.assertEqual(receipt.interaction_requests.count, 0)
        self.assertEqual(receipt.prompt_results.count, 0)
        self.assertEqual(receipt.mutations.retired_count, 0)
        self.assertEqual(
            self.controller._interaction_responses.retire_backend_epoch_after_stop.mock_calls,  # noqa: SLF001
            [call()],
        )
        self.assertEqual(
            self.controller._prompt_submissions.retire_backend_epoch_after_stop.mock_calls,  # noqa: SLF001
            [call()],
        )
        self.assertEqual(
            self.operations.retire_backend_epoch_after_stop.mock_calls,
            [call()],
        )

    def test_exact_retired_control_is_explained_without_prompt_recovery(self):
        self.controller.read_thread("tab-1", "thread-1")
        pending = self.operations.record_unknown_mutation(
            "thread-1",
            operation="delete",
            client_id="tab-1",
        )
        effect_count = len(self.fake.steered) + len(self.fake.started)

        receipt = self.operations.retire_backend_epoch_after_stop()

        self.assertEqual(receipt.retired_count, 1)
        self.assertEqual(receipt.retired_mutation_ids, (pending.mutation_id,))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))
        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.resolve_unknown_mutation(
                "tab-1",
                "thread-1",
                action="discard",
                mutation_id=pending.mutation_id,
            )
        self.assertEqual(caught.exception.code, "mutation_backend_replaced")
        self.assertEqual(len(self.fake.steered) + len(self.fake.started), effect_count)
