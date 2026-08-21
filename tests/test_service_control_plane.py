import json
import pathlib
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot.service_control_plane import (
    ServiceControlError,
    ServiceControlKnownNotCommittedError,
    ServiceControlOutcomeUnknownError,
    ServiceControlPlane,
    ServiceControlResponseTimeoutError,
    ServiceControlShutdownError,
    control_request,
)
from bot.stores.service_instance_lease import ServiceInstanceLease


class ServiceControlPlaneTests(unittest.TestCase):
    def test_stop_retains_retry_authority_until_request_thread_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire()
            entered = threading.Event()
            release = threading.Event()
            request_result: list[object] = []
            request_errors: list[BaseException] = []

            def dispatch(_method: str, _params: dict) -> dict[str, bool]:
                entered.set()
                release.wait(timeout=2.0)
                return {"released": True}

            plane = ServiceControlPlane(
                data_dir=data_dir,
                dispatch=dispatch,
                owns_current_lease=lease.owns_current_lease,
                auth_token=lambda: lease.owner_token,
            )
            endpoint = plane.start()
            lease.publish_control_endpoint(endpoint)

            def request() -> None:
                try:
                    request_result.append(
                        control_request(data_dir, "test/block", timeout_seconds=2.0)
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    request_errors.append(exc)

            worker = threading.Thread(target=request)
            worker.start()
            self.assertTrue(entered.wait(timeout=1.0))

            with self.assertRaisesRegex(
                ServiceControlShutdownError,
                "request thread",
            ):
                plane.stop(timeout=0.01)

            self.assertEqual(plane.control_endpoint, endpoint)
            release.set()
            worker.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
            self.assertEqual(request_errors, [])
            self.assertEqual(request_result, [{"released": True}])

            plane.stop(timeout=1.0)
            plane.stop(timeout=1.0)
            self.assertEqual(plane.control_endpoint, "")
            lease.release()

    def test_request_thread_cannot_claim_its_own_shutdown_barrier(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            plane: ServiceControlPlane

            def dispatch(_method: str, _params: dict) -> None:
                plane.stop(timeout=0.01)

            plane = ServiceControlPlane(data_dir=data_dir, dispatch=dispatch)
            endpoint = plane.start()
            host, port = endpoint.removeprefix("tcp://").rsplit(":", 1)
            with socket.create_connection((host, int(port))) as connection:
                connection.sendall(
                    b'{"method":"test/reentrant","params":{},"auth_token":""}\n'
                )
                response = json.loads(connection.makefile("rb").readline())

            self.assertFalse(response["ok"])
            self.assertEqual(
                response["error"]["type"],
                "ServiceControlShutdownError",
            )
            self.assertEqual(plane.control_endpoint, endpoint)
            plane.stop(timeout=1.0)

    def test_control_request_distinguishes_response_timeout_after_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire(control_endpoint="tcp://127.0.0.1:32001")
            sent_payloads: list[bytes] = []

            class _FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def settimeout(self, timeout_seconds: float) -> None:
                    self.timeout_seconds = timeout_seconds

                def sendall(self, payload: bytes) -> None:
                    sent_payloads.append(payload)

                def recv(self, size: int) -> bytes:
                    raise TimeoutError("timed out")

            try:
                with patch("bot.service_control_plane.socket.create_connection", return_value=_FakeSocket()):
                    with self.assertRaises(ServiceControlResponseTimeoutError):
                        control_request(data_dir, "service/attach", timeout_seconds=0.1)
            finally:
                lease.release()

            self.assertEqual(len(sent_payloads), 1)

    def test_control_request_treats_eof_after_send_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire(control_endpoint="tcp://127.0.0.1:32001")

            class _FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def settimeout(self, timeout_seconds: float) -> None:
                    del timeout_seconds

                def sendall(self, payload: bytes) -> None:
                    del payload

                def recv(self, size: int) -> bytes:
                    del size
                    return b""

            try:
                with patch("bot.service_control_plane.socket.create_connection", return_value=_FakeSocket()):
                    with self.assertRaises(ServiceControlOutcomeUnknownError):
                        control_request(data_dir, "thread/delete", timeout_seconds=0.1)
            finally:
                lease.release()

    def test_control_request_treats_send_failure_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire(control_endpoint="tcp://127.0.0.1:32001")

            class _FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def settimeout(self, timeout_seconds: float) -> None:
                    del timeout_seconds

                def sendall(self, payload: bytes) -> None:
                    del payload
                    raise BrokenPipeError("closed")

            try:
                with patch("bot.service_control_plane.socket.create_connection", return_value=_FakeSocket()):
                    with self.assertRaises(ServiceControlOutcomeUnknownError):
                        control_request(data_dir, "thread/archive", timeout_seconds=0.1)
            finally:
                lease.release()

    def test_control_request_connection_failure_is_known_not_committed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire(control_endpoint="tcp://127.0.0.1:32001")
            try:
                with patch(
                    "bot.service_control_plane.socket.create_connection",
                    side_effect=ConnectionRefusedError("refused"),
                ):
                    with self.assertRaises(ServiceControlKnownNotCommittedError) as caught:
                        control_request(data_dir, "thread/archive", timeout_seconds=0.1)
            finally:
                lease.release()

            self.assertNotIsInstance(caught.exception, ServiceControlOutcomeUnknownError)

    def test_control_request_treats_invalid_ack_as_outcome_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire(control_endpoint="tcp://127.0.0.1:32001")

            class _FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def settimeout(self, timeout_seconds: float) -> None:
                    del timeout_seconds

                def sendall(self, payload: bytes) -> None:
                    del payload

                def recv(self, size: int) -> bytes:
                    del size
                    return b'{"ok":"invalid"}\n'

            try:
                with patch("bot.service_control_plane.socket.create_connection", return_value=_FakeSocket()):
                    with self.assertRaises(ServiceControlOutcomeUnknownError):
                        control_request(data_dir, "thread/delete", timeout_seconds=0.1)
            finally:
                lease.release()

    def test_control_request_handles_string_error_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = pathlib.Path(tmpdir)
            lease = ServiceInstanceLease(data_dir)
            lease.acquire(control_endpoint="tcp://127.0.0.1:32001")

            class _FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def settimeout(self, timeout_seconds: float) -> None:
                    del timeout_seconds

                def sendall(self, payload: bytes) -> None:
                    del payload

                def recv(self, size: int) -> bytes:
                    del size
                    return (json.dumps({"ok": False, "error": "plain failure"}) + "\n").encode()

            try:
                with patch("bot.service_control_plane.socket.create_connection", return_value=_FakeSocket()):
                    with self.assertRaisesRegex(ServiceControlError, "plain failure") as caught:
                        control_request(data_dir, "thread/delete", timeout_seconds=0.1)
            finally:
                lease.release()

            self.assertNotIsInstance(
                caught.exception,
                ServiceControlKnownNotCommittedError,
            )


if __name__ == "__main__":
    unittest.main()
