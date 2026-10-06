# Claude Code operator handoff — Project Relay preview.3

Written for: local Windows operator. This is installation and bounded verification of the supplied candidate. Do not redesign, modify source, commit/push, publish a Release, or declare Product ACCEPTED. Report exact failures back to the development window. Local installation and live ChatGPT acceptance are separate.

## Supplied files

Use this single handoff bundle. It contains:

- `Project-Relay-Local-v0.2.0-preview.3.zip`: actual clean installer/runtime, tests and docs.
- `Project-Relay-Plugin-Template-v0.2.0-preview.3.zip`: unbound universal template. Do not import it as a connected Plugin.
- `SHA256SUMS.txt`: SHA-256 of the six release files.
- this instruction, implementation report and sanitized test evidence.

The connected private Plugin ZIP is generated after local connection configuration. Nothing in the bundle is a developer config, task DB, credential or existing project.

## Scope and credential handling

Use a new empty installation root under `%USERPROFILE%\projects`; suggested name `ProjectRelay-preview3-trial-<timestamp>`. Retain the current live installation intact as rollback. No tasks, artifacts, snapshots, subscriptions or config are imported. Fresh-install demo tasks belong to the new installation.

Reuse only connection wiring: current tunnel ID, exact tunnel-client path, health port, credential-source type/path and app ID. Inspect only connection metadata/installation receipt/process ownership needed for this; do not print or independently read credential file contents. The normal product connection helper is authorized to read the chosen credential when needed for doctor/start. Never put it in logs, the package, a source file or a comparison scan. Ignore ambient API-key environment variables; do not alter them. Do not scan arbitrary private files.

## A. Offline Windows acceptance — keep the old service running

1. Expand the handoff in a new folder. Verify full Local and Template SHA with `Get-FileHash -Algorithm SHA256` against `SHA256SUMS.txt`; inspect that ZIP names/version match. Use a real installed Python 3.11+ executable, not a Microsoft Store alias. Record OS/Python/Git versions without secrets.

2. Extract the Local ZIP separately and set variables:

```powershell
$PythonExe = '<actual installed python.exe>'
$RelayTrial = Join-Path $env:USERPROFILE ('projects\ProjectRelay-preview3-trial-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$CandidateDir = '<extracted folder>\Project-Relay-Local-v0.2.0-preview.3'
$EvidenceDir = '<new separate private evidence directory>'

& $PythonExe "$CandidateDir\tools\verify_package.py" --package "$CandidateDir"
& $PythonExe "$CandidateDir\install.py" --root "$RelayTrial" --demo --plan
& $PythonExe "$CandidateDir\install.py" --root "$RelayTrial" --demo
& $PythonExe "$RelayTrial\maintenance\check_install.py" --root "$RelayTrial" --self-test --output "$EvidenceDir\install-check.json"
& $PythonExe "$RelayTrial\maintenance\smoke_test.py" --root "$RelayTrial" --project-id sandbox --output "$EvidenceDir\project-smoke.json"
```

After EVERY command, check `$LASTEXITCODE`; stop on failure. `check_install` should report package/core/actual MCP `0.2.0-preview.3`, 54 tools and 7 successful checks. Smoke has 5 successful checks, a clean demo Git tree and actual README SHA/HEAD. Independently recompute README SHA and Git HEAD. No live tunnel is started by these commands.

3. Run new runtime cases in the extracted package, keeping the current service running:

```powershell
Push-Location "$CandidateDir\project-workbench"
& $PythonExe -m unittest discover -s tests -p test_shared.py -v
& $PythonExe -m unittest discover -s tests -p test_task_progress.py -v
Pop-Location
```

Check exit codes immediately, even inside Push/Pop. These are temporary local fixtures, not real model calls. Shared tests: 18 cases (one POSIX fd-swap case skips on Windows); task tests: 9 cases. They must not operate on installed user config/projects. If a link privilege error occurs, the test uses an ordinary local junction. Any genuine failure blocks switching; return the full sanitized traceback.

## B. Bounded switch — only after A passes and the old installation is idle

1. Identify the currently running Relay installation, launcher and exact rollback command from its existing metadata. Do not assume its folder name. Confirm no queued/running/cancelling/interrupted tasks. If active or uncertain, stop this phase and report; do not cancel user work or delete locks.
2. Record old root/launcher/profile/health port/app ID and single-client state privately. Stop the old service using its product Ctrl+C route. Verify the tunnel client and owned facade exited, health port is closed and locks removed. Do not kill unrelated processes. Unexpected lock/stop failure: report, do not delete locks automatically.
3. Initialize the fresh installation with the same endpoint/tunnel wiring. Fill the variables from the verified existing metadata; do not paste them into a public report.

```powershell
$RelayTunnelId = '<existing tunnel ID>'
$RelayAppId = '<existing MCP app ID>'
$RelayClientExe = '<actual full tunnel-client path>'
$RelayHealthPort = <existing health port>
$RelayKeyFile = '<existing explicitly selected private key-file path>'

& $PythonExe "$RelayTrial\maintenance\connect_chatgpt.py" --root "$RelayTrial" configure --tunnel-id "$RelayTunnelId" --tunnel-client "$RelayClientExe" --health-port $RelayHealthPort --credential-file "$RelayKeyFile"
& $PythonExe "$RelayTrial\maintenance\connect_chatgpt.py" --root "$RelayTrial" preflight
& $PythonExe "$RelayTrial\maintenance\connect_chatgpt.py" --root "$RelayTrial" doctor
```

If the current source is a hidden prompt, omit `--credential-file` and let the human enter the key in the product's hidden prompt. Never convert a prompt to a new saved key without a specific user request. Check every exit code. Configure produces a fresh profile and Start-Relay.ps1 pointing to the new core. Do not copy the old profile/Start script.

4. Start `& "$RelayTrial\Start-Relay.ps1"` in a retained visible PowerShell window. Verify `/readyz` and `/livez` at the selected health port, exactly one local tunnel client, and MCP command/root pointing to the fresh installation. Do not close that window while serving. Confirm the old service remains stopped. Local observation does not establish ownership of remote clients on other devices.
5. Generate the matching private Plugin:

```powershell
& $PythonExe "$RelayTrial\maintenance\connect_chatgpt.py" --root "$RelayTrial" package --app-id "$RelayAppId"
```

Check exit 0, actual filename, full SHA, four ZIP members (plugin.json, .app.json, skill, LICENSE), matching preview.3 version and unchanged app identity. Rerun: it should be `unchanged` with identical bytes. Copy that private ZIP to Downloads for the user; do not publish it. Do not independently read credentials to scan for key equality.
6. Create a dedicated external folder `$env:USERPROFILE\RelayShared-preview3-trial`, outside installation/config/state/registered projects. Put `input.txt` containing a simple known UTF-8 string and a small ZIP with a CSV inside; record actual byte sizes/SHA locally. Do not grant folder access via the local operator or modify grant DB. ChatGPT must show the approval card and the human must approve it.

Switch failure: cleanly stop the new client, verify its port/locks, then restart the recorded old launcher. Never run both against the same tunnel. Preserve failed installation/evidence; no data deletion. Existing account Plugin need not change until the new Plugin is imported. Report whether switched or rolled back.

## C. Return one sanitized verification bundle

Include REPORT.md, command names/times/exit codes, actual Local/Template/private Plugin full SHA, install-check/project-smoke JSON, new test outputs, PID/health/root/version checks, actual shared fixture bytes/SHA and rollback command/location. Replace personal paths with `%USERPROFILE%`; exclude config, profiles, credentials, DB, private backups and all app/tunnel IDs from anything public. Keep private wiring metadata locally. Do not change source to make a failure pass.

Report separately:

- Windows offline installation/tests: PASS/FAIL.
- Tunnel switch/readiness/Plugin generation: PASS/FAIL/NOT_PERFORMED.
- ChatGPT new tools/card/bytes/archive: NOT_TESTED (development window + human trial).

Tell the user: update the existing account Plugin using the generated private ZIP, rescan the existing MCP app once, then return to the development conversation. This is the required initial refresh for the new tools; normal grants/revocation/archive operations thereafter need none.

## Live trial owned by ChatGPT development window

After user refresh, verify `open_workbench.runtime_version` is preview.3; list shared folders; request exact read/write scope for the prepared external folder; user approves card; retrieve/reconstruct/hash fixture bytes; create a new output ZIP; read it back and verify SHA; exercise stale overwrite protection; revoke and confirm subsequent file operations are blocked with originals preserved. Confirm no task was created by file exchange. Exercise task archive/restore and review counts separately. Keep the injected permission-failure result incomplete; do not reproduce an actual permission problem by changing ACLs. Do not declare Product ACCEPTED from fixture tests.
