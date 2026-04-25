"""
User-service management across supported desktop platforms.
"""

from __future__ import annotations

import os
import pathlib
import plistlib
import shlex
import subprocess
from dataclasses import dataclass

from bot.instance_layout import DEFAULT_INSTANCE_NAME, InstancePaths
from bot.platform_paths import (
    default_launch_agent_dir,
    default_systemd_user_dir,
    is_linux,
    is_macos,
    is_windows,
)


class ServiceManagerError(RuntimeError):
    """Raised when local service management fails."""


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    instance_name: str
    identifier: str
    paths: InstancePaths
    daemon_command: tuple[str, ...]
    stdout_log_path: pathlib.Path
    stderr_log_path: pathlib.Path


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    installed: bool
    running: bool
    detail: str = ""


def service_identifier(instance_name: str) -> str:
    normalized = str(instance_name or "").strip().lower() or DEFAULT_INSTANCE_NAME
    if normalized == DEFAULT_INSTANCE_NAME:
        return "feishu-codex"
    return f"feishu-codex-{normalized}"


def build_service_definition(
    *,
    instance_name: str,
    paths: InstancePaths,
    daemon_command: list[str] | tuple[str, ...],
) -> ServiceDefinition:
    identifier = service_identifier(instance_name)
    return ServiceDefinition(
        instance_name=instance_name,
        identifier=identifier,
        paths=paths,
        daemon_command=tuple(str(item) for item in daemon_command),
        stdout_log_path=paths.data_dir / "service.stdout.log",
        stderr_log_path=paths.data_dir / "service.stderr.log",
    )


class ServiceManager:
    def ensure_service(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def start(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def stop(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def restart(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self.start(definition)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        raise NotImplementedError

    def uninstall(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError


class SystemdUserServiceManager(ServiceManager):
    def _unit_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return default_systemd_user_dir() / f"{definition.identifier}.service"

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        unit_path = self._unit_path(definition)
        if not unit_path.exists():
            raise ServiceManagerError(
                f"service definition 缺失：{unit_path}。"
                " 请先执行 `feishu-codex install`，或对命名实例执行"
                f" `feishu-codex instance create {definition.instance_name}`。"
            )
        return unit_path

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=check,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise ServiceManagerError("systemctl 不可用。") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise ServiceManagerError(message) from exc

    @staticmethod
    def _quote_unit_arg(arg: str) -> str:
        escaped = str(arg).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def ensure_service(self, definition: ServiceDefinition) -> None:
        unit_path = self._unit_path(definition)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        definition.paths.data_dir.mkdir(parents=True, exist_ok=True)
        definition.paths.config_dir.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(self._quote_unit_arg(item) for item in definition.daemon_command)
        unit_path.write_text(
            "\n".join(
                [
                    "[Unit]",
                    f"Description=Feishu Codex ({definition.instance_name})",
                    "After=network-online.target",
                    "Wants=network-online.target",
                    "",
                    "[Service]",
                    "Type=simple",
                    f"WorkingDirectory={definition.paths.data_dir}",
                    f"ExecStart={exec_start}",
                    "Restart=on-failure",
                    "RestartSec=10",
                    "",
                    "[Install]",
                    "WantedBy=default.target",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._run("systemctl", "--user", "daemon-reload")

    def start(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "enable", definition.identifier)
        self._run("systemctl", "--user", "start", definition.identifier)

    def stop(self, definition: ServiceDefinition) -> None:
        self._run("systemctl", "--user", "stop", definition.identifier, check=False)

    def restart(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "restart", definition.identifier)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        unit_path = self._unit_path(definition)
        if not unit_path.exists():
            return ServiceStatus(installed=False, running=False, detail="unit file missing")
        result = self._run("systemctl", "--user", "is-active", definition.identifier, check=False)
        running = result.returncode == 0 and result.stdout.strip() == "active"
        detail = result.stdout.strip() or result.stderr.strip()
        return ServiceStatus(installed=True, running=running, detail=detail)

    def uninstall(self, definition: ServiceDefinition) -> None:
        self._run("systemctl", "--user", "disable", definition.identifier, check=False)
        self._run("systemctl", "--user", "stop", definition.identifier, check=False)
        try:
            self._unit_path(definition).unlink()
        except FileNotFoundError:
            pass
        self._run("systemctl", "--user", "daemon-reload", check=False)


class LaunchdUserServiceManager(ServiceManager):
    """macOS-only launchd user service manager."""

    def _uid_domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _label(self, definition: ServiceDefinition) -> str:
        return f"io.feishu-codex.{definition.instance_name}"

    def _plist_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return default_launch_agent_dir() / f"{self._label(definition)}.plist"

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        plist_path = self._plist_path(definition)
        if not plist_path.exists():
            raise ServiceManagerError(
                f"service definition 缺失：{plist_path}。"
                " 请先执行 `feishu-codex install`，或对命名实例执行"
                f" `feishu-codex instance create {definition.instance_name}`。"
            )
        return plist_path

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=check,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise ServiceManagerError("launchctl 不可用。") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise ServiceManagerError(message) from exc

    def ensure_service(self, definition: ServiceDefinition) -> None:
        plist_path = self._plist_path(definition)
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        definition.paths.data_dir.mkdir(parents=True, exist_ok=True)
        definition.paths.config_dir.mkdir(parents=True, exist_ok=True)
        definition.stdout_log_path.parent.mkdir(parents=True, exist_ok=True)
        definition.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": self._label(definition),
            "ProgramArguments": list(definition.daemon_command),
            "WorkingDirectory": str(definition.paths.data_dir),
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(definition.stdout_log_path),
            "StandardErrorPath": str(definition.stderr_log_path),
        }
        plist_path.write_bytes(plistlib.dumps(payload))

    def start(self, definition: ServiceDefinition) -> None:
        plist_path = self._require_installed(definition)
        domain = self._uid_domain()
        label = self._label(definition)
        self._run("launchctl", "bootout", domain, label, check=False)
        self._run("launchctl", "bootstrap", domain, str(plist_path))
        self._run("launchctl", "kickstart", "-k", f"{domain}/{label}", check=False)

    def stop(self, definition: ServiceDefinition) -> None:
        domain = self._uid_domain()
        label = self._label(definition)
        self._run("launchctl", "bootout", domain, label, check=False)

    def restart(self, definition: ServiceDefinition) -> None:
        self.start(definition)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        plist_path = self._plist_path(definition)
        if not plist_path.exists():
            return ServiceStatus(installed=False, running=False, detail="plist missing")
        domain = self._uid_domain()
        label = self._label(definition)
        result = self._run("launchctl", "print", f"{domain}/{label}", check=False)
        running = result.returncode == 0 and "state = running" in result.stdout
        detail = result.stdout.strip() or result.stderr.strip()
        return ServiceStatus(installed=True, running=running, detail=detail)

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        try:
            self._plist_path(definition).unlink()
        except FileNotFoundError:
            pass


class WindowsTaskSchedulerServiceManager(ServiceManager):
    def _task_name(self, definition: ServiceDefinition) -> str:
        return definition.identifier

    def _launcher_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.paths.data_dir / "service-launch.cmd"

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        launcher_path = self._launcher_path(definition)
        if not launcher_path.exists():
            raise ServiceManagerError(
                f"service definition 缺失：{launcher_path}。"
                " 请先执行 `feishu-codex install`，或对命名实例执行"
                f" `feishu-codex instance create {definition.instance_name}`。"
            )
        return launcher_path

    @staticmethod
    def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=check,
                text=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise ServiceManagerError("schtasks.exe 不可用。") from exc
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise ServiceManagerError(message) from exc

    def ensure_service(self, definition: ServiceDefinition) -> None:
        definition.paths.data_dir.mkdir(parents=True, exist_ok=True)
        definition.paths.config_dir.mkdir(parents=True, exist_ok=True)
        launcher_path = self._launcher_path(definition)
        launcher_path.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    f'cd /d "{definition.paths.data_dir}"',
                    " ".join(f'"{item}"' for item in definition.daemon_command),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._run(
            "schtasks",
            "/Create",
            "/TN",
            self._task_name(definition),
            "/SC",
            "ONLOGON",
            "/RL",
            "LIMITED",
            "/TR",
            str(launcher_path),
            "/F",
        )

    def start(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("schtasks", "/Run", "/TN", self._task_name(definition))

    def stop(self, definition: ServiceDefinition) -> None:
        self._run("schtasks", "/End", "/TN", self._task_name(definition), check=False)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        result = self._run("schtasks", "/Query", "/TN", self._task_name(definition), "/FO", "LIST", "/V", check=False)
        if result.returncode != 0:
            return ServiceStatus(installed=False, running=False, detail=result.stderr.strip() or result.stdout.strip())
        status_line = next((line for line in result.stdout.splitlines() if line.startswith("Status:")), "")
        running = "Running" in status_line
        return ServiceStatus(installed=True, running=running, detail=status_line.strip())

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self._run("schtasks", "/Delete", "/TN", self._task_name(definition), "/F", check=False)
        try:
            self._launcher_path(definition).unlink()
        except FileNotFoundError:
            pass


def current_service_manager() -> ServiceManager:
    if is_windows():
        return WindowsTaskSchedulerServiceManager()
    if is_macos():
        return LaunchdUserServiceManager()
    if is_linux():
        return SystemdUserServiceManager()
    raise ServiceManagerError("当前平台不支持后台 service 管理。")
