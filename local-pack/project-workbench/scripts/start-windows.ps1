$ErrorActionPreference = 'Stop'
$TaskSourceRoot = Split-Path -Parent $PSScriptRoot
$TaskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $TaskPythonCommand) { throw 'Python 3.11+ is required. Install Python before running the PoC.' }
Set-Location $TaskSourceRoot
& $TaskPythonCommand.Source "$PSScriptRoot/serve.py"
