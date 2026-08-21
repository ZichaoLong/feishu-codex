"""Exclusive lifecycle transaction for the repo-managed installation surface.

The transaction owns only local installer coordination.  Runtime idleness is
proved by the running service, service execution is owned by ``ServiceManager``,
and per-instance offline ownership remains in ``ServiceInstanceMaintenanceLease``.
"""

from __future__ import annotations

import pathlib
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from bot.file_lock import (
    FileLockBusyError,
    acquire_file_lock,
    open_lock_file,
    release_file_lock,
)
from bot.service_manager import ServiceStatus
from bot.service_control_plane import ServiceControlOutcomeUnknownError


class ManagedInstallLifecycleError(RuntimeError):
    """The managed installation could not be changed with proved ownership."""


class MaintenanceLease(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ManagedInstallLifecyclePorts:
    """Required capabilities supplied by the install composition root."""

    service_status: Callable[[str], ServiceStatus]
    prepare_offline_maintenance: Callable[[str], Mapping[str, Any]]
    cancel_offline_maintenance: Callable[[str], None]
    stop_service: Callable[[str], None]
    start_service: Callable[[str], None]
    maintenance_lease: Callable[[str], MaintenanceLease]


@dataclass(frozen=True, slots=True)
class ManagedRemovalTarget:
    role: str
    path: pathlib.Path


def remove_managed_trees(
    targets: tuple[ManagedRemovalTarget, ...],
    *,
    operation: str,
) -> None:
    """Remove exact managed trees with explicit partial-result reporting."""

    removed: list[ManagedRemovalTarget] = []
    for target in targets:
        path = pathlib.Path(target.path)
        if path.is_symlink():
            raise ManagedInstallLifecycleError(
                f"{operation} 拒绝删除符号链接 target：{target.role}={path}"
            )
        if not path.exists():
            continue
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            removed_summary = ", ".join(
                f"{item.role}={item.path}" for item in removed
            ) or "无"
            raise ManagedInstallLifecycleError(
                f"{operation} 未完成：删除 {target.role} 失败：{path}: {exc}。"
                f"本次已删除 target：{removed_summary}；不会报告成功。"
            ) from exc
        removed.append(target)


def managed_install_lock_path(data_root: pathlib.Path) -> pathlib.Path:
    """Keep the install lock outside the data root that purge may remove."""

    normalized = pathlib.Path(data_root).expanduser()
    leaf = normalized.name.strip()
    if not leaf:
        raise ManagedInstallLifecycleError(
            f"无法为过宽的 FOCUS data root 建立安装互斥锁：{normalized}"
        )
    return normalized.parent / f".{leaf}.managed-install.lock"


def managed_install_handoff_lock_path(lock_path: pathlib.Path) -> pathlib.Path:
    """Return the barrier used to transfer deletion ownership on Windows."""

    normalized = pathlib.Path(lock_path)
    return normalized.with_name(f"{normalized.name}.handoff")


class ManagedInstallLock:
    """Non-blocking machine owner shared by install/uninstall/purge.

    The primary lock serializes lifecycle admission.  A second barrier normally
    travels with it, but can be yielded to an accepted Windows deletion helper
    while the parent still holds the primary lock.  That closes the otherwise
    unavoidable process-exit race without introducing durable install state.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self.handoff_path = managed_install_handoff_lock_path(self.path)
        self._primary_handle = None
        self._handoff_handle = None

    def acquire(self) -> None:
        if self._primary_handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        primary_handle = open_lock_file(self.path)
        try:
            acquire_file_lock(primary_handle, blocking=False)
        except FileLockBusyError as exc:
            primary_handle.close()
            raise ManagedInstallLifecycleError(
                "已有 install、uninstall 或 purge 正在修改本机 FOCUS 安装面；"
                "请等待其结束后重试。"
            ) from exc
        except BaseException:
            primary_handle.close()
            raise
        handoff_handle = None
        try:
            handoff_handle = open_lock_file(self.handoff_path)
            acquire_file_lock(handoff_handle, blocking=False)
        except FileLockBusyError as exc:
            handoff_handle.close()
            release_file_lock(primary_handle)
            primary_handle.close()
            raise ManagedInstallLifecycleError(
                "已有 Windows 删除 helper 正在收口本机 FOCUS 安装面；"
                "请查看先前命令给出的 result 文件，等待其结束后重试。"
            ) from exc
        except BaseException:
            if handoff_handle is not None:
                handoff_handle.close()
            release_file_lock(primary_handle)
            primary_handle.close()
            raise
        self._primary_handle = primary_handle
        self._handoff_handle = handoff_handle

    def yield_handoff_barrier(self) -> None:
        """Yield only the deletion barrier while retaining lifecycle admission."""

        if self._primary_handle is None or self._handoff_handle is None:
            raise ManagedInstallLifecycleError(
                "managed-install lock 当前没有可移交的 Windows handoff barrier"
            )
        handoff_handle = self._handoff_handle
        self._handoff_handle = None
        try:
            release_file_lock(handoff_handle)
        finally:
            handoff_handle.close()

    def release(self) -> None:
        handoff_handle = self._handoff_handle
        primary_handle = self._primary_handle
        self._handoff_handle = None
        self._primary_handle = None
        try:
            if handoff_handle is not None:
                try:
                    release_file_lock(handoff_handle)
                finally:
                    handoff_handle.close()
        finally:
            if primary_handle is not None:
                try:
                    release_file_lock(primary_handle)
                finally:
                    primary_handle.close()

    def __enter__(self) -> ManagedInstallLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.release()


class ManagedInstallTransaction:
    """Stop only proved-idle services around one installation mutation.

    The transaction deliberately has no rollback generation.  A failed body
    releases its offline leases and leaves every service it stopped offline.
    Only a successful body restores the exact originally-running set.
    """

    def __init__(
        self,
        *,
        operation: str,
        instance_names: Iterable[str],
        lock: ManagedInstallLock,
        ports: ManagedInstallLifecyclePorts,
        restore_running_on_success: bool = True,
        status_timeout_seconds: float = 10.0,
        status_poll_seconds: float = 0.1,
    ) -> None:
        normalized_operation = str(operation or "").strip()
        if not normalized_operation:
            raise ValueError("managed install transaction requires an operation")
        names = tuple(dict.fromkeys(str(name or "").strip() for name in instance_names))
        if not names or any(not name for name in names):
            raise ValueError("managed install transaction requires explicit instance names")
        if type(ports) is not ManagedInstallLifecyclePorts:
            raise TypeError("managed install transaction requires typed ports")
        self.operation = normalized_operation
        self.instance_names = names
        self._lock = lock
        self._ports = ports
        self._restore_running_on_success = bool(restore_running_on_success)
        self._status_timeout_seconds = max(float(status_timeout_seconds), 0.0)
        self._status_poll_seconds = max(float(status_poll_seconds), 0.0)
        self._state = "new"
        self._originally_running: tuple[str, ...] = ()
        self._prepared_instances: list[str] = []
        self._maintenance_leases: list[tuple[str, MaintenanceLease]] = []
        self._restored_instances: tuple[str, ...] = ()
        self._handoff_barrier_yielded = False

    @property
    def originally_running_instances(self) -> tuple[str, ...]:
        return self._originally_running

    @property
    def restored_instances(self) -> tuple[str, ...]:
        return self._restored_instances

    def prepare(self) -> None:
        if self._state != "new":
            raise ManagedInstallLifecycleError(
                f"{self.operation} transaction cannot prepare from {self._state}"
            )
        self._lock.acquire()
        stop_phase_started = False
        try:
            statuses = {
                instance_name: self._checked_status(instance_name)
                for instance_name in self.instance_names
            }
            self._originally_running = tuple(
                name for name in self.instance_names if statuses[name].running
            )
            for instance_name in self._originally_running:
                self._prepare_instance(instance_name)

            stop_phase_started = bool(self._originally_running)
            for instance_name in self._originally_running:
                self._ports.stop_service(instance_name)
            self._wait_for_running_state(
                self._originally_running,
                expected_running=False,
                stage="停服",
            )

            for instance_name in self.instance_names:
                lease = self._ports.maintenance_lease(instance_name)
                lease.acquire()
                self._maintenance_leases.append((instance_name, lease))
            self._state = "prepared"
        except BaseException as exc:
            cleanup_failures: list[str] = []
            self._release_maintenance_leases(cleanup_failures)
            if not stop_phase_started:
                self._cancel_prepared_instances(cleanup_failures)
            self._release_lock(cleanup_failures)
            self._state = "closed"
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                if cleanup_failures:
                    exc.add_note("; ".join(cleanup_failures))
                raise
            detail = str(exc).strip() or exc.__class__.__name__
            if stop_phase_started:
                message = (
                    f"{self.operation} 未开始修改安装面，但停服阶段未能完整收口：{detail}。"
                    "为避免把未知状态伪装成恢复成功，不会自动重启已进入停服阶段的实例；"
                    "请检查 service 状态后重试。"
                )
            else:
                message = (
                    f"{self.operation} 在修改安装面前被拒绝：{detail}。"
                    "若运行中实例仍是旧版本、不认识 maintenance 控制方法，请先手工停止所有实例，"
                    "再重新运行安装脚本。"
                )
            if cleanup_failures:
                message += " 清理警告：" + "; ".join(cleanup_failures)
            raise ManagedInstallLifecycleError(message) from exc

    def complete(self) -> None:
        if self._state != "prepared":
            raise ManagedInstallLifecycleError(
                f"{self.operation} transaction cannot complete from {self._state}"
            )
        failures: list[str] = []
        self._release_maintenance_leases(failures)
        restored: tuple[str, ...] = ()
        try:
            if not failures and self._restore_running_on_success:
                start_failures: list[str] = []
                for instance_name in self._originally_running:
                    try:
                        self._ports.start_service(instance_name)
                    except Exception as exc:
                        start_failures.append(f"{instance_name}: {exc}")
                try:
                    self._wait_for_running_state(
                        self._originally_running,
                        expected_running=True,
                        stage="恢复",
                    )
                except Exception as exc:
                    failures.append(str(exc))
                else:
                    # A status proof supersedes a noisy start command result.
                    restored = self._originally_running
                if not restored and start_failures:
                    failures.append("service start 失败：" + "; ".join(start_failures))
        finally:
            self._release_lock(failures)
            self._state = "closed"
            self._restored_instances = restored
        if failures:
            raise ManagedInstallLifecycleError(
                f"{self.operation} 安装面已修改，但服务恢复未能证明完成："
                + "; ".join(failures)
                + "。实例保持当前状态；请检查后手工启动。"
            )

    def abort(self) -> None:
        if self._state == "closed":
            return
        failures: list[str] = []
        self._release_maintenance_leases(failures)
        self._release_lock(failures)
        self._state = "closed"
        if failures:
            raise ManagedInstallLifecycleError(
                f"{self.operation} 失败后的 ownership 清理未完成："
                + "; ".join(failures)
            )

    def yield_handoff_barrier(self) -> None:
        """Let an out-of-process remover take deletion ownership before exit."""

        if self._state != "prepared" or self._restore_running_on_success:
            raise ManagedInstallLifecycleError(
                f"{self.operation} transaction 当前不能移交 Windows 删除 ownership"
            )
        if self._handoff_barrier_yielded:
            raise ManagedInstallLifecycleError(
                f"{self.operation} transaction 已经移交 Windows 删除 ownership"
            )
        self._lock.yield_handoff_barrier()
        self._handoff_barrier_yielded = True

    def __enter__(self) -> ManagedInstallTransaction:
        self.prepare()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc_type, traceback
        if exc is None:
            self.complete()
            return False
        try:
            self.abort()
        except Exception as cleanup_error:
            if isinstance(exc, BaseException):
                exc.add_note(str(cleanup_error))
        return False

    def _prepare_instance(self, instance_name: str) -> None:
        try:
            result = self._ports.prepare_offline_maintenance(instance_name)
        except ServiceControlOutcomeUnknownError:
            # The request may have closed ingress even though its response was
            # lost.  A best-effort exact cancel is the only safe pre-stop
            # cleanup; never proceed to service or environment mutation.
            self._prepared_instances.append(instance_name)
            raise
        if not isinstance(result, Mapping):
            self._prepared_instances.append(instance_name)
            raise ManagedInstallLifecycleError(
                f"实例 {instance_name} 返回了无效 maintenance admission 结果"
            )
        result_instance = str(result.get("instance_name", "") or "").strip()
        status = str(result.get("status", "") or "").strip()
        if result_instance != instance_name or status != "prepared":
            self._prepared_instances.append(instance_name)
            raise ManagedInstallLifecycleError(
                f"实例 {instance_name} 未返回 matching prepared proof："
                f"instance={result_instance or '<empty>'} status={status or '<empty>'}"
            )
        self._prepared_instances.append(instance_name)

    def _cancel_prepared_instances(self, failures: list[str]) -> None:
        for instance_name in reversed(self._prepared_instances):
            try:
                self._ports.cancel_offline_maintenance(instance_name)
            except Exception as exc:
                failures.append(f"取消 {instance_name} maintenance 失败：{exc}")
        self._prepared_instances.clear()

    def _checked_status(self, instance_name: str) -> ServiceStatus:
        status = self._ports.service_status(instance_name)
        if not isinstance(status, ServiceStatus):
            raise ManagedInstallLifecycleError(
                f"实例 {instance_name} 返回了无效 service status"
            )
        if type(status.installed) is not bool or type(status.running) is not bool:
            raise ManagedInstallLifecycleError(
                f"实例 {instance_name} 的 service status 缺少明确布尔状态"
            )
        return status

    def _wait_for_running_state(
        self,
        instance_names: tuple[str, ...],
        *,
        expected_running: bool,
        stage: str,
    ) -> None:
        if not instance_names:
            return
        deadline = time.monotonic() + self._status_timeout_seconds
        last_pending: list[str] = []
        while True:
            pending: list[str] = []
            for instance_name in instance_names:
                status = self._checked_status(instance_name)
                if status.running is not expected_running:
                    detail = str(status.detail or "").strip()
                    pending.append(
                        instance_name + (f" ({detail})" if detail else "")
                    )
            if not pending:
                return
            last_pending = pending
            if time.monotonic() >= deadline:
                expected = "running" if expected_running else "stopped"
                raise ManagedInstallLifecycleError(
                    f"{stage}后无法证明实例已进入 {expected}：{', '.join(last_pending)}"
                )
            time.sleep(self._status_poll_seconds)

    def _release_maintenance_leases(self, failures: list[str]) -> None:
        while self._maintenance_leases:
            instance_name, lease = self._maintenance_leases.pop()
            try:
                lease.release()
            except Exception as exc:
                failures.append(f"释放 {instance_name} maintenance lease 失败：{exc}")

    def _release_lock(self, failures: list[str]) -> None:
        try:
            self._lock.release()
        except Exception as exc:
            failures.append(f"释放 managed-install lock 失败：{exc}")
