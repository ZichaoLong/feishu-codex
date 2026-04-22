"""
Process existence helpers.
"""

from __future__ import annotations

import ctypes
import os

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, normalized_pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True
