import unittest
from types import SimpleNamespace

from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
)
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundGateway,
    FeishuOutboundOperation,
    classify_feishu_api_failure,
)


def _client_with_message_api(message_api: object) -> object:
    return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))


class FeishuOutboundClassificationTests(unittest.TestCase):
    def _classify(
        self,
        operation: FeishuOutboundOperation,
        code: str,
        *,
        allow_destination_proof: bool = True,
    ):
        return classify_feishu_api_failure(
            operation=operation,
            chat_id="chat-1",
            attempt_id=f"attempt-{operation.value}-{code}",
            error_code=code,
            error_message="official response",
            allow_destination_proof=allow_destination_proof,
        )

    def test_only_two_reviewed_codes_prove_destination_loss(self) -> None:
        expected_types = {
            "230002": FeishuDestinationLossProofType.OUTBOUND_BOT_OUTSIDE_CHAT,
            "232009": FeishuDestinationLossProofType.OUTBOUND_CHAT_DISSOLVED,
        }
        for operation in FeishuOutboundOperation:
            for code, proof_type in expected_types.items():
                with self.subTest(operation=operation, code=code):
                    result = self._classify(operation, code)

                    self.assertEqual(result.effect, FeishuOutboundEffect.REJECTED)
                    self.assertEqual(
                        result.destination_liveness,
                        FeishuDestinationLiveness.PROVEN_UNREACHABLE,
                    )
                    self.assertEqual(
                        result.destination_loss_proof(),
                        FeishuDestinationLossProof(
                            source_id=result.attempt_id,
                            chat_id="chat-1",
                            proof_type=proof_type,
                        ),
                    )
                    self.assertFalse(result.safe_to_fallback)

    def test_permission_rejection_keeps_liveness_unknown(self) -> None:
        for operation in FeishuOutboundOperation:
            with self.subTest(operation=operation):
                result = self._classify(operation, "230013")

                self.assertEqual(result.effect, FeishuOutboundEffect.REJECTED)
                self.assertEqual(
                    result.destination_liveness,
                    FeishuDestinationLiveness.UNKNOWN,
                )
                self.assertIsNone(result.destination_loss_proof())
                self.assertTrue(result.safe_to_fallback)

    def test_sending_in_progress_and_unreviewed_codes_keep_effect_unknown(self) -> None:
        for operation in FeishuOutboundOperation:
            for code in ("230049", "future-code"):
                with self.subTest(operation=operation, code=code):
                    result = self._classify(operation, code)

                    self.assertEqual(result.effect, FeishuOutboundEffect.UNKNOWN)
                    self.assertEqual(
                        result.destination_liveness,
                        FeishuDestinationLiveness.UNKNOWN,
                    )
                    self.assertIsNone(result.destination_loss_proof())
                    self.assertFalse(result.safe_to_fallback)

    def test_non_chat_create_cannot_manufacture_destination_loss(self) -> None:
        result = self._classify(
            FeishuOutboundOperation.CREATE_MESSAGE,
            "230002",
            allow_destination_proof=False,
        )

        self.assertEqual(result.effect, FeishuOutboundEffect.REJECTED)
        self.assertEqual(
            result.destination_liveness,
            FeishuDestinationLiveness.UNKNOWN,
        )
        self.assertIsNone(result.destination_loss_proof())


class FeishuOutboundGatewayTests(unittest.TestCase):
    def _gateway(self, message_api: object):
        proofs: list[FeishuDestinationLossProof] = []
        client = _client_with_message_api(message_api)
        return (
            FeishuOutboundGateway(
                client=lambda: client,
                publish_destination_loss=proofs.append,
                request_timeout_seconds=15,
            ),
            proofs,
        )

    def test_patch_frequency_limit_is_rejected_and_retryable(self) -> None:
        response = SimpleNamespace(
            code=230020,
            msg="This operation triggers the frequency limit",
            raw={"ext": ""},
            success=lambda: False,
        )
        gateway, _proofs = self._gateway(
            SimpleNamespace(patch=lambda _request: response)
        )

        result = gateway.patch_message("chat-1", "om_123", "{}")

        self.assertEqual(result.effect, FeishuOutboundEffect.REJECTED)
        self.assertEqual(result.retry_after_seconds, 2.0)
        self.assertTrue(result.safe_to_fallback)

    def test_patch_timeout_is_unknown_and_retryable(self) -> None:
        def _raise_timeout(_request):
            raise TimeoutError("Read timed out.")

        gateway, proofs = self._gateway(SimpleNamespace(patch=_raise_timeout))

        result = gateway.patch_message("chat-1", "om_456", "{}")

        self.assertEqual(result.effect, FeishuOutboundEffect.UNKNOWN)
        self.assertEqual(result.retry_after_seconds, 2.0)
        self.assertFalse(result.safe_to_fallback)
        self.assertEqual(proofs, [])

    def test_patch_invalid_card_content_is_rejected(self) -> None:
        response = SimpleNamespace(
            code=230099,
            msg="Failed to create card content: markdown content parse error",
            raw={"ext": "ErrCode: 11311"},
            success=lambda: False,
        )
        gateway, _proofs = self._gateway(
            SimpleNamespace(patch=lambda _request: response)
        )

        result = gateway.patch_message("chat-1", "om_invalid", "{}")

        self.assertEqual(result.effect, FeishuOutboundEffect.REJECTED)
        self.assertTrue(result.content_rejected)
        self.assertTrue(result.safe_to_fallback)

    def test_create_and_reply_use_official_uuid(self) -> None:
        captured_create: list[object] = []
        captured_reply: list[object] = []
        response = SimpleNamespace(
            data=SimpleNamespace(message_id="message-confirmed"),
            success=lambda: True,
        )
        gateway, _proofs = self._gateway(
            SimpleNamespace(
                create=lambda request: captured_create.append(request) or response,
                reply=lambda request: captured_reply.append(request) or response,
            )
        )

        create_result = gateway.send_message(
            "chat-1",
            "text",
            "{}",
            attempt_id="attempt-create",
        )
        reply_result = gateway.reply_to_message(
            "chat-1",
            "parent-1",
            "text",
            "{}",
            reply_in_thread=True,
            attempt_id="attempt-reply",
        )

        self.assertEqual(captured_create[0].request_body.uuid, "attempt-create")
        self.assertEqual(captured_reply[0].request_body.uuid, "attempt-reply")
        self.assertTrue(captured_reply[0].request_body.reply_in_thread)
        self.assertEqual(create_result.attempt_id, "attempt-create")
        self.assertEqual(reply_result.attempt_id, "attempt-reply")

    def test_permanent_codes_publish_proof_for_every_operation(self) -> None:
        for operation in FeishuOutboundOperation:
            for code in (230002, 232009):
                with self.subTest(operation=operation, code=code):
                    response = SimpleNamespace(
                        code=code,
                        msg="permanent destination loss",
                        raw={"ext": ""},
                        success=lambda: False,
                    )
                    gateway, proofs = self._gateway(
                        SimpleNamespace(
                            create=lambda _request: response,
                            reply=lambda _request: response,
                            patch=lambda _request: response,
                        )
                    )
                    attempt_id = f"attempt-{operation.value}-{code}"
                    if operation is FeishuOutboundOperation.CREATE_MESSAGE:
                        result = gateway.send_message(
                            "chat-1", "text", "{}", attempt_id=attempt_id
                        )
                    elif operation is FeishuOutboundOperation.REPLY_MESSAGE:
                        result = gateway.reply_to_message(
                            "chat-1",
                            "parent-1",
                            "text",
                            "{}",
                            reply_in_thread=False,
                            attempt_id=attempt_id,
                        )
                    else:
                        result = gateway.patch_message(
                            "chat-1",
                            "message-1",
                            "{}",
                            attempt_id=attempt_id,
                        )

                    proof = result.destination_loss_proof()
                    self.assertIsNotNone(proof)
                    self.assertEqual(proofs, [proof])
                    self.assertEqual(proof.source_id, attempt_id)
                    self.assertEqual(proof.chat_id, "chat-1")
                    self.assertFalse(result.safe_to_fallback)


if __name__ == "__main__":
    unittest.main()
