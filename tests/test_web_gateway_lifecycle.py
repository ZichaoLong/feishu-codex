from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from bot.web_runtime.gateway import (
    WebGateway,
    WebGatewayConfig,
    WebGatewayShutdownError,
)


class _ResultFuture:
    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.result_calls = 0
        self._done = False

    def result(self, *, timeout: float) -> None:
        self.result_calls += 1
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            if not isinstance(result, TimeoutError):
                self._done = True
            raise result
        self._done = True

    def done(self) -> bool:
        return self._done


class WebGatewayLifecycleTests(unittest.TestCase):
    def _gateway(self, *, thread: Mock, loop: Mock) -> WebGateway:
        gateway = WebGateway.__new__(WebGateway)
        gateway._config = WebGatewayConfig()
        gateway._thread = thread
        gateway._loop = loop
        gateway._stop_lock = threading.Lock()
        gateway._stop_future = None
        gateway._async_stop_completed = False
        gateway._endpoint = "http://127.0.0.1:32100"
        gateway._clear_runtime_store_safely = Mock()
        return gateway

    @staticmethod
    def _loop() -> Mock:
        loop = Mock()
        loop.is_running.return_value = True
        return loop

    @staticmethod
    def _thread(*alive: bool) -> Mock:
        thread = Mock()
        thread.is_alive.side_effect = alive
        return thread

    @staticmethod
    def _submit(future: _ResultFuture):
        def submit(coroutine, _loop):
            coroutine.close()
            return future

        return submit

    def _assert_retry_handles_retained(
        self,
        gateway: WebGateway,
        *,
        thread: Mock,
        loop: Mock,
    ) -> None:
        self.assertIs(gateway._thread, thread)
        self.assertIs(gateway._loop, loop)
        self.assertEqual(gateway.endpoint, "http://127.0.0.1:32100")
        gateway._clear_runtime_store_safely.assert_not_called()

    def test_stop_timeout_retains_inflight_future_and_runtime_handles(self) -> None:
        thread = self._thread(True, True)
        loop = self._loop()
        gateway = self._gateway(thread=thread, loop=loop)
        future = _ResultFuture(TimeoutError("not settled"))

        with patch(
            "bot.web_runtime.gateway.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit(future),
        ):
            with self.assertRaisesRegex(WebGatewayShutdownError, "async cleanup"):
                gateway.stop()

        self.assertIs(gateway._stop_future, future)
        loop.call_soon_threadsafe.assert_not_called()
        thread.join.assert_not_called()
        self._assert_retry_handles_retained(gateway, thread=thread, loop=loop)

    def test_stop_async_exception_is_explicit_and_retains_runtime_handles(self) -> None:
        thread = self._thread(True, True)
        loop = self._loop()
        gateway = self._gateway(thread=thread, loop=loop)
        future = _ResultFuture(RuntimeError("runner cleanup failed"))

        with patch(
            "bot.web_runtime.gateway.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit(future),
        ):
            with self.assertRaisesRegex(
                WebGatewayShutdownError,
                "runner cleanup failed",
            ) as caught:
                gateway.stop()

        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertIsNone(gateway._stop_future)
        loop.call_soon_threadsafe.assert_not_called()
        thread.join.assert_not_called()
        self._assert_retry_handles_retained(gateway, thread=thread, loop=loop)

    def test_stop_live_thread_after_join_retains_runtime_handles(self) -> None:
        thread = self._thread(True, True, True, True)
        loop = self._loop()
        gateway = self._gateway(thread=thread, loop=loop)
        future = _ResultFuture(None)

        with patch(
            "bot.web_runtime.gateway.asyncio.run_coroutine_threadsafe",
            side_effect=self._submit(future),
        ):
            with self.assertRaisesRegex(WebGatewayShutdownError, "thread did not exit"):
                gateway.stop()

        loop.call_soon_threadsafe.assert_called_once_with(loop.stop)
        thread.join.assert_called_once_with(timeout=5.0)
        self.assertTrue(gateway._async_stop_completed)
        self._assert_retry_handles_retained(gateway, thread=thread, loop=loop)

    def test_retry_reuses_timed_out_cleanup_then_clears_only_after_thread_exit(self) -> None:
        thread = self._thread(True, True, True, True, True, False)
        loop = self._loop()
        gateway = self._gateway(thread=thread, loop=loop)
        future = _ResultFuture(TimeoutError("not settled"), None)
        submit = Mock(side_effect=self._submit(future))

        with patch(
            "bot.web_runtime.gateway.asyncio.run_coroutine_threadsafe",
            submit,
        ):
            with self.assertRaises(WebGatewayShutdownError):
                gateway.stop()
            gateway.stop()

        submit.assert_called_once()
        self.assertEqual(future.result_calls, 2)
        self.assertIsNone(gateway._thread)
        self.assertIsNone(gateway._loop)
        self.assertEqual(gateway.endpoint, "")
        gateway._clear_runtime_store_safely.assert_called_once_with()

    def test_retry_after_live_thread_joins_same_thread_without_recleaning(self) -> None:
        thread = self._thread(True, True, True, True, True, True, True, False)
        loop = self._loop()
        loop.is_running.side_effect = (True, False)
        gateway = self._gateway(thread=thread, loop=loop)
        future = _ResultFuture(None)
        submit = Mock(side_effect=self._submit(future))

        with patch(
            "bot.web_runtime.gateway.asyncio.run_coroutine_threadsafe",
            submit,
        ):
            with self.assertRaisesRegex(WebGatewayShutdownError, "thread did not exit"):
                gateway.stop()
            gateway.stop()

        submit.assert_called_once()
        self.assertEqual(thread.join.call_count, 2)
        self.assertIsNone(gateway._thread)
        self.assertIsNone(gateway._loop)
        self.assertEqual(gateway.endpoint, "")
        gateway._clear_runtime_store_safely.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
