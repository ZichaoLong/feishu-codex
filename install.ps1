Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:FOCUS_POWERSHELL_PROFILE_PATH = $PROFILE.CurrentUserAllHosts

function Test-SupportedPython {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Command,

    [string[]]$Arguments = @()
  )

  if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
    return $false
  }

  $probeArgs = @($Arguments) + @(
    "-c",
    "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info >= (3, 11) else 1)"
  )
  & $Command @probeArgs *> $null
  return $LASTEXITCODE -eq 0
}

function Get-PersistentExecutionPolicy {
  foreach ($scope in @("MachinePolicy", "UserPolicy", "CurrentUser", "LocalMachine")) {
    $policy = Get-ExecutionPolicy -Scope $scope -ErrorAction SilentlyContinue
    if ($policy -and $policy -ne "Undefined") {
      return [string]$policy
    }
  }

  return "Restricted"
}

function Test-PowerShellProfileAutoloadAllowed {
  return @("Bypass", "Unrestricted", "RemoteSigned") -contains (Get-PersistentExecutionPolicy)
}

try {
  $pythonCommand = $null
  $pythonArgs = @()

  if (Test-SupportedPython -Command "py" -Arguments @("-3")) {
    $pythonCommand = "py"
    $pythonArgs = @("-3")
  }
  elseif (Test-SupportedPython -Command "python") {
    $pythonCommand = "python"
  }
  elseif (Test-SupportedPython -Command "python3") {
    $pythonCommand = "python3"
  }

  if (-not $pythonCommand) {
    Write-Error "需要 CPython 3.11 或更高版本。"
  }

  if (-not (Test-PowerShellProfileAutoloadAllowed)) {
    $env:FOCUS_POWERSHELL_SKIP_PROFILE_AUTOLOAD = "1"
  }

  & $pythonCommand @pythonArgs "$scriptDir\install.py" @args
  exit $LASTEXITCODE
}
finally {
  Remove-Item Env:FOCUS_POWERSHELL_SKIP_PROFILE_AUTOLOAD -ErrorAction SilentlyContinue
  Remove-Item Env:FOCUS_POWERSHELL_PROFILE_PATH -ErrorAction SilentlyContinue
}
