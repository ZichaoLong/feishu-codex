"""Owned Codex app-server guardian process lifecycle."""

from __future__ import annotations

import logging
import os
import pathlib
import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from bot.codex_command_resolver import resolve_managed_codex_command
from bot.codex_protocol.managed_endpoint import (
    ManagedAppServerEndpointAllocator,
    log_managed_stream,
)
from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.instance_layout import global_data_dir
from bot.local_websocket_auth import AppServerWebsocketAuthTokenStore
from bot.stores.app_server_runtime_store import (
    OWNED_PROCESS_KIND_GUARDIAN,
    AppServerRuntimeStore,
    uses_default_app_server_url,
)

logger = logging.getLogger(__name__)

_MANAGED_APP_SERVER_START_LOCK = "codex-app-server-start.lock"
_MANAGED_APP_SERVER_VERIFY_GRACE_SECONDS = 0.5
_MANAGED_DEFAULT_START_MAX_ATTEMPTS = 3
_MANAGED_PROCESS_TERMINATE_GRACE_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class ManagedProcessStopResult:
    pending_resources: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.pending_resources and not self.failures


class ManagedAppServerProcess:
    """Own one guardian process generation and its durable publication."""

    def __init__(
        self,
        *,
        codex_command: str,
        configured_url: str,
        runtime_store: AppServerRuntimeStore | None,
        startup_lock_path: pathlib.Path | str | None,
        websocket_auth_store: AppServerWebsocketAuthTokenStore | None,
    ) -> None:
        self._codex_command = str(codex_command or "").strip()
        self._configured_url = str(configured_url or "").strip()
        self._runtime_store = runtime_store
        self._startup_lock_path = (
            pathlib.Path(startup_lock_path)
            if startup_lock_path is not None
            else None
        )
        self._websocket_auth_store = websocket_auth_store
        self._endpoint_allocator = ManagedAppServerEndpointAllocator(
            self._configured_url
        )
        self._active_url = self._configured_url
        self._process: subprocess.Popen[str] | None = None
        self._stream_threads: set[threading.Thread] = set()
        self._cleanup_token: str | None = None
        self._process_terminate_sent = False
        self._process_kill_sent = False
        self._process_reaped = True
        self._runtime_state_cleared = True

    @property
    def configured_url(self) -> str:
        return self._configured_url

    @property
    def active_url(self) -> str:
        return self._active_url

    def max_start_attempts(self) -> int:
        if uses_default_app_server_url(self._configured_url):
            return _MANAGED_DEFAULT_START_MAX_ATTEMPTS
        return 1

    def select_endpoint(self) -> str:
        return self._endpoint_allocator.select()

    def allocate_retry_endpoint(self) -> str:
        return self._endpoint_allocator.allocate(self._configured_url)

    @contextmanager
    def startup_lock(self):
        lock_path = self._startup_lock_path
        if lock_path is None:
            lock_path = global_data_dir() / _MANAGED_APP_SERVER_START_LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open_lock_file(lock_path) as handle:
            acquire_file_lock(handle, blocking=True)
            try:
                yield
            finally:
                release_file_lock(handle)

    def prepare_for_start(self) -> bool:
        """Return whether a live generation can be reused.

        A dead guardian may be discarded only after its durable cleanup receipt
        allows the matching runtime publication to be cleared.
        """

        process = self._process
        if process is None:
            if self.has_active_resources():
                raise RuntimeError(
                    "managed app-server cleanup must complete before start"
                )
            return False
        if process.poll() is None:
            return True
        self._process_reaped = True
        self._clear_runtime_state()
        self._process = None
        self._stream_threads = {
            thread for thread in self._stream_threads if self._thread_is_alive(thread)
        }
        self._reset_stop_flags()
        return False

    def launch(self, listen_url: str) -> None:
        if self.has_active_resources():
            raise RuntimeError(
                "managed app-server generation already owns process resources"
            )
        self._active_url = str(listen_url or "").strip()
        if not self._active_url:
            raise ValueError("managed app-server listen URL cannot be empty")
        self._reset_stop_flags()
        self._process_reaped = False
        self._runtime_state_cleared = self._runtime_store is None
        guardian_options: list[str] = []
        if self._runtime_store is not None:
            self._cleanup_token = (
                self._runtime_store.begin_guardian_generation()
            )
            cleanup_receipt_path = self._runtime_store.cleanup_receipt_path(
                self._cleanup_token
            )
            guardian_options = [
                f"--cleanup-receipt-path={cleanup_receipt_path}",
                f"--cleanup-token={self._cleanup_token}",
            ]
        effective_command = resolve_managed_codex_command(self._codex_command)
        if effective_command != self._codex_command:
            logger.info(
                "默认 codex 命令不可用，回退到稳定启动命令: %s",
                effective_command,
            )
        app_server_cmd = [
            *shlex.split(effective_command),
            "app-server",
            "--listen",
            self._active_url,
            "--ws-auth",
            "capability-token",
            "--ws-token-file",
            str(self._managed_ws_auth_token_file()),
        ]
        command = [
            sys.executable,
            "-m",
            "bot.owned_app_server_guard",
            *guardian_options,
            "--",
            *app_server_cmd,
        ]
        logger.info("启动受监护的 Codex app-server: %s", app_server_cmd)
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self._process_env(),
        )
        self.publish_runtime()
        guardian_stdin = self._process.stdin
        if guardian_stdin is None:
            raise RuntimeError(
                "owned app-server guardian activation pipe is unavailable"
            )
        guardian_stdin.write("1\n")
        guardian_stdin.flush()
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stream_threads = {
            thread for thread in self._stream_threads if self._thread_is_alive(thread)
        }
        for stream, level, name in (
            (self._process.stdout, logging.DEBUG, "stdout"),
            (self._process.stderr, logging.INFO, "stderr"),
        ):
            stream_thread = threading.Thread(
                target=log_managed_stream,
                args=(stream, level, name),
                name=f"focus-codex-{name}",
                daemon=True,
            )
            stream_thread.start()
            self._stream_threads.add(stream_thread)

    def verify_alive(self) -> None:
        process = self._process
        if process is None:
            raise RuntimeError("managed codex app-server process is unavailable")
        deadline = time.time() + _MANAGED_APP_SERVER_VERIFY_GRACE_SECONDS
        while True:
            if process.poll() is not None:
                raise RuntimeError("codex app-server exited after websocket connected")
            if time.time() >= deadline:
                return
            time.sleep(0.05)

    def publish_runtime(self) -> None:
        if self._runtime_store is None:
            self._runtime_state_cleared = True
            return
        process = self._process
        if process is None or not getattr(process, "pid", None):
            raise RuntimeError("owned app-server guardian pid is unavailable")
        if not self._cleanup_token:
            raise RuntimeError(
                "owned app-server guardian cleanup token is unavailable"
            )
        self._runtime_store.save_owned_runtime(
            configured_url=self._configured_url,
            active_url=self._active_url,
            owner_pid=os.getpid(),
            lifecycle_pid=int(process.pid),
            lifecycle_kind=OWNED_PROCESS_KIND_GUARDIAN,
            cleanup_token=self._cleanup_token,
        )
        self._runtime_state_cleared = False

    def is_running(self) -> bool:
        process = self._process
        return bool(process is not None and process.poll() is None)

    def has_exited(self) -> bool:
        process = self._process
        return bool(process is not None and process.poll() is not None)

    def has_active_resources(self) -> bool:
        return bool(
            self._process is not None
            or self._cleanup_token
            or not self._process_reaped
            or not self._runtime_state_cleared
            or any(self._thread_is_alive(thread) for thread in self._stream_threads)
        )

    def request_stop(self) -> tuple[str, ...]:
        failures: list[str] = []
        process = self._process
        if process is None:
            self._process_reaped = True
            return ()
        try:
            return_code = process.poll()
        except Exception as exc:
            return (f"managed process status check failed: {exc}",)
        if return_code is not None:
            self._process_reaped = True
            return ()
        if self._process_terminate_sent or self._process_kill_sent:
            return ()
        guardian_stdin = getattr(process, "stdin", None)
        if guardian_stdin is not None:
            try:
                guardian_stdin.close()
            except Exception as exc:
                failures.append(
                    f"owned process guardian shutdown request failed: {exc}"
                )
            else:
                self._process_terminate_sent = True
            return tuple(failures)
        try:
            process.terminate()
        except Exception as exc:
            failures.append(f"managed process terminate failed: {exc}")
        else:
            self._process_terminate_sent = True
        return tuple(failures)

    def drain_stop(self, *, deadline_monotonic: float) -> ManagedProcessStopResult:
        failures: list[str] = []
        self._drain_process(
            deadline_monotonic=deadline_monotonic,
            failures=failures,
        )
        if self._process_reaped:
            self._join_stream_threads(
                deadline_monotonic=deadline_monotonic,
                failures=failures,
            )
        if self._process_reaped and not self._runtime_state_cleared:
            try:
                self._clear_runtime_state()
            except Exception as exc:
                failures.append(f"owned runtime state cleanup failed: {exc}")
        pending_resources = self.pending_resource_names()
        result = ManagedProcessStopResult(
            pending_resources=pending_resources,
            failures=tuple(failures),
        )
        if result.complete:
            self._process = None
            self._stream_threads.clear()
            self._active_url = self._configured_url
            self._reset_stop_flags()
        return result

    def pending_resource_names(self) -> tuple[str, ...]:
        pending: list[str] = []
        if not self._process_reaped:
            pending.append("managed process")
        pending.extend(
            f"managed stream thread {getattr(thread, 'name', 'unnamed')}"
            for thread in self._stream_threads
            if self._thread_is_alive(thread)
        )
        if self._process_reaped and not self._runtime_state_cleared:
            pending.append("owned runtime state")
        return tuple(pending)

    def owns_thread(self, thread: threading.Thread) -> bool:
        return thread in self._stream_threads

    def _drain_process(
        self,
        *,
        deadline_monotonic: float,
        failures: list[str],
    ) -> None:
        process = self._process
        if process is None or self._process_reaped:
            self._process_reaped = True
            return
        if not (self._process_terminate_sent or self._process_kill_sent):
            return
        remaining = max(0.0, deadline_monotonic - time.monotonic())
        if self._process_kill_sent:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                failures.append("managed process wait after kill timed out")
            except Exception as exc:
                failures.append(f"managed process wait after kill failed: {exc}")
            else:
                self._process_reaped = True
            return
        guardian_stdin = getattr(process, "stdin", None)
        terminate_wait = (
            remaining
            if guardian_stdin is not None
            else min(
                _MANAGED_PROCESS_TERMINATE_GRACE_SECONDS,
                remaining / 2.0,
            )
        )
        try:
            process.wait(timeout=terminate_wait)
        except subprocess.TimeoutExpired:
            if guardian_stdin is not None:
                failures.append("owned process guardian shutdown timed out")
                return
            try:
                process.kill()
            except Exception as exc:
                failures.append(f"managed process kill failed: {exc}")
                return
            self._process_kill_sent = True
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                failures.append("managed process wait after kill timed out")
            except Exception as exc:
                failures.append(f"managed process wait after kill failed: {exc}")
            else:
                self._process_reaped = True
        except Exception as exc:
            failures.append(f"managed process wait after terminate failed: {exc}")
        else:
            self._process_reaped = True

    def _join_stream_threads(
        self,
        *,
        deadline_monotonic: float,
        failures: list[str],
    ) -> None:
        current_thread = threading.current_thread()
        for thread in self._stream_threads:
            if not self._thread_is_alive(thread):
                continue
            name = getattr(thread, "name", None) or "managed stream"
            if thread is current_thread:
                failures.append(
                    f"cannot join current managed stream thread {name}"
                )
                continue
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            try:
                thread.join(timeout=remaining)
            except Exception as exc:
                failures.append(f"managed stream thread {name} join failed: {exc}")
                continue
            if self._thread_is_alive(thread):
                failures.append(f"managed stream thread {name} join timed out")

    def _clear_runtime_state(self) -> None:
        if self._runtime_store is not None:
            self._runtime_store.clear_owned_runtime(
                owner_pid=os.getpid(),
                cleanup_token=self._cleanup_token,
            )
        self._cleanup_token = None
        self._runtime_state_cleared = True

    def _managed_ws_auth_token_file(self) -> pathlib.Path:
        if self._websocket_auth_store is None:
            raise RuntimeError(
                "owned app-server websocket auth requires app_server_data_dir"
            )
        self._websocket_auth_store.ensure()
        return self._websocket_auth_store.path

    @staticmethod
    def _process_env() -> dict[str, str]:
        return os.environ.copy()

    @staticmethod
    def _thread_is_alive(thread: threading.Thread) -> bool:
        is_alive = getattr(thread, "is_alive", None)
        if not callable(is_alive):
            return False
        return bool(is_alive())

    def _reset_stop_flags(self) -> None:
        self._process_terminate_sent = False
        self._process_kill_sent = False
        self._process_reaped = self._process is None
