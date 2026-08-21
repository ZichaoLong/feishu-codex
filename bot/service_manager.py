"""
User-service management across supported desktop platforms.
"""

from __future__ import annotations

import os
import pathlib
import plistlib
import subprocess
import xml.etree.ElementTree as ET
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
    source: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AutostartStatus:
    enabled: bool
    source: str = ""
    detail: str = ""


def service_identifier(instance_name: str) -> str:
    normalized = str(instance_name or "").strip().lower() or DEFAULT_INSTANCE_NAME
    if normalized == DEFAULT_INSTANCE_NAME:
        return "focus"
    return f"focus-{normalized}"


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


def _missing_service_definition_message(path: pathlib.Path, instance_name: str) -> str:
    if instance_name == DEFAULT_INSTANCE_NAME:
        return (
            f"service definition 缺失：{path}。"
            " 请重新运行仓库根目录下的 `install.sh` 或 `install.ps1`。"
        )
    return (
        f"service definition 缺失：{path}。"
        " 请重新运行仓库根目录下的 `install.sh` 或 `install.ps1`；"
        f" 如果这是一个新实例，先执行 `focusctl instance create {instance_name}`。"
    )


class ServiceManager:
    def display_name(self, definition: ServiceDefinition) -> str:
        return definition.identifier

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        raise NotImplementedError

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        raise NotImplementedError

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

    def is_instance_uninstalled(self, definition: ServiceDefinition, status: ServiceStatus) -> bool:
        """Return whether per-instance registration and execution are gone."""

        del definition
        return not status.installed and not status.running

    def uninstall_shared(self) -> None:
        return None


class SystemdUserServiceManager(ServiceManager):
    def _unit_name(self, definition: ServiceDefinition) -> str:
        if definition.instance_name == DEFAULT_INSTANCE_NAME:
            return "focus"
        return f"focus@{definition.instance_name}"

    def display_name(self, definition: ServiceDefinition) -> str:
        return self._unit_name(definition)

    def _autostart_status_source(self, definition: ServiceDefinition) -> str:
        return f"systemctl --user is-enabled {self._unit_name(definition)}"

    def _status_source(self, definition: ServiceDefinition) -> str:
        return f"systemctl --user is-active {self._unit_name(definition)}"

    def _template_unit_path(self) -> pathlib.Path:
        return default_systemd_user_dir() / "focus@.service"

    def _unit_path(self, definition: ServiceDefinition) -> pathlib.Path:
        if definition.instance_name == DEFAULT_INSTANCE_NAME:
            return default_systemd_user_dir() / "focus.service"
        return self._template_unit_path()

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        unit_path = self._unit_path(definition)
        if not unit_path.exists():
            raise ServiceManagerError(_missing_service_definition_message(unit_path, definition.instance_name))
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

    def _render_unit(self, definition: ServiceDefinition) -> str:
        if definition.instance_name == DEFAULT_INSTANCE_NAME:
            working_directory = str(definition.paths.data_dir)
            exec_start = " ".join(self._quote_unit_arg(item) for item in definition.daemon_command)
            description = "FOCUS (default)"
        else:
            working_directory = f"{definition.paths.data_dir.parent}/%i"
            if (
                len(definition.daemon_command) < 2
                or definition.daemon_command[-2] != "--instance"
            ):
                raise ServiceManagerError(
                    "命名实例 service command 必须以 `--instance <name>` 结尾。"
                )
            template_command = (*definition.daemon_command[:-1], "%i")
            exec_start = " ".join(
                self._quote_unit_arg(item)
                for item in template_command
            )
            description = "FOCUS (%i)"
        return "\n".join(
            [
                "[Unit]",
                f"Description={description}",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={working_directory}",
                f"ExecStart={exec_start}",
                "Restart=on-failure",
                "RestartSec=10",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )

    def ensure_service(self, definition: ServiceDefinition) -> None:
        unit_path = self._unit_path(definition)
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        definition.paths.data_dir.mkdir(parents=True, exist_ok=True)
        definition.paths.config_dir.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(self._render_unit(definition), encoding="utf-8")
        self._run("systemctl", "--user", "daemon-reload")

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "enable", self._unit_name(definition))

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        self._run("systemctl", "--user", "disable", self._unit_name(definition), check=False)

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        if not self._unit_path(definition).exists():
            return AutostartStatus(
                enabled=False,
                source=self._autostart_status_source(definition),
                detail="unit file missing",
            )
        result = self._run("systemctl", "--user", "is-enabled", self._unit_name(definition), check=False)
        detail = result.stdout.strip() or result.stderr.strip()
        return AutostartStatus(
            enabled=result.returncode == 0,
            source=self._autostart_status_source(definition),
            detail=detail,
        )

    def start(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "start", self._unit_name(definition))

    def stop(self, definition: ServiceDefinition) -> None:
        self._run("systemctl", "--user", "stop", self._unit_name(definition), check=False)

    def restart(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("systemctl", "--user", "restart", self._unit_name(definition))

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        unit_path = self._unit_path(definition)
        if not unit_path.exists():
            return ServiceStatus(installed=False, running=False, source=self._status_source(definition), detail="unit file missing")
        result = self._run("systemctl", "--user", "is-active", self._unit_name(definition), check=False)
        running = result.returncode == 0 and result.stdout.strip() == "active"
        detail = result.stdout.strip() or result.stderr.strip()
        return ServiceStatus(installed=True, running=running, source=self._status_source(definition), detail=detail)

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.autostart_disable(definition)
        self._run("systemctl", "--user", "stop", self._unit_name(definition), check=False)
        if definition.instance_name == DEFAULT_INSTANCE_NAME:
            try:
                self._unit_path(definition).unlink()
            except FileNotFoundError:
                pass
        self._run("systemctl", "--user", "daemon-reload", check=False)

    def is_instance_uninstalled(self, definition: ServiceDefinition, status: ServiceStatus) -> bool:
        if definition.instance_name != DEFAULT_INSTANCE_NAME:
            # Named instances share focus@.service.  The template remaining
            # installed is not a registration for this particular instance.
            return not status.running
        return super().is_instance_uninstalled(definition, status)

    def uninstall_shared(self) -> None:
        try:
            self._template_unit_path().unlink()
        except FileNotFoundError:
            pass
        self._run("systemctl", "--user", "daemon-reload", check=False)


class LaunchdUserServiceManager(ServiceManager):
    """macOS-only launchd user service manager."""

    def _uid_domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _label(self, definition: ServiceDefinition) -> str:
        return f"io.focus.{definition.instance_name}"

    def _autostart_status_source(self, definition: ServiceDefinition) -> str:
        return f"LaunchAgent {self._label(definition)}"

    def _status_source(self, definition: ServiceDefinition) -> str:
        return f"launchctl print {self._uid_domain()}/{self._label(definition)}"

    def _definition_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.paths.data_dir / "service.plist"

    def _plist_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return default_launch_agent_dir() / f"{self._label(definition)}.plist"

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        plist_path = self._definition_path(definition)
        if not plist_path.exists():
            raise ServiceManagerError(_missing_service_definition_message(plist_path, definition.instance_name))
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
        plist_path = self._definition_path(definition)
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
        plist_path = self._definition_path(definition)
        if not plist_path.exists():
            return ServiceStatus(installed=False, running=False, source=self._status_source(definition), detail="plist missing")
        domain = self._uid_domain()
        label = self._label(definition)
        result = self._run("launchctl", "print", f"{domain}/{label}", check=False)
        running = result.returncode == 0 and "state = running" in result.stdout
        detail = result.stdout.strip() or result.stderr.strip()
        return ServiceStatus(installed=True, running=running, source=self._status_source(definition), detail=detail)

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self.autostart_disable(definition)
        try:
            self._definition_path(definition).unlink()
        except FileNotFoundError:
            pass

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        definition_path = self._require_installed(definition)
        autostart_path = self._plist_path(definition)
        autostart_path.parent.mkdir(parents=True, exist_ok=True)
        if autostart_path.exists() or autostart_path.is_symlink():
            autostart_path.unlink()
        autostart_path.symlink_to(definition_path)

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        try:
            self._plist_path(definition).unlink()
        except FileNotFoundError:
            pass

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        autostart_path = self._plist_path(definition)
        if autostart_path.is_symlink() and not autostart_path.exists():
            return AutostartStatus(
                enabled=False,
                source=self._autostart_status_source(definition),
                detail="launch agent symlink is dangling",
            )
        enabled = autostart_path.exists()
        detail = str(autostart_path) if enabled else "launch agent disabled"
        return AutostartStatus(
            enabled=enabled,
            source=self._autostart_status_source(definition),
            detail=detail,
        )


class WindowsTaskSchedulerServiceManager(ServiceManager):
    _TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    _TASK_STATE_LABELS = {
        0: "unknown",
        1: "disabled",
        2: "queued",
        3: "ready",
        4: "running",
    }

    def _task_name(self, definition: ServiceDefinition) -> str:
        return definition.identifier

    def _autostart_status_source(self, definition: ServiceDefinition) -> str:
        return f"schtasks /Query /TN {self._task_name(definition)} /XML"

    def _status_source(self, definition: ServiceDefinition) -> str:
        return f"Task Scheduler COM state for {self._task_name(definition)}"

    def _launcher_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.paths.data_dir / "service-launch.cmd"

    def _task_xml_path(self, definition: ServiceDefinition) -> pathlib.Path:
        return definition.paths.data_dir / "service-task.xml"

    @staticmethod
    def _is_access_denied_error(message: str) -> bool:
        normalized = str(message or "").strip().lower()
        if not normalized:
            return False
        return "access is denied" in normalized or "拒绝访问" in normalized

    def _rewrite_existing_task_access_denied_message(self, definition: ServiceDefinition, original_message: str) -> str:
        task_name = self._task_name(definition)
        return (
            "Task Scheduler 拒绝改写现有任务；这通常表示该任务是由不同权限上下文创建的旧任务。\n"
            "请先在当前 PowerShell 中删除旧任务；如果仍然提示拒绝访问，再改用管理员 PowerShell：\n"
            f"  schtasks /Delete /TN {task_name} /F\n"
            f"  focusctl --instance {definition.instance_name} service autostart enable\n"
            f"原始错误：{original_message}"
        )

    def _require_installed(self, definition: ServiceDefinition) -> pathlib.Path:
        launcher_path = self._launcher_path(definition)
        if not launcher_path.exists():
            raise ServiceManagerError(_missing_service_definition_message(launcher_path, definition.instance_name))
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

    def _query_task_xml(self, definition: ServiceDefinition) -> ET.Element | None:
        result = self._run("schtasks", "/Query", "/TN", self._task_name(definition), "/XML", check=False)
        if result.returncode != 0:
            return None
        try:
            return ET.fromstring(result.stdout)
        except ET.ParseError as exc:
            raise ServiceManagerError("Task Scheduler 返回了无法解析的 XML。") from exc

    def _task_autostart_enabled(self, definition: ServiceDefinition) -> bool:
        root = self._query_task_xml(definition)
        if root is None:
            return False
        return root.find(f".//{{{self._TASK_XML_NAMESPACE}}}LogonTrigger") is not None

    def _query_task_state(self, definition: ServiceDefinition) -> int | None:
        # schtasks /FO LIST localizes both the "Status" field name and values.
        # The Task Scheduler COM API exposes the stable TASK_STATE enum instead.
        # Handle ERROR_FILE_NOT_FOUND by HRESULT so missing and access-denied
        # cannot collapse into the same localized command failure.
        script = (
            "& { param([string]$TaskName) "
            "$ErrorActionPreference = 'Stop'; "
            "try { "
            "$service = New-Object -ComObject 'Schedule.Service'; "
            "$service.Connect(); "
            "$task = $service.GetFolder('\\').GetTask($TaskName); "
            "[Console]::Out.Write([int]$task.State) "
            "} catch { "
            "if ($_.Exception.HResult -eq -2147024894) { "
            "[Console]::Out.Write('missing'); exit 0 "
            "}; throw "
            "} }"
        )
        result = self._run(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            self._task_name(definition),
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Task Scheduler state query failed"
            raise ServiceManagerError(detail)
        rendered = result.stdout.strip()
        if rendered == "missing":
            return None
        try:
            state = int(rendered)
        except ValueError as exc:
            raise ServiceManagerError("Task Scheduler 返回了无法解析的 numeric state。") from exc
        if state not in self._TASK_STATE_LABELS:
            raise ServiceManagerError(f"Task Scheduler 返回了未知 state：{state}")
        return state

    def _task_xml_bytes(self, definition: ServiceDefinition, *, autostart_enabled: bool) -> bytes:
        ET.register_namespace("", self._TASK_XML_NAMESPACE)

        def tag(name: str) -> str:
            return f"{{{self._TASK_XML_NAMESPACE}}}{name}"

        task = ET.Element(tag("Task"), {"version": "1.3"})
        registration = ET.SubElement(task, tag("RegistrationInfo"))
        ET.SubElement(registration, tag("Description")).text = f"FOCUS ({definition.instance_name})"
        if autostart_enabled:
            triggers = ET.SubElement(task, tag("Triggers"))
            logon = ET.SubElement(triggers, tag("LogonTrigger"))
            ET.SubElement(logon, tag("Enabled")).text = "true"
        principals = ET.SubElement(task, tag("Principals"))
        principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
        ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
        ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"
        settings = ET.SubElement(task, tag("Settings"))
        for key, value in (
            ("AllowStartOnDemand", "true"),
            ("MultipleInstancesPolicy", "IgnoreNew"),
            ("DisallowStartIfOnBatteries", "false"),
            ("StopIfGoingOnBatteries", "false"),
            ("AllowHardTerminate", "true"),
            ("StartWhenAvailable", "false"),
            ("RunOnlyIfNetworkAvailable", "false"),
            ("Enabled", "true"),
            ("Hidden", "false"),
            ("ExecutionTimeLimit", "PT0S"),
            ("Priority", "7"),
            ("RunOnlyIfIdle", "false"),
            ("WakeToRun", "false"),
        ):
            ET.SubElement(settings, tag(key)).text = value
        actions = ET.SubElement(task, tag("Actions"), {"Context": "Author"})
        exec_action = ET.SubElement(actions, tag("Exec"))
        ET.SubElement(exec_action, tag("Command")).text = str(self._launcher_path(definition))
        return ET.tostring(task, encoding="utf-16", xml_declaration=True)

    def _register_task(self, definition: ServiceDefinition, *, autostart_enabled: bool) -> None:
        xml_path = self._task_xml_path(definition)
        xml_path.write_bytes(self._task_xml_bytes(definition, autostart_enabled=autostart_enabled))
        try:
            self._run(
                "schtasks",
                "/Create",
                "/TN",
                self._task_name(definition),
                "/XML",
                str(xml_path),
                "/F",
            )
        except ServiceManagerError as exc:
            if self._is_access_denied_error(str(exc)) and self._query_task_xml(definition) is not None:
                raise ServiceManagerError(
                    self._rewrite_existing_task_access_denied_message(definition, str(exc))
                ) from exc
            raise

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
        self._register_task(definition, autostart_enabled=self._task_autostart_enabled(definition))

    def start(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._run("schtasks", "/Run", "/TN", self._task_name(definition))

    def stop(self, definition: ServiceDefinition) -> None:
        self._run("schtasks", "/End", "/TN", self._task_name(definition), check=False)

    def status(self, definition: ServiceDefinition) -> ServiceStatus:
        state = self._query_task_state(definition)
        if state is None:
            return ServiceStatus(
                installed=False,
                running=False,
                source=self._status_source(definition),
                detail="scheduled task missing",
            )
        return ServiceStatus(
            installed=True,
            # Queued is a live/in-flight state for lifecycle and destructive
            # admission purposes, even before Task Scheduler reports Running.
            running=state in {2, 4},
            source=self._status_source(definition),
            detail=f"state={state} ({self._TASK_STATE_LABELS[state]})",
        )

    def uninstall(self, definition: ServiceDefinition) -> None:
        self.stop(definition)
        self._run("schtasks", "/Delete", "/TN", self._task_name(definition), "/F", check=False)
        try:
            self._launcher_path(definition).unlink()
        except FileNotFoundError:
            pass
        try:
            self._task_xml_path(definition).unlink()
        except FileNotFoundError:
            pass

    def autostart_enable(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._register_task(definition, autostart_enabled=True)

    def autostart_disable(self, definition: ServiceDefinition) -> None:
        self._require_installed(definition)
        self._register_task(definition, autostart_enabled=False)

    def autostart_status(self, definition: ServiceDefinition) -> AutostartStatus:
        if self._query_task_xml(definition) is None:
            return AutostartStatus(
                enabled=False,
                source=self._autostart_status_source(definition),
                detail="scheduled task missing",
            )
        enabled = self._task_autostart_enabled(definition)
        detail = "logon trigger enabled" if enabled else "logon trigger disabled"
        return AutostartStatus(
            enabled=enabled,
            source=self._autostart_status_source(definition),
            detail=detail,
        )


def current_service_manager() -> ServiceManager:
    if is_windows():
        return WindowsTaskSchedulerServiceManager()
    if is_macos():
        return LaunchdUserServiceManager()
    if is_linux():
        return SystemdUserServiceManager()
    raise ServiceManagerError("当前平台不支持后台 service 管理。")
