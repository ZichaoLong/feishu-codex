Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (Get-Command py -ErrorAction SilentlyContinue) {
  & py -3.11 "$scriptDir\install.py"
  exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
  & python "$scriptDir\install.py"
  exit $LASTEXITCODE
}

Write-Error "需要 Python 3.11 或更高版本。"
