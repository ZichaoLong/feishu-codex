"""Small cross-process primitives for local atomic files and secrets."""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile
from collections.abc import Callable

from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.file_permissions import ensure_private_file_permissions


def _fsync_directory(path: pathlib.Path) -> None:
    """Persist a replaced directory entry on POSIX filesystems."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_text(
    path: pathlib.Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Replace ``path`` with a fully written same-directory temporary file."""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    temporary_path = pathlib.Path(raw_temporary_path)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            if mode == 0o600:
                ensure_private_file_permissions(temporary_path)
            else:
                os.chmod(temporary_path, mode)
        os.replace(temporary_path, target)
        # fsyncing the temporary file protects its contents; the parent
        # directory sync is the separate durability boundary for the rename.
        # Authority stores must not report a successful commit which can
        # disappear after a host crash.
        _fsync_directory(target.parent)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def ensure_private_token(
    path: pathlib.Path | str,
    generate: Callable[[], str],
) -> str:
    """Return one stable token, serialized across threads and processes."""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    with open_lock_file(lock_path) as lock_file:
        acquire_file_lock(lock_file, blocking=True)
        try:
            token = _read_existing_token(target)
            if token:
                ensure_private_file_permissions(target)
                return token
            token = str(generate() or "").strip()
            if not token:
                raise ValueError("token generator returned an empty token")
            atomic_write_text(target, f"{token}\n", mode=0o600)
            return token
        finally:
            release_file_lock(lock_file)


def _read_existing_token(target: pathlib.Path) -> str:
    """Read an existing token through a no-follow, identity-checked handle."""

    try:
        before = target.lstat()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ValueError(f"token path cannot be inspected safely: {target}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"token path must be a regular file: {target}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    file_descriptor = -1
    try:
        file_descriptor = os.open(target, flags)
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValueError(f"token path changed while opening: {target}")
        with os.fdopen(file_descriptor, "r", encoding="utf-8") as handle:
            file_descriptor = -1
            token = handle.read().strip()
            if token:
                if os.name == "nt":
                    ensure_private_file_permissions(target)
                else:
                    os.fchmod(handle.fileno(), 0o600)
        after = target.lstat()
        if (
            stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(f"token path changed while reading: {target}")
        return token
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"token path cannot be read safely: {target}") from exc
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
