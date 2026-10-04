# Fresh-install acceptance

Use the exact Local ZIP, extracted to a new directory. Keep the normal installation and tunnel unchanged until these checks finish. Python 3.11+ and Git must be available. This trial has its own config, token, state and demo Git repository.

## Windows local smoke

From the extracted release directory in PowerShell:

```powershell
.\Test-FreshInstall.ps1
```

The script creates an isolated directory under the current user's temporary directory, installs only this package, runs the installation self-test, and reads the demo README/Git status/capabilities/preflight through a real stdio MCP process. It returns report paths and keeps the test installation for inspection. PASS covers those local checks; it does not indicate a connected ChatGPT account or a Claude/R run.

An alternative Python invocation from the same release directory is:

```powershell
$RelayTrialRoot = Join-Path $env:TEMP 'ProjectRelay-clean-trial'
python .\install.py --root "$RelayTrialRoot" --demo
python "$RelayTrialRoot\maintenance\check_install.py" --root "$RelayTrialRoot" --self-test
python "$RelayTrialRoot\maintenance\smoke_test.py" --root "$RelayTrialRoot" --project-id sandbox
```

Choose a new empty target if that trial directory already exists.

## Real tunnel-client init and startup ownership

After local checks, before switching services:

```powershell
python "$RelayTrialRoot\maintenance\test_tunnel_init.py" --root "$RelayTrialRoot" --tunnel-client "$RelayTunnelClient" --output .\tunnel-init-probe.json
```

This credential-free offline probe uses the real full binary and temporary synthetic profiles. Require all three path cases PASS. Configure this installation with an explicitly selected `--credential-file` for noninteractive startup, or use hidden prompts. Existing inherited API-key values are ignored.

Run `connect_chatgpt.py --root <root> preflight` while the old client is active: expect refusal with a PID, with no client started. `doctor` may pass while reporting that active client. Stop the old client only after its tasks/state are clear and rollback details are recorded, then require preflight PASS and start Relay. Verify the fixed health port and actual target. After restart/key-file rotation, repeat diagnostics.

## Private ChatGPT connection

Follow the README and CONNECT_CHATGPT.md using the test installation and a tunnel/account connection authorized for the trial. Keep a single client for that tunnel. Run doctor, start the client, register the app in ChatGPT, generate the app-bound Plugin ZIP, and import or update the private plugin with Plugin Creator.

In a new conversation, enable Project Relay and ask:

> List the registered projects. For sandbox, read README.md, show Git status, and return project capabilities. Run preflight_task for executor command and profile smoke. Do not edit files or start an executor.

Match the project/README/Git answers to the local smoke receipt. Open the Tasks card and verify light/dark, narrow layouts, and results/logs expanding inside the selected task. This is host acceptance of the exact UI revision, not a source-only assertion.

## One short real task

Ask Project Relay to start the configured smoke command on sandbox, with a new idempotency key and a 30-second timeout. Retrieve get_task and get_task_result until terminal, then check actual exit_code 0 and result_sha256. Do not record execution completion as accepted project changes. No Codex, Claude or R installation is required for this configured Python smoke command.

## Optional computation and file checks

After local executor login and native R configuration, repeat the R collaboration loop with exact staged input, intentional failure/stderr, a corrected rerun, CSV/RDS/PNG/ZIP outputs, independently verified artifact bytes and a follow-up using a prior artifact. Current test_native_r.py can check the local command runtime; it does not automate a real Claude/ChatGPT evaluation.

For snapshot-only use, test one approved outside-project file and one denied request. Retrieve the approved bytes and independently verify the whole-file SHA. Check same-key retry and original-file changes without broadening the approval.

## Evidence to retain

Keep sanitized install/smoke reports, release SHA, platform/Python/Git versions, actual tunnel/client readiness, app discovery and selected tool results, one task/result SHA, and explicit host/computation outcomes. Keep all tokens, runtime keys, account identifiers and private project contents out of public evidence. Mark any step not performed as NOT_TESTED.
