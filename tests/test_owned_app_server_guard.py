from __future__ import annotations

import ctypes
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


_GUARD_MODULE = "bot.owned_app_server_guard"
_PROCESS_TIMEOUT_SECONDS = 15.0


def _guard_argv(
    child_argv: list[str],
    *,
    terminate_timeout: float = 0.2,
    cleanup_receipt_path: pathlib.Path | None = None,
    cleanup_token: str | None = None,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        _GUARD_MODULE,
        "--terminate-timeout-seconds",
        str(terminate_timeout),
    ]
    if cleanup_receipt_path is not None or cleanup_token is not None:
        if cleanup_receipt_path is None or cleanup_token is None:
            raise ValueError("receipt path and token must be provided together")
        argv.extend(
            [
                "--cleanup-receipt-path",
                str(cleanup_receipt_path),
                "--cleanup-token",
                cleanup_token,
            ]
        )
    return [*argv, "--", *child_argv]


def _activate_guard(process: subprocess.Popen[str]) -> None:
    if process.stdin is None:
        raise AssertionError("guard activation pipe is unavailable")
    process.stdin.write("1\n")
    process.stdin.flush()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        synchronize = 0x00100000
        wait_object_0 = 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) != wait_object_0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_not_running(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _force_stop_pid(pid: int) -> None:
    if not _pid_is_running(pid):
        return
    try:
        os.kill(pid, signal.SIGKILL if os.name == "posix" else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return


def _read_ready_record(path: pathlib.Path, process: subprocess.Popen[str]) -> dict[str, int]:
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            else:
                if isinstance(payload, dict):
                    return {str(key): int(value) for key, value in payload.items()}
        returncode = process.poll()
        if returncode is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(
                f"guard exited before child readiness: rc={returncode}, stderr={stderr}"
            )
        time.sleep(0.05)
    raise AssertionError("timed out waiting for guarded child readiness")


def _long_lived_tree_script() -> str:
    return """
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ready_path = pathlib.Path(sys.argv[1])
grandchild_ready_path = pathlib.Path(sys.argv[2])
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
grandchild_code = r'''\
import os
import pathlib
import signal
import sys
import time
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(1)
'''
grandchild = subprocess.Popen([sys.executable, "-c", grandchild_code, str(grandchild_ready_path)])
deadline = time.monotonic() + 10
while not grandchild_ready_path.exists():
    if grandchild.poll() is not None:
        raise SystemExit("grandchild exited before readiness")
    if time.monotonic() >= deadline:
        raise SystemExit("grandchild readiness timed out")
    time.sleep(0.02)
ready_path.write_text(
    json.dumps({"leader": os.getpid(), "descendant": grandchild.pid}),
    encoding="utf-8",
)
while True:
    time.sleep(1)
"""


def _natural_exit_with_descendant_script() -> str:
    return """
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ready_path = pathlib.Path(sys.argv[1])
grandchild_ready_path = pathlib.Path(sys.argv[2])
grandchild_code = r'''\
import os
import pathlib
import signal
import sys
import time
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(1)
'''
grandchild = subprocess.Popen([sys.executable, "-c", grandchild_code, str(grandchild_ready_path)])
deadline = time.monotonic() + 10
while not grandchild_ready_path.exists():
    if grandchild.poll() is not None:
        raise SystemExit("grandchild exited before readiness")
    if time.monotonic() >= deadline:
        raise SystemExit("grandchild readiness timed out")
    time.sleep(0.02)
ready_path.write_text(
    json.dumps({"leader": os.getpid(), "descendant": grandchild.pid}),
    encoding="utf-8",
)
raise SystemExit(9)
"""


def _detached_descendant_script() -> str:
    return """
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

ready_path = pathlib.Path(sys.argv[1])
descendant_code = r'''\
import os
import signal
import time
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
'''
descendant = subprocess.Popen(
    [sys.executable, "-c", descendant_code],
    start_new_session=True,
)
ready_path.write_text(
    json.dumps({"leader": os.getpid(), "detached": descendant.pid}),
    encoding="utf-8",
)
while True:
    time.sleep(1)
"""


class OwnedAppServerGuardTests(unittest.TestCase):
    def test_parser_accepts_cleanup_token_with_leading_hyphen(self) -> None:
        from bot import owned_app_server_guard as guard_module

        timeout, receipt_path, cleanup_token, child_argv = guard_module._parse_args(
            [
                "--cleanup-receipt-path=cleanup-receipt.json",
                "--cleanup-token=-leading-hyphen-token",
                "--",
                "codex",
                "app-server",
            ]
        )

        self.assertEqual(timeout, 3.0)
        self.assertEqual(receipt_path, pathlib.Path("cleanup-receipt.json"))
        self.assertEqual(cleanup_token, "-leading-hyphen-token")
        self.assertEqual(child_argv, ["codex", "app-server"])

    def test_windows_job_query_uses_the_five_argument_abi(self) -> None:
        from ctypes import wintypes

        from bot import owned_app_server_guard as guard_module

        query = MagicMock(return_value=True)
        kernel32 = MagicMock()
        kernel32.QueryInformationJobObject = query
        job = guard_module._WindowsJob(123, kernel32)

        self.assertEqual(job.active_processes(), 0)
        self.assertEqual(len(query.argtypes), 5)
        self.assertIs(query.argtypes[-1], ctypes.POINTER(wintypes.DWORD))
        self.assertEqual(len(query.call_args.args), 5)

    def test_windows_job_query_reads_nonzero_active_processes_from_abi_layout(
        self,
    ) -> None:
        from ctypes import wintypes

        from bot import owned_app_server_guard as guard_module

        class ExpectedBasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        def populate_accounting(
            _job_handle,
            _information_class,
            information,
            information_size,
            _return_length,
        ) -> bool:
            self.assertEqual(
                information_size,
                ctypes.sizeof(ExpectedBasicAccountingInformation),
            )
            accounting = ctypes.cast(
                information,
                ctypes.POINTER(ExpectedBasicAccountingInformation),
            ).contents
            accounting.ActiveProcesses = 7
            return True

        query = MagicMock(side_effect=populate_accounting)
        kernel32 = MagicMock()
        kernel32.QueryInformationJobObject = query
        job = guard_module._WindowsJob(123, kernel32)

        self.assertEqual(job.active_processes(), 7)

    def test_receipt_write_failure_returns_fail_closed_status(self) -> None:
        from bot import owned_app_server_guard as guard_module

        with patch.object(
            guard_module,
            "atomic_write_text",
            side_effect=OSError("disk unavailable"),
        ):
            returncode = guard_module._complete_guard(
                0,
                receipt_path=pathlib.Path("cleanup-receipt.json"),
                cleanup_token="cleanup-token",
            )

        self.assertEqual(returncode, 70)

    def test_cleanup_failure_retains_authority_until_a_later_proof(self) -> None:
        from bot import owned_app_server_guard as guard_module

        class FakeWatcher:
            def __init__(self) -> None:
                self.activated = threading.Event()
                self.parent_lost = threading.Event()

            def start(self) -> None:
                self.activated.set()
                return None

        watcher = FakeWatcher()
        child = guard_module._OwnedChild(
            process=MagicMock(spec=subprocess.Popen),
            posix_process_group_id=123,
        )

        def spawn(_argv: list[str]) -> object:
            watcher.parent_lost.set()
            return child

        with (
            patch.object(guard_module, "_ParentPipeWatcher", return_value=watcher),
            patch.object(guard_module, "_spawn_owned_child", side_effect=spawn),
            patch.object(
                guard_module,
                "_terminate_owned_tree",
                side_effect=[
                    guard_module.ProcessTreeCleanupError("first proof failed"),
                    None,
                ],
            ) as terminate_tree,
            patch.object(guard_module.time, "sleep"),
        ):
            returncode = guard_module.run_guard(["codex"])

        self.assertEqual(returncode, 70)
        self.assertEqual(terminate_tree.call_count, 2)

    def test_natural_child_exit_propagates_code_and_inherits_output_not_stdin(self) -> None:
        child_code = (
            "import sys; "
            "payload = sys.stdin.buffer.read(); "
            "print('child-stdout', flush=True); "
            "print('child-stderr', file=sys.stderr, flush=True); "
            "raise SystemExit(7 if payload == b'' else 8)"
        )
        guard = subprocess.Popen(
            _guard_argv([sys.executable, "-c", child_code]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIsNotNone(guard.stdin)
        try:
            _activate_guard(guard)
            returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
            stdout = guard.stdout.read() if guard.stdout is not None else ""
            stderr = guard.stderr.read() if guard.stderr is not None else ""
        finally:
            guard.stdin.close()
            if guard.poll() is None:
                guard.kill()
                guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)

        self.assertEqual(returncode, 7, stderr)
        self.assertIn("child-stdout", stdout)
        self.assertIn("child-stderr", stderr)

    def test_parent_pipe_eof_terminates_and_reaps_entire_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ready_path = pathlib.Path(tmpdir) / "tree.json"
            grandchild_ready_path = pathlib.Path(tmpdir) / "grandchild.txt"
            cleanup_receipt_path = pathlib.Path(tmpdir) / "cleanup-receipt.json"
            cleanup_token = "cleanup-token"
            guard = subprocess.Popen(
                _guard_argv(
                    [
                        sys.executable,
                        "-c",
                        _long_lived_tree_script(),
                        str(ready_path),
                        str(grandchild_ready_path),
                    ],
                    cleanup_receipt_path=cleanup_receipt_path,
                    cleanup_token=cleanup_token,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIsNotNone(guard.stdin)
            pids: dict[str, int] = {}
            try:
                _activate_guard(guard)
                pids = _read_ready_record(ready_path, guard)
                guard.stdin.close()
                returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                stderr = guard.stderr.read() if guard.stderr is not None else ""
            finally:
                if guard.stdin is not None and not guard.stdin.closed:
                    guard.stdin.close()
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                for pid in pids.values():
                    _force_stop_pid(pid)

            self.assertEqual(returncode, 0, stderr)
            for label, pid in pids.items():
                self.assertTrue(
                    _wait_until_not_running(pid),
                    f"{label} process leaked after guard EOF: pid={pid}",
                )
            self.assertEqual(
                json.loads(cleanup_receipt_path.read_text(encoding="utf-8")),
                {"cleanup_token": cleanup_token},
            )

    def test_parent_eof_before_activation_never_launches_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            marker_path = pathlib.Path(tmpdir) / "child-started.txt"
            cleanup_receipt_path = pathlib.Path(tmpdir) / "cleanup-receipt.json"
            cleanup_token = "pre-activation-token"
            guard = subprocess.Popen(
                _guard_argv(
                    [
                        sys.executable,
                        "-c",
                        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')",
                        str(marker_path),
                    ],
                    cleanup_receipt_path=cleanup_receipt_path,
                    cleanup_token=cleanup_token,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIsNotNone(guard.stdin)
            guard.stdin.close()
            returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
            stderr = guard.stderr.read() if guard.stderr is not None else ""

            self.assertEqual(returncode, 0, stderr)
            self.assertFalse(marker_path.exists())
            self.assertEqual(
                json.loads(cleanup_receipt_path.read_text(encoding="utf-8")),
                {"cleanup_token": cleanup_token},
            )

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "Linux subreaper containment contract",
    )
    def test_linux_subreaper_cleans_descendant_in_a_separate_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ready_path = pathlib.Path(tmpdir) / "detached-tree.json"
            guard = subprocess.Popen(
                _guard_argv(
                    [
                        sys.executable,
                        "-c",
                        _detached_descendant_script(),
                        str(ready_path),
                    ]
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIsNotNone(guard.stdin)
            pids: dict[str, int] = {}
            try:
                _activate_guard(guard)
                pids = _read_ready_record(ready_path, guard)
                guard.stdin.close()
                returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                stderr = guard.stderr.read() if guard.stderr is not None else ""
            finally:
                if guard.stdin is not None and not guard.stdin.closed:
                    guard.stdin.close()
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                for pid in pids.values():
                    _force_stop_pid(pid)

            self.assertEqual(returncode, 0, stderr)
            self.assertTrue(
                _wait_until_not_running(pids["detached"]),
                f"detached descendant leaked: pid={pids['detached']}",
            )

    def test_natural_leader_exit_cleans_descendants_before_propagating_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ready_path = pathlib.Path(tmpdir) / "tree.json"
            grandchild_ready_path = pathlib.Path(tmpdir) / "grandchild.txt"
            guard = subprocess.Popen(
                _guard_argv(
                    [
                        sys.executable,
                        "-c",
                        _natural_exit_with_descendant_script(),
                        str(ready_path),
                        str(grandchild_ready_path),
                    ]
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIsNotNone(guard.stdin)
            pids: dict[str, int] = {}
            try:
                _activate_guard(guard)
                pids = _read_ready_record(ready_path, guard)
                returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                stderr = guard.stderr.read() if guard.stderr is not None else ""
            finally:
                guard.stdin.close()
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                for pid in pids.values():
                    _force_stop_pid(pid)

            self.assertEqual(returncode, 9, stderr)
            self.assertTrue(
                _wait_until_not_running(pids["descendant"]),
                f"descendant leaked after natural leader exit: pid={pids['descendant']}",
            )

    @unittest.skipUnless(os.name == "posix", "POSIX signal-handler contract")
    def test_guard_sigterm_cleans_tree_and_returns_signal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ready_path = pathlib.Path(tmpdir) / "tree.json"
            grandchild_ready_path = pathlib.Path(tmpdir) / "grandchild.txt"
            guard = subprocess.Popen(
                _guard_argv(
                    [
                        sys.executable,
                        "-c",
                        _long_lived_tree_script(),
                        str(ready_path),
                        str(grandchild_ready_path),
                    ]
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertIsNotNone(guard.stdin)
            pids: dict[str, int] = {}
            try:
                _activate_guard(guard)
                pids = _read_ready_record(ready_path, guard)
                os.kill(guard.pid, signal.SIGTERM)
                returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                stderr = guard.stderr.read() if guard.stderr is not None else ""
            finally:
                guard.stdin.close()
                if guard.poll() is None:
                    guard.kill()
                    guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
                for pid in pids.values():
                    _force_stop_pid(pid)

            self.assertEqual(returncode, 128 + signal.SIGTERM, stderr)
            for label, pid in pids.items():
                self.assertTrue(
                    _wait_until_not_running(pid),
                    f"{label} process leaked after guard SIGTERM: pid={pid}",
                )

    def test_child_launch_failure_is_reported_without_hanging(self) -> None:
        missing = f"focus-missing-child-{os.getpid()}-{time.time_ns()}"
        guard = subprocess.Popen(
            _guard_argv([missing]),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIsNotNone(guard.stdin)
        try:
            _activate_guard(guard)
            returncode = guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)
            stderr = guard.stderr.read() if guard.stderr is not None else ""
        finally:
            guard.stdin.close()
            if guard.poll() is None:
                guard.kill()
                guard.wait(timeout=_PROCESS_TIMEOUT_SECONDS)

        self.assertEqual(returncode, 127)
        self.assertIn("could not launch child", stderr)


if __name__ == "__main__":
    unittest.main()
