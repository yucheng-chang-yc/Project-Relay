# Install Project Relay v0.2.0-preview.3

Target: Windows 10/11, one local account per installation. Windows 11 installation/bootstrap/ChatGPT/Claude/R passed on `.1`, and the included D-004 shutdown change passed on Windows. The exact `.2` ZIP still needs Windows fresh-install acceptance; Windows 10 has not been separately validated. The installer/runtime are also exercised on Linux. See [VERIFICATION.md](VERIFICATION.md).

## Requirements

| Use | Requirement |
|---|---|
| Local Runtime and file snapshots | Python 3.11+ on PATH |
| Registered projects, demo and tasks | Git on PATH; project has an initial commit |
| ChatGPT connection | Developer mode, Secure MCP Tunnel access and Plugin Creator; full official tunnel-client with `init`, `doctor`, `run` |
| Codex tasks | Separately installed/authenticated Codex CLI; Node only if that CLI installation requires it |
| Claude tasks | Separately installed/authenticated Claude Code 2.1.248+ |
| R computations | Optional Rscript, packages your script needs and explicit native execution opt-in |

These binaries are external prerequisites. The Local Runtime has no third-party Python runtime dependencies. Private tunnel access uses outbound HTTPS to OpenAI; no inbound public HTTP port is needed for stdio. Tunnel health uses a fixed loopback port, default 8080; choose `--health-port` on first configure if needed. Optional local HTTP uses configurable loopback port 8765.

## Verify and install

Compare the complete ZIP hash with `SHA256SUMS.txt` from the same candidate:

```powershell
Get-FileHash .\Project-Relay-Local-v0.2.0-preview.3.zip -Algorithm SHA256
Expand-Archive .\Project-Relay-Local-v0.2.0-preview.3.zip -DestinationPath .\relay-unpacked
Set-Location .\relay-unpacked\Project-Relay-Local-v0.2.0-preview.3
$RelayRoot = Join-Path $env:USERPROFILE 'ProjectRelay'
python .\install.py --root "$RelayRoot" --plan --demo
python .\install.py --root "$RelayRoot" --demo
python "$RelayRoot\maintenance\check_install.py" --root "$RelayRoot" --self-test
python "$RelayRoot\maintenance\smoke_test.py" --root "$RelayRoot" --project-id sandbox
```

`Install.ps1 -Root <empty-root> -PythonExe <python.exe> -Demo` wraps the same installer. Omit `--demo` for snapshots without a project. Existing installations are preserved. The optional demo creates a committed Git repository and fixed Python smoke profile. These checks start/close a local MCP process; they do not run an AI task or verify ChatGPT.

The installer verifies its source manifest before writing and creates fresh configuration, token and state. It imports no old credentials, project registry, tasks, subscriptions or snapshots. Use your normal local account.

## Installed layout

| Path under the installation root | Contents |
|---|---|
| `app\0.2.0-spike.4\project-workbench` | Versioned runtime and tests |
| `maintenance` | Integrity, launch, registration, smoke and bootstrap tools |
| `config\config.json` | Settings and local HTTP token |
| `state` | SQLite, task outputs, snapshots and logs |
| `projects\demo` | Optional demo Git project |
| `connection\stdio.json`, `mcp-command.txt` | Installed MCP command |
| `connection\chatgpt.json`, `tunnel-profiles` | Binding and profiles after configuration |
| `connection\generated` | Private per-app Plugin ZIP after generation |
| `docs` | Installed instructions |
| `LICENSE` | MIT notice, also retained in the installed runtime |

## Start and connect

Follow [CONNECT_CHATGPT.md](CONNECT_CHATGPT.md). Its helper configures the external tunnel-client, creates `Start-Relay.ps1`, and generates your Plugin ZIP from your registered app ID. Account registration/import are manual Preview steps. Run one facade per installation; do not start `Start-HTTP.ps1` or `Start-MCP.ps1` alongside the tunnel.

Local-only `Start-HTTP.ps1` launches authenticated loopback HTTP; `Start-MCP.ps1` launches stdio. Neither creates a ChatGPT connection. Ctrl+C stops foreground launchers. After a crash, inspect recorded PIDs/children before manually removing ownership locks.

## Register a project

Stop this facade and resolve active/uncertain tasks. Supply an exact Git root with an initial commit:

```powershell
python "$RelayRoot\maintenance\register_project.py" --root "$RelayRoot" --project "C:\Projects\my-analysis" --project-id analysis
```

Read access is the default. Add `--writable` to authorize changes and tasks. CLI discovery does not log in or prove provider authentication. Settings live in `config\config.json`; existing registrations are not silently replaced. Change access locally while idle. Project and runtime/state directories remain separate.

For native R, add both `--rscript "C:\path\to\Rscript.exe" --ack-native-r` during registration. R then runs with the local account's rights and `os_containment=false`. Trust the scripts you run. R is never enabled merely because it was detected.

## Optional host-file input

For host URL staging, explicitly allow the exact HTTPS host in `binary_transfer.allowed_download_hosts` while idle. The default empty list blocks downloads. Keep signed URLs/credentials private. Host transport support needs separate acceptance.

## Upgrade and removal

Preview releases may require a clean reinstall. This installer refuses a nonempty target and provides no update/state migration/uninstall operation. Keep the old installation stopped for rollback. Config, registrations, task DB, snapshots and subscriptions are not copied automatically. Only matching Local/Plugin versions are tested. The internal legacy 0.1.x updater is not the v0.2.0-preview.3 upgrade route.

## Rollback

Before replacing a working installation, record its startup command, root, app/tunnel mapping and credential source; retain its files/state. Avoid outstanding or uncertain tasks. This is an explicit operator procedure, not an automatic migration.

1. Stop the new installation with Ctrl+C. Confirm its tunnel-client and children have exited and its health port is closed.
2. Start the retained installation with **its own** saved launcher/configuration. Keep one client for the tunnel; never run old and new clients simultaneously.
3. If account app/tunnel mapping changed, restore the previous connection and Plugin version through the same account tools used to connect it. Verify project identity and a read-only smoke before resuming work.

The new installer does not alter the old configuration, task DB or projects. A rollback does not move new tasks/artifacts into the old installation. Retained in-progress state needs local recovery before switching; a Git worktree is not portable task-state migration.

## Credentials and licensing

Protect `config/config.json`, state/logs/backups, CLI authentication and any explicit tunnel-key file with local account permissions. The optional local HTTP token is installation-wide and stored in plaintext config; the tunnel runtime key is separately created/rotated in OpenAI Platform and supplied via hidden prompt or explicit private file. See [SECURITY.md](SECURITY.md). Source is MIT-licensed; `LICENSE` is included in the Local Pack, installation and Plugin artifacts. External tools/binaries are not bundled.

## Preview.3 additions

Shared-folder-only use needs Python and the ChatGPT connection, without Git or executors. Omit `--demo` to install without projects. Folder grants cover current/future contents until revoked. New grants, revocation and archives are live operations; initial deployment of these new tools requires a restart and MCP rescan. See [PREVIEW3_CHANGES.md](PREVIEW3_CHANGES.md).
