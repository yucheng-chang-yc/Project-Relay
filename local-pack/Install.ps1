param(
    [string]$Root = (Join-Path $env:USERPROFILE 'ProjectRelay'),
    [string]$PythonExe = 'python',
    [switch]$Demo,
    [switch]$Plan
)
$ErrorActionPreference = 'Stop'
$TaskPython = Get-Command $PythonExe -ErrorAction Stop
$TaskArguments = @((Join-Path $PSScriptRoot 'install.py'), '--root', $Root)
if ($Demo) { $TaskArguments += '--demo' }
if ($Plan) { $TaskArguments += '--plan' }
& $TaskPython.Source @TaskArguments
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Existing installations were preserved.' }
