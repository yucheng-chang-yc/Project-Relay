$ErrorActionPreference = 'Stop'
$TaskSourceRoot = Split-Path -Parent $PSScriptRoot
$TaskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $TaskPythonCommand) { throw 'Python 3.11+ is required.' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git is required.' }
Set-Location $TaskSourceRoot
& $TaskPythonCommand.Source "$PSScriptRoot/configure.py" --demo
if ($LASTEXITCODE -ne 0) { throw 'Setup did not complete. Existing configuration was preserved.' }
Write-Host 'Demo configured. Start start-windows.ps1, then run open_dashboard.py.'
