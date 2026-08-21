"""Guard one Codex app-server lifecycle on behalf of a Focus service.

The Focus process starts this module with a dedicated stdin pipe and keeps the
write end open for exactly as long as it owns the backend. The guarded child
never inherits that pipe. EOF therefore means the owner disappeared, including
an ungraceful crash, and must synchronously settle the platform-proved
containment set before the guard exits. Linux uses subreaper descendants and
Windows a Job Object; macOS can prove only the created process group, not an
arbitrary descendant which deliberately escapes that group.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO, Iterator, Sequence

from bot.atomic_file import atomic_write_text


_DEFAULT_TERMINATE_TIMEOUT_SECONDS = 3.0
_KILL_CONFIRM_TIMEOUT_SECONDS = 5.0
_MONITOR_POLL_SECONDS = 0.05
_GUARD_FAILURE_EXIT_CODE = 70
_CHILD_LAUNCH_FAILURE_EXIT_CODE = 127
_WINDOWS_CREATE_SUSPENDED = 0x00000004


class ProcessTreeCleanupError(RuntimeError):
    """The guard could not prove that its platform containment set disappeared."""


class _ParentPipeWatcher:
    """Turn blocking, cross-platform pipe EOF into a process-local event."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.activated = threading.Event()
        self.parent_lost = threading.Event()
        self.started = threading.Event()
        self.done = threading.Event()
        self._stream = stream
        try:
            self._file_descriptor: int | None = stream.fileno()
        except (AttributeError, OSError, ValueError):
            self._file_descriptor = None
        self._thread = threading.Thread(
            target=self._watch,
            name="focus-owned-app-server-parent-watch",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        self.started.wait()

    def _watch(self) -> None:
        self.started.set()
        try:
            while True:
                # Real owner pipes use the raw descriptor. A daemon thread
                # blocked on ``sys.stdin.buffer`` can otherwise own its IO
                # lock during interpreter finalization and abort an otherwise
                # clean guard exit.
                chunk = (
                    os.read(self._file_descriptor, 1)
                    if self._file_descriptor is not None
                    else self._stream.read(1)
                )
                if not chunk:
                    self.parent_lost.set()
                    return
                self.activated.set()
        except (OSError, ValueError):
            # A closed or broken owner pipe is semantically identical to EOF.
            self.parent_lost.set()
        finally:
            self.done.set()


class _ShutdownSignals:
    def __init__(self) -> None:
        self.requested = threading.Event()
        self.number: int | None = None

    def request(self, signum: int) -> None:
        if self.number is None:
            self.number = int(signum)
        self.requested.set()


@contextmanager
def _installed_shutdown_signal_handlers(
    requests: _ShutdownSignals,
) -> Iterator[None]:
    """Convert guard-directed termination signals into orderly tree cleanup."""

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("owned app-server guard must run on the main thread")

    handled: list[int] = []
    for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is None or int(value) in handled:
            continue
        handled.append(int(value))

    previous: dict[int, object] = {}

    def handle(signum: int, _frame: object) -> None:
        requests.request(signum)

    try:
        for signum in handled:
            try:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, handle)
            except (OSError, ValueError):
                previous.pop(signum, None)
        yield
    finally:
        for signum, old_handler in previous.items():
            signal.signal(signum, old_handler)


class _WindowsJob:
    """Minimal Windows Job Object with kill-on-close tree ownership."""

    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self, handle: object, kernel32: object) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def create(cls) -> _WindowsJob:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            cls._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            cls._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(handle)
            raise error
        return cls(handle, kernel32)

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
            self._handle,
            wintypes.HANDLE(int(process_handle)),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int:
        import ctypes
        from ctypes import wintypes

        class BasicAccountingInformation(ctypes.Structure):
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

        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        accounting = BasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def terminate(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        if not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


@dataclass(slots=True)
class _OwnedChild:
    process: subprocess.Popen[bytes]
    posix_process_group_id: int | None = None
    linux_subreaper: bool = False
    windows_job: _WindowsJob | None = None


def _enable_linux_subreaper() -> bool:
    """Make every orphaned Linux descendant settle under this guardian."""

    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        # PR_SET_CHILD_SUBREAPER. This is a proof prerequisite, not an
        # optimization: upstream deliberately starts MCP and shell children
        # in separate process groups/sessions.
        if prctl(36, 1, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"could not establish Linux subreaper authority: {exc}") from exc
    return True


def _taskkill_unassigned_windows_tree(process: subprocess.Popen[bytes]) -> None:
    """Fail-safe cleanup if a new Windows child could not enter its Job."""

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            check=False,
        )
    except Exception as exc:
        raise ProcessTreeCleanupError(
            f"Windows process-tree cleanup could not run taskkill: {exc}"
        ) from exc
    if result.returncode != 0:
        # The process was created suspended, so a root which is already gone
        # could not have created an untracked descendant.
        if process.poll() is not None:
            return
        detail = result.stderr.decode(errors="replace").strip()
        raise ProcessTreeCleanupError(
            "Windows process-tree cleanup was not proved by taskkill"
            + (f": {detail}" if detail else "")
        )
    process.wait()


def _resume_suspended_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume a Popen child only after it has entered the owned Job Object."""

    import ctypes
    from ctypes import wintypes

    process_handle = getattr(process, "_handle", None)
    if process_handle is None:
        raise RuntimeError("suspended Windows child has no process handle")
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = int(ntdll.NtResumeProcess(wintypes.HANDLE(int(process_handle))))
    if status != 0:
        raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")


def _spawn_owned_child(child_argv: Sequence[str]) -> _OwnedChild:
    argv = [str(value) for value in child_argv]
    if not argv or not argv[0]:
        raise ValueError("owned app-server child argv is empty")

    if os.name == "posix":
        linux_subreaper = _enable_linux_subreaper()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return _OwnedChild(
            process=process,
            posix_process_group_id=process.pid,
            linux_subreaper=linux_subreaper,
        )

    if os.name == "nt":
        job = _WindowsJob.create()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                # Suspension closes the otherwise unavoidable assignment
                # race: the child cannot spawn a descendant before it belongs
                # to the kill-on-close Job Object.
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | _WINDOWS_CREATE_SUSPENDED
                ),
            )
        except Exception:
            job.close()
            raise
        try:
            job.assign(process)
        except Exception as assign_error:
            pending_error: BaseException | None = assign_error
            while True:
                if pending_error is not None:
                    print(
                        "owned app-server guard could not assign its suspended "
                        f"Windows child; retaining authority: {pending_error}",
                        file=sys.stderr,
                    )
                    pending_error = None
                try:
                    _taskkill_unassigned_windows_tree(process)
                except BaseException as exc:
                    pending_error = exc
                    time.sleep(1.0)
                    continue
                break
            job.close()
            raise ProcessTreeCleanupError(
                f"Windows Job assignment failed: {assign_error}"
            ) from assign_error
        try:
            _resume_suspended_windows_process(process)
        except Exception as resume_error:
            _cleanup_until_proved(
                _OwnedChild(process=process, windows_job=job),
                _DEFAULT_TERMINATE_TIMEOUT_SECONDS,
                initial_error=resume_error,
            )
            job.close()
            raise ProcessTreeCleanupError(
                f"Windows child resume failed: {resume_error}"
            ) from resume_error
        return _OwnedChild(process=process, windows_job=job)

    raise RuntimeError(f"unsupported process-tree platform: {os.name}")


def _posix_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap_adopted_posix_descendants(
    child: subprocess.Popen[bytes],
    process_group_id: int,
) -> None:
    if child.returncode is None:
        return
    while True:
        try:
            reaped_pid, _status = os.waitpid(-process_group_id, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped_pid <= 0:
            return


def _reap_exited_linux_children() -> bool:
    """Reap exited adoptees and report whether any child still exists."""

    while True:
        try:
            reaped_pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False
        if reaped_pid == 0:
            return True


def _linux_direct_child_pids() -> set[int]:
    """Return child PIDs across every guardian thread from procfs."""

    task_dir = pathlib.Path("/proc/self/task")
    try:
        task_paths = tuple(task_dir.iterdir())
    except OSError as exc:
        raise ProcessTreeCleanupError(
            f"cannot enumerate Linux subreaper children: {exc}"
        ) from exc
    children: set[int] = set()
    for task_path in task_paths:
        try:
            raw = (task_path / "children").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProcessTreeCleanupError(
                f"cannot read Linux subreaper children: {exc}"
            ) from exc
        for value in raw.split():
            if value.isdigit() and int(value) > 0:
                children.add(int(value))
    return children


def _signal_linux_adoptees_until_empty(
    signum: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if not _reap_exited_linux_children():
            return True
        for child_pid in _linux_direct_child_pids():
            try:
                os.kill(child_pid, signum)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise ProcessTreeCleanupError(
                    f"cannot signal adopted Linux descendant {child_pid}: {exc}"
                ) from exc
        if time.monotonic() >= deadline:
            return False
        time.sleep(_MONITOR_POLL_SECONDS)


def _terminate_linux_adopted_descendants(
    terminate_timeout_seconds: float,
) -> None:
    if _signal_linux_adoptees_until_empty(
        signal.SIGTERM,
        terminate_timeout_seconds,
    ):
        return
    if not _signal_linux_adoptees_until_empty(
        signal.SIGKILL,
        _KILL_CONFIRM_TIMEOUT_SECONDS,
    ):
        raise ProcessTreeCleanupError(
            "Linux subreaper still owns descendants after SIGKILL"
        )


def _wait_for_posix_group_exit(
    child: subprocess.Popen[bytes],
    process_group_id: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        child.poll()
        _reap_adopted_posix_descendants(child, process_group_id)
        if not _posix_group_exists(process_group_id):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_MONITOR_POLL_SECONDS)


def _terminate_posix_tree(
    owned: _OwnedChild,
    terminate_timeout_seconds: float,
) -> None:
    child = owned.process
    process_group_id = owned.posix_process_group_id
    if process_group_id is None:
        raise ProcessTreeCleanupError("POSIX child has no owned process group")

    if _posix_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not _wait_for_posix_group_exit(
            child,
            process_group_id,
            terminate_timeout_seconds,
        ):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # SIGKILL is not catchable. Waiting without a timeout is deliberate: the
    # guard must reap its direct child before it may release ownership.
    child.wait()
    if not _wait_for_posix_group_exit(
        child,
        process_group_id,
        _KILL_CONFIRM_TIMEOUT_SECONDS,
    ):
        raise ProcessTreeCleanupError(
            f"POSIX process group {process_group_id} still exists after SIGKILL"
        )
    if owned.linux_subreaper:
        _terminate_linux_adopted_descendants(terminate_timeout_seconds)


def _wait_for_windows_job_exit(job: _WindowsJob, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if job.active_processes() == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_MONITOR_POLL_SECONDS)


def _terminate_windows_tree(
    owned: _OwnedChild,
    _terminate_timeout_seconds: float,
) -> None:
    child = owned.process
    job = owned.windows_job
    if job is None:
        raise ProcessTreeCleanupError("Windows child has no owned Job Object")
    if job.active_processes() > 0:
        try:
            job.terminate()
        except OSError:
            if job.active_processes() > 0:
                raise
    child.wait()
    if not _wait_for_windows_job_exit(job, _KILL_CONFIRM_TIMEOUT_SECONDS):
        raise ProcessTreeCleanupError(
            "Windows Job still has active processes after TerminateJobObject"
        )


def _terminate_owned_tree(
    owned: _OwnedChild,
    terminate_timeout_seconds: float,
) -> None:
    if owned.posix_process_group_id is not None:
        _terminate_posix_tree(owned, terminate_timeout_seconds)
        return
    if owned.windows_job is not None:
        _terminate_windows_tree(owned, terminate_timeout_seconds)
        return
    raise ProcessTreeCleanupError("owned child has no process-tree authority")


def _settle_tree_after_natural_child_exit(
    owned: _OwnedChild,
    terminate_timeout_seconds: float,
) -> None:
    if owned.posix_process_group_id is not None:
        if _posix_group_exists(owned.posix_process_group_id):
            _terminate_posix_tree(owned, terminate_timeout_seconds)
        elif owned.linux_subreaper:
            _terminate_linux_adopted_descendants(terminate_timeout_seconds)
        return
    if owned.windows_job is not None:
        if owned.windows_job.active_processes() > 0:
            _terminate_windows_tree(owned, terminate_timeout_seconds)
        return
    raise ProcessTreeCleanupError("owned child has no process-tree authority")


def _child_exit_code(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    # Popen reports POSIX signal death as a negative signal number. A wrapper
    # cannot return a negative wait status, so preserve the shell convention.
    return 128 + abs(returncode)


def _cleanup_until_proved(
    owned: _OwnedChild,
    terminate_timeout_seconds: float,
    *,
    initial_error: BaseException | None = None,
) -> bool:
    """Retain platform containment authority until disappearance is proved.

    Once a child has launched, returning without this proof would let the
    parent clear runtime publication and release instance authority while an
    old tree may still execute. A transient cleanup failure may therefore
    change the eventual exit status, but it can never make the guard exit
    early.
    """

    had_failure = initial_error is not None
    pending_error = initial_error
    while True:
        if pending_error is not None:
            print(
                f"owned app-server guard cleanup attempt failed; retaining authority: {pending_error}",
                file=sys.stderr,
            )
            time.sleep(min(max(terminate_timeout_seconds, 0.1), 1.0))
            pending_error = None
        try:
            _terminate_owned_tree(owned, terminate_timeout_seconds)
        except BaseException as exc:
            had_failure = True
            pending_error = exc
            continue
        return had_failure


def _write_cleanup_receipt(
    receipt_path: pathlib.Path | None,
    cleanup_token: str | None,
) -> None:
    """Durably publish proof that this generation has no remaining tree."""

    if receipt_path is None and cleanup_token is None:
        return
    if receipt_path is None or cleanup_token is None:
        raise ValueError("cleanup receipt path and token must be configured together")
    atomic_write_text(
        receipt_path,
        json.dumps(
            {"cleanup_token": cleanup_token},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n",
        mode=0o600,
    )


def _complete_guard(
    returncode: int,
    *,
    receipt_path: pathlib.Path | None,
    cleanup_token: str | None,
) -> int:
    try:
        _write_cleanup_receipt(receipt_path, cleanup_token)
    except Exception as exc:
        # Absence of a matching receipt deliberately leaves the next service
        # generation fail-closed. The tree is already proved absent here, so
        # exiting cannot release authority accidentally.
        print(
            f"owned app-server guard could not persist cleanup proof: {exc}",
            file=sys.stderr,
        )
        return _GUARD_FAILURE_EXIT_CODE
    return int(returncode)


def run_guard(
    child_argv: Sequence[str],
    *,
    parent_stream: IO[bytes] | None = None,
    terminate_timeout_seconds: float = _DEFAULT_TERMINATE_TIMEOUT_SECONDS,
    cleanup_receipt_path: pathlib.Path | None = None,
    cleanup_token: str | None = None,
) -> int:
    """Run and synchronously own ``child_argv`` until child or owner exit."""

    timeout = float(terminate_timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("terminate_timeout_seconds must be finite and positive")
    normalized_cleanup_token = str(cleanup_token or "").strip() or None
    normalized_receipt_path = (
        pathlib.Path(cleanup_receipt_path)
        if cleanup_receipt_path is not None
        else None
    )
    if (normalized_receipt_path is None) != (normalized_cleanup_token is None):
        raise ValueError("cleanup receipt path and token must be configured together")

    def complete(returncode: int) -> int:
        return _complete_guard(
            returncode,
            receipt_path=normalized_receipt_path,
            cleanup_token=normalized_cleanup_token,
        )

    stream = parent_stream if parent_stream is not None else sys.stdin.buffer
    watcher = _ParentPipeWatcher(stream)
    watcher.start()
    shutdown_signals = _ShutdownSignals()

    with _installed_shutdown_signal_handlers(shutdown_signals):
        # The parent first publishes this guardian's durable runtime record,
        # then writes one activation byte. Until that byte arrives no child is
        # allowed to exist, closing the Popen-before-publication crash window.
        while not watcher.activated.is_set():
            if watcher.parent_lost.is_set() or shutdown_signals.requested.is_set():
                return complete(0)
            watcher.activated.wait(timeout=_MONITOR_POLL_SECONDS)
        if watcher.parent_lost.is_set() or shutdown_signals.requested.is_set():
            return complete(0)
        try:
            owned = _spawn_owned_child(child_argv)
        except (OSError, ValueError) as exc:
            print(f"owned app-server guard could not launch child: {exc}", file=sys.stderr)
            return complete(_CHILD_LAUNCH_FAILURE_EXIT_CODE)
        except Exception as exc:
            print(f"owned app-server guard setup failed: {exc}", file=sys.stderr)
            return complete(_GUARD_FAILURE_EXIT_CODE)

        try:
            while True:
                if watcher.parent_lost.is_set() or shutdown_signals.requested.is_set():
                    cleanup_had_failure = _cleanup_until_proved(owned, timeout)
                    if cleanup_had_failure:
                        return complete(_GUARD_FAILURE_EXIT_CODE)
                    if shutdown_signals.number is not None:
                        return complete(128 + shutdown_signals.number)
                    return complete(0)
                try:
                    returncode = owned.process.wait(timeout=_MONITOR_POLL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
                try:
                    _settle_tree_after_natural_child_exit(owned, timeout)
                except BaseException as exc:
                    _cleanup_until_proved(owned, timeout, initial_error=exc)
                    return complete(_GUARD_FAILURE_EXIT_CODE)
                return complete(_child_exit_code(returncode))
        except BaseException as exc:
            _cleanup_until_proved(owned, timeout, initial_error=exc)
            return complete(_GUARD_FAILURE_EXIT_CODE)
        finally:
            if owned.windows_job is not None:
                # KILL_ON_JOB_CLOSE is a final crash fence. Normal paths have
                # already proved ActiveProcesses == 0 before closing it.
                owned.windows_job.close()


def _positive_timeout(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _parse_args(
    argv: Sequence[str],
) -> tuple[float, pathlib.Path | None, str | None, list[str]]:
    raw = list(argv)
    try:
        separator = raw.index("--")
    except ValueError:
        separator = -1
    parser = argparse.ArgumentParser(
        prog="python -m bot.owned_app_server_guard",
        description=(
            "Guard one Codex app-server lifecycle within the platform's "
            "proved containment set until stdin EOF."
        ),
    )
    parser.add_argument(
        "--terminate-timeout-seconds",
        type=_positive_timeout,
        default=_DEFAULT_TERMINATE_TIMEOUT_SECONDS,
    )
    parser.add_argument("--cleanup-receipt-path", type=pathlib.Path)
    parser.add_argument("--cleanup-token")
    if separator < 0:
        parser.error("expected `--` followed by the Codex child argv")
    options = parser.parse_args(raw[:separator])
    child_argv = raw[separator + 1 :]
    if not child_argv:
        parser.error("Codex child argv is required after `--`")
    if (options.cleanup_receipt_path is None) != (options.cleanup_token is None):
        parser.error("--cleanup-receipt-path and --cleanup-token must be used together")
    if options.cleanup_token is not None and not str(options.cleanup_token).strip():
        parser.error("--cleanup-token must not be empty")
    return (
        float(options.terminate_timeout_seconds),
        options.cleanup_receipt_path,
        str(options.cleanup_token).strip() if options.cleanup_token is not None else None,
        child_argv,
    )


def main(argv: Sequence[str] | None = None) -> int:
    timeout, receipt_path, cleanup_token, child_argv = _parse_args(
        sys.argv[1:] if argv is None else argv
    )
    return run_guard(
        child_argv,
        terminate_timeout_seconds=timeout,
        cleanup_receipt_path=receipt_path,
        cleanup_token=cleanup_token,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess tests
    raise SystemExit(main())
