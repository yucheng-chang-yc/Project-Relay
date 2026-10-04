param(
    [string]$PythonExe = 'python',
    [string]$TrialRoot = (Join-Path $env:TEMP ('ProjectRelay-Acceptance-' + [guid]::NewGuid().ToString('N')))
)
$ErrorActionPreference = 'Stop'
$TaskPython = (Get-Command $PythonExe -ErrorAction Stop).Source
if (Test-Path -LiteralPath $TrialRoot) { throw 'Choose a new acceptance directory.' }
New-Item -ItemType Directory -Path $TrialRoot | Out-Null
$InstallRoot = Join-Path $TrialRoot 'installed runtime'
$CheckFile = Join-Path $TrialRoot 'install-check.json'
$SmokeFile = Join-Path $TrialRoot 'project-smoke.json'
& $TaskPython (Join-Path $PSScriptRoot 'install.py') --root $InstallRoot --demo
if ($LASTEXITCODE -ne 0) { throw 'Fresh installation failed.' }
& $TaskPython (Join-Path $InstallRoot 'maintenance/check_install.py') --root $InstallRoot --self-test --output $CheckFile
if ($LASTEXITCODE -ne 0) { throw 'Installation self-test failed.' }
& $TaskPython (Join-Path $InstallRoot 'maintenance/smoke_test.py') --root $InstallRoot --project-id sandbox --output $SmokeFile
if ($LASTEXITCODE -ne 0) { throw 'Project smoke test failed.' }
[pscustomobject]@{
    status = 'PASS'
    scope = 'Windows fresh installation and local MCP project inspection'
    host_chatgpt = 'NOT_TESTED'
    executor_run = 'NOT_RUN'
    install_check = $CheckFile
    project_smoke = $SmokeFile
    retained_installation = $InstallRoot
} | ConvertTo-Json
