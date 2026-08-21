"""
Cross-platform advisory file locks.
"""

from __future__ import annotations

import os
import pathlib
import stat
from typing import TextIO


class FileLockBusyError(BlockingIOError):
    """Raised when a non-blocking file lock cannot be acquired."""


def _file_path(file_obj) -> pathlib.Path | None:
    raw_name = getattr(file_obj, "name", None)
    if isinstance(raw_name, (str, bytes, os.PathLike)):
        return pathlib.Path(raw_name)
    return None


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _validate_open_file(file_obj, *, path: pathlib.Path | None = None) -> None:
    """Reject non-regular or path-replaced descriptors before locking.

    A caller may open a lock with the platform's normal path API before it
    reaches this module.  Re-checking both the descriptor and its directory
    entry prevents a symlink (or an observed path replacement) from becoming
    the authority file.  ``open_lock_file`` additionally uses ``O_NOFOLLOW``
    to close the check-before-open window for Focus-owned lock paths.
    """

    metadata = os.fstat(file_obj.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("lock file must be a regular file")
    target = path if path is not None else _file_path(file_obj)
    if target is None:
        return
    try:
        path_metadata = target.lstat()
    except OSError as exc:
        raise OSError(f"lock file path cannot be verified: {target}") from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise OSError(f"lock file path must be a regular file: {target}")
    if _identity(path_metadata) != _identity(metadata):
        raise OSError(f"lock file path changed while opening: {target}")


def open_lock_file(path: pathlib.Path | str) -> TextIO:
    """Open a private lock file without following a final symlink.

    The returned text handle is intentionally ordinary so existing callers can
    use ``with`` or retain it for the lifetime of a lease.  Creation and the
    descriptor/path identity checks happen before the handle is returned;
    ``acquire_file_lock`` repeats the checks around the platform lock call for
    callers that still provide a handle opened elsewhere.
    """

    target = pathlib.Path(path)
    try:
        before = target.lstat()
    except FileNotFoundError:
        before = None
    except OSError as exc:
        raise OSError(f"lock file path cannot be inspected: {target}") from exc
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise OSError(f"lock file path must be a regular file: {target}")

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    file_descriptor = -1
    try:
        file_descriptor = os.open(target, flags, 0o600)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"lock file must be a regular file: {target}")
        after = target.lstat()
        if not stat.S_ISREG(after.st_mode) or _identity(after) != _identity(metadata):
            raise OSError(f"lock file path changed while opening: {target}")
        if before is not None and _identity(before) != _identity(metadata):
            raise OSError(f"lock file path changed while opening: {target}")
        handle = os.fdopen(file_descriptor, "a+", encoding="utf-8")
        file_descriptor = -1
        return handle
    except BaseException:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        raise


def _ensure_lock_file(file_obj) -> None:
    file_obj.seek(0, os.SEEK_END)
    if file_obj.tell() == 0:
        file_obj.write("\0")
        file_obj.flush()
    file_obj.seek(0)


if os.name == "nt":
    import msvcrt

    def acquire_file_lock(file_obj, *, blocking: bool) -> None:
        _validate_open_file(file_obj)
        _ensure_lock_file(file_obj)
        _validate_open_file(file_obj)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            file_obj.seek(0)
            msvcrt.locking(file_obj.fileno(), mode, 1)
        except OSError as exc:
            raise FileLockBusyError(str(exc)) from exc
        _validate_open_file(file_obj)

    def release_file_lock(file_obj) -> None:
        _ensure_lock_file(file_obj)
        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def acquire_file_lock(file_obj, *, blocking: bool) -> None:
        _validate_open_file(file_obj)
        _ensure_lock_file(file_obj)
        _validate_open_file(file_obj)
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(file_obj.fileno(), flags)
        except BlockingIOError as exc:
            raise FileLockBusyError(str(exc)) from exc
        _validate_open_file(file_obj)

    def release_file_lock(file_obj) -> None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
