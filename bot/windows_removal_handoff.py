"""Out-of-process Windows deletion for the managed Python installation.

The current ``focusctl`` interpreter can live inside the tree being removed.
This owner stages an exact deletion plan and obtains proof that the helper owns
the handoff barrier before the parent exits.  The parent reports only that
handoff; the helper writes the eventual deletion proof to a separate result.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from bot.file_permissions import ensure_private_file_permissions
from bot.install_lifecycle import managed_install_handoff_lock_path


_WINDOWS_REMOVAL_SCHEMA = 1
_ARM_TIMEOUT_SECONDS = 5.0


class WindowsRemovalHandoffError(RuntimeError):
    """A Windows removal helper could not be staged or safely committed."""


@dataclass(frozen=True, slots=True)
class WindowsRemovalTarget:
    role: str
    path: pathlib.Path


@dataclass(frozen=True, slots=True)
class WindowsRemovalHandoffReceipt:
    handoff_id: str
    operation: Literal["uninstall", "purge"]
    helper_pid: int
    result_path: pathlib.Path


_HELPER_SCRIPT = r'''param(
    [Parameter(Mandatory=$true)][string]$PlanPath,
    [Parameter(Mandatory=$true)][string]$ArmedPath,
    [Parameter(Mandatory=$true)][string]$ResultPath
)

$ErrorActionPreference = 'Stop'

function Write-JsonFile {
    param([string]$Path, [object]$Value)
    $temporary = "$Path.tmp-$PID"
    $json = $Value | ConvertTo-Json -Depth 8
    [System.IO.File]::WriteAllText(
        $temporary,
        $json + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

$plan = $null
$targetResults = @()
$errors = @()
$handoffLock = $null
$handoffLockHeld = $false
try {
    $plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$plan.schema -ne 1) { throw 'unsupported removal plan schema' }
    if ([string]::IsNullOrWhiteSpace([string]$plan.handoff_id)) { throw 'missing handoff id' }
    if ([string]$plan.operation -notin @('uninstall', 'purge')) { throw 'invalid removal operation' }
    if ([int]$plan.parent_pid -le 0) { throw 'invalid parent pid' }
    if (-not [System.IO.Path]::IsPathRooted([string]$plan.handoff_lock_path)) {
        throw 'handoff lock path is not absolute'
    }
    foreach ($target in @($plan.targets)) {
        if ([string]::IsNullOrWhiteSpace([string]$target.role)) { throw 'target role is empty' }
        if (-not [System.IO.Path]::IsPathRooted([string]$target.path)) {
            throw "target path is not absolute: $($target.path)"
        }
    }

    # Obtaining the Process object while the parent is known alive avoids PID
    # reuse ambiguity. Force its exact OS handle open before arming; the
    # Process object retains that handle through WaitForExit.
    $parent = [System.Diagnostics.Process]::GetProcessById([int]$plan.parent_pid)
    $null = $parent.Handle

    # The parent yielded only this barrier while retaining the primary lock, so
    # no normal install can pass both locks while ownership moves.
    $handoffLock = [System.IO.File]::Open(
        [string]$plan.handoff_lock_path,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::ReadWrite
    )
    if ($handoffLock.Length -lt 1) {
        $handoffLock.SetLength(1)
        $handoffLock.Flush()
    }
    $handoffDeadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not $handoffLockHeld) {
        try {
            $handoffLock.Lock(0, 1)
            $handoffLockHeld = $true
        } catch [System.IO.IOException] {
            if ($parent.HasExited) { throw 'parent exited before handoff barrier transfer' }
            if ([DateTime]::UtcNow -ge $handoffDeadline) {
                throw 'handoff barrier transfer timed out'
            }
            Start-Sleep -Milliseconds 50
        }
    }
    Write-JsonFile -Path $ArmedPath -Value ([ordered]@{
        schema = 1
        handoff_id = [string]$plan.handoff_id
        parent_pid = [int]$plan.parent_pid
        helper_pid = $PID
        status = 'armed'
    })

    $parent.WaitForExit()

    foreach ($target in @($plan.targets)) {
        $role = [string]$target.role
        $path = [string]$target.path
        try {
            if (-not (Test-Path -LiteralPath $path)) {
                $targetResults += [ordered]@{ role = $role; path = $path; status = 'missing' }
                continue
            }
            $item = Get-Item -LiteralPath $path -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'target root became a reparse point'
            }
            Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $path) { throw 'target still exists after deletion' }
            $targetResults += [ordered]@{ role = $role; path = $path; status = 'deleted' }
        } catch {
            $message = [string]$_.Exception.Message
            $targetResults += [ordered]@{ role = $role; path = $path; status = 'failed'; error = $message }
            $errors += "${role}=${path}: $message"
        }
    }
} catch {
    $errors += [string]$_.Exception.Message
} finally {
    if ($handoffLockHeld -and $null -ne $handoffLock) {
        try { $handoffLock.Unlock(0, 1) } catch { }
    }
    if ($null -ne $handoffLock) { $handoffLock.Dispose() }
    $operation = if ($null -ne $plan) { [string]$plan.operation } else { '' }
    $handoffId = if ($null -ne $plan) { [string]$plan.handoff_id } else { '' }
    $status = if ($errors.Count -eq 0) { 'complete' } else { 'failed' }
    Write-JsonFile -Path $ResultPath -Value ([ordered]@{
        schema = 1
        handoff_id = $handoffId
        operation = $operation
        status = $status
        targets = @($targetResults)
        errors = @($errors)
        helper_pid = $PID
        finished_at_utc = [DateTime]::UtcNow.ToString('o')
    })
    Remove-Item -LiteralPath $ArmedPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
'''


def _path_contains(parent: pathlib.Path, child: pathlib.Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _atomic_json(path: pathlib.Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    ensure_private_file_permissions(temporary)
    os.replace(temporary, path)
    ensure_private_file_permissions(path)


class WindowsRemovalHandoff:
    def __init__(
        self,
        *,
        operation: Literal["uninstall", "purge"],
        parent_pid: int,
        powershell_executable: str,
        staging_dir: pathlib.Path,
        handoff_id: str,
    ) -> None:
        self.operation = operation
        self.parent_pid = int(parent_pid)
        self.powershell_executable = str(powershell_executable)
        self.staging_dir = pathlib.Path(staging_dir)
        self.handoff_id = str(handoff_id)
        self.plan_path = self.staging_dir / "plan.json"
        self.script_path = self.staging_dir / "remove.ps1"
        self.armed_path = self.staging_dir / "armed.json"
        self.result_path = self.staging_dir / "result.json"
        self._process: subprocess.Popen[bytes] | None = None
        self._armed = False

    @property
    def armed(self) -> bool:
        return self._armed

    def launch(
        self,
        *,
        arm_timeout_seconds: float = _ARM_TIMEOUT_SECONDS,
    ) -> WindowsRemovalHandoffReceipt:
        if self._process is not None:
            raise WindowsRemovalHandoffError("Windows removal helper is already running")
        creationflags = 0
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        try:
            process = subprocess.Popen(
                [
                    self.powershell_executable,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.script_path),
                    "-PlanPath",
                    str(self.plan_path),
                    "-ArmedPath",
                    str(self.armed_path),
                    "-ResultPath",
                    str(self.result_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise WindowsRemovalHandoffError(
                f"无法启动 Windows 删除 helper：{exc}"
            ) from exc
        self._process = process

        deadline = time.monotonic() + max(float(arm_timeout_seconds), 0.0)
        try:
            armed: dict[str, object] | None = None
            while time.monotonic() <= deadline:
                if self.armed_path.is_file():
                    try:
                        raw = json.loads(self.armed_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        raw = None
                    if isinstance(raw, dict):
                        armed = raw
                        break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if armed is None:
                raise WindowsRemovalHandoffError(
                    "Windows 删除 helper 未能取得 handoff barrier；删除任务不会继续。"
                )
            helper_pid = armed.get("helper_pid")
            if (
                set(armed)
                != {"schema", "handoff_id", "parent_pid", "helper_pid", "status"}
                or armed.get("schema") != _WINDOWS_REMOVAL_SCHEMA
                or armed.get("handoff_id") != self.handoff_id
                or armed.get("parent_pid") != self.parent_pid
                or armed.get("status") != "armed"
                or isinstance(helper_pid, bool)
                or not isinstance(helper_pid, int)
                or helper_pid <= 0
            ):
                raise WindowsRemovalHandoffError(
                    "Windows 删除 helper 返回了不匹配的 armed proof；删除任务不会继续。"
                )
            self._armed = True
            return WindowsRemovalHandoffReceipt(
                handoff_id=self.handoff_id,
                operation=self.operation,
                helper_pid=helper_pid,
                result_path=self.result_path,
            )
        except BaseException:
            if not self._armed:
                self._stop_pre_arm_helper()
            raise

    def discard(self) -> None:
        if self._armed:
            return
        self._stop_pre_arm_helper()
        shutil.rmtree(self.staging_dir, ignore_errors=True)

    def _stop_pre_arm_helper(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2.0)
            except Exception:
                pass


def prepare_windows_removal_handoff(
    *,
    operation: Literal["uninstall", "purge"],
    parent_pid: int,
    machine_lock_path: pathlib.Path,
    targets: tuple[WindowsRemovalTarget, ...],
    powershell_executable: str | None = None,
    staging_parent: pathlib.Path | None = None,
) -> WindowsRemovalHandoff:
    if operation not in {"uninstall", "purge"}:
        raise WindowsRemovalHandoffError(f"不支持的 Windows 删除操作：{operation}")
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        raise WindowsRemovalHandoffError("Windows 删除 handoff 缺少有效 parent PID")
    executable = str(
        powershell_executable
        or shutil.which("powershell.exe")
        or shutil.which("pwsh.exe")
        or ""
    ).strip()
    if not executable:
        raise WindowsRemovalHandoffError(
            "找不到 powershell.exe/pwsh.exe；不会开始卸载或 purge。"
        )
    if not targets:
        raise WindowsRemovalHandoffError("Windows 删除 handoff 至少需要一个 exact target")

    normalized_targets: list[WindowsRemovalTarget] = []
    roles: set[str] = set()
    for target in targets:
        role = str(target.role or "").strip()
        raw_path = pathlib.Path(target.path).expanduser()
        if not role or role in roles:
            raise WindowsRemovalHandoffError("Windows 删除 target role 必须非空且唯一")
        if raw_path.is_symlink():
            raise WindowsRemovalHandoffError(
                f"Windows 删除 target 不能是符号链接或 reparse projection：{raw_path}"
            )
        path = raw_path.resolve(strict=False)
        if not path.is_absolute() or path == pathlib.Path(path.anchor):
            raise WindowsRemovalHandoffError(f"Windows 删除 target 过宽或非绝对路径：{path}")
        roles.add(role)
        normalized_targets.append(WindowsRemovalTarget(role=role, path=path))
    for index, target in enumerate(normalized_targets):
        for other in normalized_targets[index + 1 :]:
            if _path_contains(target.path, other.path) or _path_contains(other.path, target.path):
                raise WindowsRemovalHandoffError(
                    f"Windows 删除 targets 不能相同或互相包含：{target.path} / {other.path}"
                )

    lock_path = pathlib.Path(machine_lock_path).expanduser().resolve(strict=False)
    if not lock_path.is_absolute():
        raise WindowsRemovalHandoffError("Windows 删除 handoff 的 machine lock 必须是绝对路径")
    handoff_lock_path = managed_install_handoff_lock_path(lock_path)
    if any(
        _path_contains(target.path, candidate)
        for target in normalized_targets
        for candidate in (lock_path, handoff_lock_path)
    ):
        raise WindowsRemovalHandoffError(
            "Windows 删除 handoff 的 machine lock/barrier 必须位于所有删除 target 之外"
        )

    staging_parent_path = pathlib.Path(staging_parent).expanduser() if staging_parent is not None else None
    if staging_parent_path is not None:
        staging_parent_path.mkdir(parents=True, exist_ok=True)
    staging_dir = pathlib.Path(
        tempfile.mkdtemp(
            prefix="focus-removal-",
            dir=str(staging_parent_path) if staging_parent_path is not None else None,
        )
    ).resolve(strict=False)
    try:
        if any(
            _path_contains(target.path, staging_dir)
            or _path_contains(staging_dir, target.path)
            for target in normalized_targets
        ):
            raise WindowsRemovalHandoffError(
                "Windows 删除 helper staging 目录必须位于所有删除 target 之外"
            )
        handoff_id = uuid.uuid4().hex
        handoff = WindowsRemovalHandoff(
            operation=operation,
            parent_pid=parent_pid,
            powershell_executable=executable,
            staging_dir=staging_dir,
            handoff_id=handoff_id,
        )
        _atomic_json(
            handoff.plan_path,
            {
                "schema": _WINDOWS_REMOVAL_SCHEMA,
                "handoff_id": handoff_id,
                "operation": operation,
                "parent_pid": parent_pid,
                "handoff_lock_path": str(handoff_lock_path),
                "targets": [
                    {"role": target.role, "path": str(target.path)}
                    for target in normalized_targets
                ],
            },
        )
        handoff.script_path.write_text(_HELPER_SCRIPT, encoding="utf-8", newline="\r\n")
        ensure_private_file_permissions(handoff.script_path)
        return handoff
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
