from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import unittest
from unittest.mock import patch

from bot.web_runtime.gateway_external_transaction import (
    WebGatewayExternalTransactionRunner,
)
from tests.web_runtime.gateway_harness import WebGatewayHarness


class _ExactReceipt:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "prepared"
        self.abandon_attempts = 0
        self.settlements = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def claim(self) -> None:
        with self._lock:
            if self._state != "prepared":
                raise RuntimeError("receipt is no longer prepared")
            self._state = "claimed"

    def settle(self) -> None:
        with self._lock:
            if self._state != "claimed":
                raise RuntimeError("receipt was not claimed")
            self._state = "settled"
            self.settlements += 1

    def abandon(self, receipt: object) -> bool:
        if receipt is not self:
            raise RuntimeError("abandon received a different receipt")
        with self._lock:
            self.abandon_attempts += 1
            if self._state != "prepared":
                return False
            self._state = "abandoned"
            self.settlements += 1
            return True


class WebGatewayExternalTransactionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_async_prepare_abandons_eventual_receipt(self) -> None:
        receipt = _ExactReceipt()
        runner = WebGatewayExternalTransactionRunner(receipt.abandon)
        prepare_entered = asyncio.Event()
        release_prepare = asyncio.Event()
        prepare_finished = asyncio.Event()
        child_tasks: list[asyncio.Task[_ExactReceipt]] = []

        async def prepare_owned() -> _ExactReceipt:
            child = asyncio.current_task()
            assert child is not None
            child_tasks.append(child)
            prepare_entered.set()
            try:
                await release_prepare.wait()
                return receipt
            finally:
                prepare_finished.set()

        request = asyncio.create_task(runner.prepare_async(prepare_owned))
        await prepare_entered.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request

        self.assertFalse(prepare_finished.is_set())
        self.assertEqual(receipt.state, "prepared")
        release_prepare.set()
        await prepare_finished.wait()
        await asyncio.sleep(0)

        self.assertEqual(receipt.state, "abandoned")
        self.assertEqual(receipt.abandon_attempts, 1)
        self.assertEqual(receipt.settlements, 1)
        self.assertEqual(len(child_tasks), 1)
        self.assertTrue(child_tasks[0].done())

    async def test_cancelled_async_prepare_consumes_late_failure(self) -> None:
        receipt = _ExactReceipt()
        runner = WebGatewayExternalTransactionRunner(receipt.abandon)
        prepare_entered = asyncio.Event()
        release_prepare = asyncio.Event()
        prepare_finished = asyncio.Event()
        child_tasks: list[asyncio.Task[object]] = []
        failure = RuntimeError("late prepare failure")

        async def fail_prepare() -> object:
            child = asyncio.current_task()
            assert child is not None
            child_tasks.append(child)
            prepare_entered.set()
            try:
                await release_prepare.wait()
                raise failure
            finally:
                prepare_finished.set()

        request = asyncio.create_task(runner.prepare_async(fail_prepare))
        await prepare_entered.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request

        release_prepare.set()
        await prepare_finished.wait()
        await asyncio.sleep(0)

        self.assertEqual(receipt.state, "prepared")
        self.assertEqual(receipt.abandon_attempts, 0)
        self.assertEqual(len(child_tasks), 1)
        self.assertTrue(child_tasks[0].done())
        self.assertIs(child_tasks[0].exception(), failure)

    async def test_executor_admission_failure_abandons_unclaimed_receipt(
        self,
    ) -> None:
        receipt = _ExactReceipt()
        runner = WebGatewayExternalTransactionRunner(receipt.abandon)
        failure = RuntimeError("executor admission failed")

        async def fail_executor_admission(*_args, **_kwargs):
            raise failure

        with patch(
            "bot.web_runtime.gateway_external_transaction.asyncio.to_thread",
            fail_executor_admission,
        ):
            with self.assertRaises(RuntimeError) as raised:
                await runner.execute(receipt, lambda prepared: prepared)

        self.assertIs(raised.exception, failure)
        self.assertEqual(receipt.state, "abandoned")
        self.assertEqual(receipt.abandon_attempts, 1)
        self.assertEqual(receipt.settlements, 1)


class WebGatewayPreparedDocumentTransactionTests(WebGatewayHarness):
    async def test_cancelled_prepare_blocks_document_reissue_until_worker_exits(
        self,
    ) -> None:
        await self._authenticate()
        original_document = await self._register_document(
            resume_client_id="cancelled-prepare-client",
            incarnation_id="before-cancelled-prepare",
        )
        client_id = original_document["client_id"]
        receipt = _ExactReceipt()
        prepare_entered = threading.Event()
        release_prepare = threading.Event()
        prepare_finished = threading.Event()
        receipt_abandoned = threading.Event()
        continuity_lock = threading.Lock()
        continuity = "original"
        executed: list[_ExactReceipt] = []

        def blocking_prepare(
            prepared_client_id: str,
            thread_id: str,
            **_kwargs,
        ) -> _ExactReceipt:
            nonlocal continuity
            self.assertEqual(prepared_client_id, client_id)
            self.assertEqual(thread_id, "thread-1")
            prepare_entered.set()
            try:
                if not release_prepare.wait(timeout=2.0):
                    raise TimeoutError("test did not release document prepare")
                with continuity_lock:
                    continuity = "old_prepare"
                return receipt
            finally:
                prepare_finished.set()

        def run_prepared(prepared: _ExactReceipt) -> dict[str, object]:
            nonlocal continuity
            executed.append(prepared)
            with continuity_lock:
                continuity = "old_materialized"
            prepared.claim()
            prepared.settle()
            return {"thread": {"id": "thread-1"}}

        def abandon_prepared(prepared: object) -> bool:
            abandoned = receipt.abandon(prepared)
            receipt_abandoned.set()
            return abandoned

        def record_document_reissue(reissued_client_id: str) -> None:
            nonlocal continuity
            self.document_reissued.append(reissued_client_id)
            with continuity_lock:
                continuity = "replacement"

        self.gateway._ports.prepare_read_thread = blocking_prepare
        self.gateway._ports.run_prepared_thread_read = run_prepared
        self.gateway._ports.client_document_reissued = record_document_reissue
        self.gateway._external_transactions = WebGatewayExternalTransactionRunner(
            abandon_prepared
        )
        gateway_loop = self.gateway._loop
        self.assertIsNotNone(gateway_loop)
        assert gateway_loop is not None

        async def cancel_initial_disconnect_timer() -> None:
            task = self.gateway._disconnect_tasks.pop(client_id, None)
            if task is None:
                return
            task.cancel()
            await task

        await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(
                cancel_initial_disconnect_timer(),
                gateway_loop,
            )
        )

        async def request_old_document() -> object:
            return await self.gateway._staged_document_request_to_thread(
                blocking_prepare,
                object(),
                client_id,
                "thread-1",
            )

        with patch.object(
            self.gateway,
            "_required_client_id",
            return_value=client_id,
        ):
            request = asyncio.run_coroutine_threadsafe(
                request_old_document(),
                gateway_loop,
            )
            self.assertTrue(
                await asyncio.to_thread(prepare_entered.wait, 1.0)
            )

        registration: asyncio.Task[dict] | None = None
        try:
            self.assertTrue(request.cancel())
            marker = asyncio.run_coroutine_threadsafe(
                asyncio.sleep(0),
                gateway_loop,
            )
            await asyncio.wrap_future(marker)
            with self.assertRaises(concurrent.futures.CancelledError):
                request.result(timeout=0)

            registration = asyncio.create_task(
                self._register_document(
                    resume_client_id=client_id,
                    incarnation_id="after-cancelled-prepare",
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(registration.done())
            self.assertEqual(self.document_reissued, [])
            with continuity_lock:
                self.assertEqual(continuity, "original")
        finally:
            release_prepare.set()

        self.assertTrue(await asyncio.to_thread(prepare_finished.wait, 1.0))
        assert registration is not None
        replacement_document = await asyncio.wait_for(registration, timeout=1.0)
        self.assertTrue(await asyncio.to_thread(receipt_abandoned.wait, 1.0))

        self.assertEqual(replacement_document["client_id"], client_id)
        self.assertEqual(self.document_reissued, [client_id])
        self.assertEqual(receipt.state, "abandoned")
        self.assertEqual(receipt.abandon_attempts, 1)
        self.assertEqual(executed, [])
        with continuity_lock:
            self.assertEqual(continuity, "replacement")
        self.assertNotIn(client_id, self.gateway._client_operation_lock_users)
        self.assertFalse(self.gateway._client_operation_locks[client_id].locked())


class WebGatewayExternalTransactionExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_failure_after_claim_is_not_double_settled(self) -> None:
        receipt = _ExactReceipt()
        runner = WebGatewayExternalTransactionRunner(receipt.abandon)
        failure = ValueError("claimed execution failed")

        def fail_after_claim(prepared: _ExactReceipt) -> None:
            prepared.claim()
            try:
                raise failure
            finally:
                prepared.settle()

        with self.assertRaises(ValueError) as raised:
            await runner.execute(receipt, fail_after_claim)

        self.assertIs(raised.exception, failure)
        self.assertEqual(receipt.state, "settled")
        self.assertEqual(receipt.abandon_attempts, 1)
        self.assertEqual(receipt.settlements, 1)

    async def test_abandon_failure_does_not_mask_execute_failure(self) -> None:
        failure = ValueError("original execute failure")

        def fail_execute(_prepared: object) -> None:
            raise failure

        def fail_abandon(_prepared: object) -> bool:
            raise RuntimeError("abandon failed")

        runner = WebGatewayExternalTransactionRunner(fail_abandon)
        with self.assertLogs(
            "bot.web_runtime.gateway_external_transaction",
            level="ERROR",
        ):
            with self.assertRaises(ValueError) as raised:
                await runner.execute(object(), fail_execute)

        self.assertIs(raised.exception, failure)

    async def test_cancellation_abandons_receipt_before_execution_claim(
        self,
    ) -> None:
        receipt = _ExactReceipt()
        runner = WebGatewayExternalTransactionRunner(receipt.abandon)
        execution_entered = threading.Event()
        release_execution = threading.Event()
        execution_finished = threading.Event()

        def execute_after_release(prepared: _ExactReceipt) -> None:
            execution_entered.set()
            try:
                if not release_execution.wait(timeout=2.0):
                    raise TimeoutError("test did not release execution")
                prepared.claim()
                prepared.settle()
            finally:
                execution_finished.set()

        request = asyncio.create_task(
            runner.execute(receipt, execute_after_release)
        )
        try:
            self.assertTrue(
                await asyncio.to_thread(execution_entered.wait, 1.0)
            )
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request

            self.assertEqual(receipt.state, "abandoned")
            self.assertEqual(receipt.abandon_attempts, 1)
            self.assertEqual(receipt.settlements, 1)
        finally:
            release_execution.set()
            self.assertTrue(
                await asyncio.to_thread(execution_finished.wait, 1.0)
            )
            await asyncio.sleep(0)

    async def test_cancellation_leaves_claimed_execution_to_settle(self) -> None:
        receipt = _ExactReceipt()
        runner = WebGatewayExternalTransactionRunner(receipt.abandon)
        execution_claimed = threading.Event()
        release_execution = threading.Event()
        execution_finished = threading.Event()

        def execute_after_claim(prepared: _ExactReceipt) -> None:
            prepared.claim()
            execution_claimed.set()
            try:
                if not release_execution.wait(timeout=2.0):
                    raise TimeoutError("test did not release execution")
            finally:
                prepared.settle()
                execution_finished.set()

        request = asyncio.create_task(runner.execute(receipt, execute_after_claim))
        try:
            self.assertTrue(
                await asyncio.to_thread(execution_claimed.wait, 1.0)
            )
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request

            self.assertEqual(receipt.state, "claimed")
            self.assertEqual(receipt.abandon_attempts, 1)
            self.assertEqual(receipt.settlements, 0)
        finally:
            release_execution.set()
            self.assertTrue(
                await asyncio.to_thread(execution_finished.wait, 1.0)
            )
            await asyncio.sleep(0)

        self.assertEqual(receipt.state, "settled")
        self.assertEqual(receipt.abandon_attempts, 1)
        self.assertEqual(receipt.settlements, 1)


if __name__ == "__main__":
    unittest.main()
