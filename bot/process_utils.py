"""
Process existence helpers.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _linux_process_identity(pid: int) -> str:
    """Return a boot-scoped Linux process incarnation identifier.

    A PID only names a slot in the process table and can be reused.  Linux's
    ``/proc/<pid>/stat`` start-time tick is stable for one incarnation; the
    boot id keeps that tick from being mistaken for the same process after a
    reboot.
    """

    try:
        boot_id = (
            pathlib.Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
        stat = (pathlib.Path("/proc") / str(pid) / "stat").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""
    # The comm field is parenthesized and may itself contain whitespace or a
    # closing parenthesis.  Splitting at the final ``)`` leaves field 3
    # (state) at index 0 and field 22 (starttime) at index 19.
    _, separator, suffix = stat.rpartition(")")
    if not boot_id or not separator:
        return ""
    fields = suffix.strip().split()
    if len(fields) <= 19:
        return ""
    start_ticks = str(fields[19] or "").strip()
    if not start_ticks.isdigit():
        return ""
    return f"linux:{boot_id}:{start_ticks}"


def _windows_process_identity(pid: int) -> str:
    """Return the Windows process creation FILETIME for one PID."""

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_uint32),
            ("dwHighDateTime", ctypes.c_uint32),
        ]

    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    get_process_times = kernel32.GetProcessTimes
    get_process_times.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    get_process_times.restype = ctypes.c_int
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    creation = _FileTime()
    exit_time = _FileTime()
    kernel_time = _FileTime()
    user_time = _FileTime()
    try:
        if not get_process_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return ""
    finally:
        close_handle(handle)
    created_at = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    return f"windows:{created_at}" if created_at > 0 else ""


def _posix_process_identity(pid: int) -> str:
    """Return a locale-stable process start value on non-/proc POSIX hosts."""
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            text=True,
            capture_output=True,
            env=environment,
        )
    except OSError:
        return ""
    started_at = " ".join(result.stdout.strip().split())
    if result.returncode != 0 or not started_at:
        return ""
    return f"posix:{started_at}"


def process_identity(pid: int) -> str:
    """Return an opaque identifier for the current incarnation of ``pid``.

    An empty result means the platform cannot prove the incarnation.  Store
    cleanup must treat that as unknown and retain the record (fail closed),
    rather than falling back to PID-only identity.
    """

    normalized_pid = int(pid or 0)
    if normalized_pid <= 0:
        return ""
    if os.name == "nt":
        return _windows_process_identity(normalized_pid)
    if pathlib.Path("/proc").is_dir():
        return _linux_process_identity(normalized_pid)
    if os.name == "posix":
        return _posix_process_identity(normalized_pid)
    return ""


def _linux_process_state(pid: int) -> str:
    status_path = pathlib.Path("/proc") / str(pid) / "status"
    try:
        with status_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                if raw_line.startswith("State:"):
                    parts = raw_line.split()
                    if len(parts) >= 2:
                        return str(parts[1]).strip().upper()
                    return ""
    except OSError:
        return ""
    return ""


def process_exists(pid: int) -> bool:
    normalized_pid = int(pid or 0)
    if normalized_pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(normalized_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        if _linux_process_state(normalized_pid) == "Z":
            return False
        return True
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, normalized_pid)
    if not handle:
        return False
    close_handle(handle)
    return True
