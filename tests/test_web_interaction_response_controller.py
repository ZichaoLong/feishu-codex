import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.web_runtime.interaction_inbox import (
    WebAutoResolutionPreparation,
    WebInteractionChange,
    WebInteractionInboxError,
)
from bot.web_runtime.interaction_response_controller import (
    WebInteractionResponseController,
    WebInteractionResponsePorts,
)
from bot.web_runtime.contract import WebRuntimeError


class WebInteractionResponseControllerTests(unittest.TestCase):
    def _build(self):
        inbox = Mock(name="inbox")
        ports = WebInteractionResponsePorts(
            require_client_id=Mock(name="require_client_id"),
            require_connected_writer=Mock(name="require_connected_writer"),
            shared_interaction_eligible=Mock(name="shared_interaction_eligible"),
            publish_changes=Mock(name="publish_changes"),
        )
        ports.require_client_id.side_effect = lambda client_id: client_id
        ports.shared_interaction_eligible.return_value = True
        return WebInteractionResponseController(inbox=inbox, ports=ports), inbox, ports

    def test_shared_response_uses_participant_eligibility_not_writer_guard(self) -> None:
        controller, inbox, ports = self._build()
        inbox.prepare_response.return_value = SimpleNamespace(
            delivery_scope="shared_interaction",
            root_thread_id="root-1",
            thread_id="root-1",
            turn_id="turn-1",
        )
        inbox.submit_response.return_value = SimpleNamespace(
            request_key="request-1",
            status="submitted",
            changes=(),
        )

        result = controller.respond(
            "tab-2",
            "request-1",
            connection_generation=7,
            response_capability="capability-1",
            action="approve_once",
            answers=None,
        )

        self.assertTrue(result["accepted"])
        ports.shared_interaction_eligible.assert_called_once_with(
            "tab-2",
            "root-1",
            "root-1",
            "turn-1",
        )
        ports.require_connected_writer.assert_not_called()

    def test_shared_response_rechecks_endpoint_authority_before_submission(self) -> None:
        controller, inbox, ports = self._build()
        inbox.prepare_response.return_value = SimpleNamespace(
            delivery_scope="shared_interaction",
            root_thread_id="root-1",
            thread_id="root-1",
            turn_id="turn-1",
        )
        ports.shared_interaction_eligible.return_value = False

        with self.assertRaises(WebRuntimeError) as denied:
            controller.respond(
                "stale-tab",
                "request-1",
                connection_generation=7,
                response_capability="capability-1",
                action="approve_once",
                answers=None,
            )

        self.assertEqual(denied.exception.code, "request_not_owned")
        inbox.submit_response.assert_not_called()
        ports.publish_changes.assert_not_called()

    def test_writer_response_keeps_exact_writer_guard(self) -> None:
        controller, inbox, ports = self._build()
        inbox.prepare_response.return_value = SimpleNamespace(
            delivery_scope="writer_interaction",
            root_thread_id="root-1",
            thread_id="child-1",
            turn_id="child-turn",
        )
        inbox.submit_response.return_value = SimpleNamespace(
            request_key="request-1",
            status="submitted",
            changes=(),
        )

        controller.respond(
            "tab-1",
            "request-1",
            connection_generation=7,
            response_capability="capability-1",
            action="approve_once",
            answers=None,
        )

        ports.require_connected_writer.assert_called_once_with("tab-1", "root-1")
        ports.shared_interaction_eligible.assert_not_called()

    def test_auto_resolution_uses_only_the_exact_timer_capability(self) -> None:
        controller, inbox, _ports = self._build()
        response = SimpleNamespace(delivery_scope="shared_interaction")
        inbox.prepare_auto_resolution.return_value = WebAutoResolutionPreparation(
            "ready",
            response,
        )
        inbox.submit_response.return_value = SimpleNamespace(changes=())

        self.assertTrue(controller.auto_resolve_request("request-1", 3, 5))

        inbox.prepare_auto_resolution.assert_called_once_with("request-1", 3, 5)
        inbox.submit_response.assert_called_once_with(
            response,
            action="auto_resolve",
            answers=None,
        )

    def test_stale_or_missing_timer_is_consumed_without_submission(self) -> None:
        cases = (
            (WebAutoResolutionPreparation("missing"), False),
            (WebAutoResolutionPreparation("recognized"), True),
        )
        for preparation, expected in cases:
            with self.subTest(outcome=preparation.outcome):
                controller, inbox, _ports = self._build()
                inbox.prepare_auto_resolution.return_value = preparation
                self.assertEqual(
                    controller.auto_resolve_request("request-1", 3, 5),
                    expected,
                )
                inbox.submit_response.assert_not_called()

    def test_auto_resolution_error_publishes_inbox_changes_and_is_not_retried(
        self,
    ) -> None:
        controller, inbox, ports = self._build()
        response = SimpleNamespace(delivery_scope="shared_interaction")
        inbox.prepare_auto_resolution.return_value = WebAutoResolutionPreparation(
            "ready",
            response,
        )
        changes = (WebInteractionChange("root-1", "response_unknown"),)
        error = WebInteractionInboxError(
            "unknown",
            code="request_response_unknown",
            status=409,
            changes=changes,
        )

        inbox.submit_response.side_effect = error
        with self.assertLogs(
            "bot.web_runtime.interaction_response_controller",
            level="WARNING",
        ):
            self.assertTrue(
                controller.auto_resolve_request("request-1", 3, 5)
            )

        ports.publish_changes.assert_called_once_with(changes)


if __name__ == "__main__":
    unittest.main()
