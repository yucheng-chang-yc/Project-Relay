# Connect Project Relay to ChatGPT

Local Pack and Plugin Template are universal source artifacts. A connected Plugin ZIP is generated for your actual registered ChatGPT app. It contains the dependency ID, workflow skill and MIT license notice, with no token, tunnel key, runtime files or device path. Keep the generated package private.

## 1. Create a private tunnel

Use the official [Secure MCP Tunnel setup](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) to create a tunnel in OpenAI Platform and obtain its tunnel ID and runtime API key. Install the **full official tunnel-client** with `init`, `doctor`, `run`; its runtime-only build is insufficient for bootstrap. Project Relay does not provision cloud resources or retrieve credentials.

The route uses outbound HTTPS and local stdio. No per-session public URL goes in a manifest. The ChatGPT app uses a stable tunnel ID; the Plugin uses that app's ID. Public ChatGPT directory submission needs a separate supported architecture.

## 2. Configure the installed client

After [INSTALL.md](INSTALL.md)'s local checks, choose the credential source **before your first configure**. The commands below use hidden prompts for doctor/startup. For key-file startup, append `--credential-file "C:\path\to\private-runtime-key.txt"` to the configure command before running it. Keep the existing key file outside registered projects. This Preview preserves the first binding rather than changing its credential source later. Then set these nonsecret values in PowerShell:

```powershell
$RelayRoot = Join-Path $env:USERPROFILE 'ProjectRelay'
$RelayTunnelId = Read-Host 'Tunnel ID from OpenAI Platform'
$RelayTunnelClient = (Get-Command tunnel-client.exe -ErrorAction Stop).Source
python "$RelayRoot\maintenance\connect_chatgpt.py" --root "$RelayRoot" configure --tunnel-id $RelayTunnelId --tunnel-client $RelayTunnelClient
python "$RelayRoot\maintenance\connect_chatgpt.py" --root "$RelayRoot" doctor
& "$RelayRoot\Start-Relay.ps1"
```

If absent from PATH, set `$RelayTunnelClient` to its exact `.exe` path; shell wrappers are rejected. `configure` writes a profile referencing `env:CONTROL_PLANE_API_KEY`, with no key value. `doctor`/startup request hidden input by default and ignore inherited `CONTROL_PLANE_API_KEY`. With the key-file option chosen above, the helper records only the explicit path, reads it afresh at each start, and injects its value into the tunnel child environment. A missing/invalid file fails with no fallback. Keep the file outside registered projects and protect it with local account permissions. Never put it in commands, source or Plugin files. Keep startup open; Ctrl+C requests shutdown and attempts process-tree termination if the client remains active. Before takeover or rollback, verify the client and its children have exited.

Binding is stored in `connection\chatgpt.json`, profiles in `connection\tunnel-profiles`. Installed files, profile hashes and ownership locks are checked. Identical configuration preserves its profile. A changed tunnel/client path, explicit key-file path, health port or modified profile is rejected; rebind via a fresh installation in this Preview. Rotate keys by replacing contents at the same configured path while stopped. `--health-port` chooses the fixed loopback health port (default 8080).

A successful `doctor` exit checks client diagnostics, not app ownership or ChatGPT rendering. It can complete while an old client runs and reports the observed PIDs. The `preflight` command checks startup ownership without reading a key or starting a client; while an old client is active, a blocked result is expected. Raw client output is omitted to avoid displaying credentials.

## Before starting a replacement

Run the credential-free real-client path probe from the installed maintenance directory:

```powershell
python "$RelayRoot\maintenance\test_tunnel_init.py" --root "$RelayRoot" --tunnel-client "$RelayTunnelClient" --output .\tunnel-init-probe.json
python "$RelayRoot\maintenance\connect_chatgpt.py" --root "$RelayRoot" preflight
```

The probe invokes real `init` with synthetic tunnel identity in temporary profiles. It checks the installed command and paths with spaces, Unicode and apostrophes; it starts no runtime and contacts no tunnel. Temporary interpreter copies are used only for executable-path preflight.

Before takeover, finish/recover the old installation's tasks, record its rollback command, stop its client and verify it exited. A local active client on another health port still blocks startup when its tunnel binding cannot be established. Re-run preflight and then Start-Relay. The ownership guard covers cooperating Relay launchers on this host; a legacy script starting after the scan or a client on another device requires operator coordination. No client is killed automatically.

## 3. Register the ChatGPT app

In an eligible account/workspace, enable developer mode, create a private MCP app with the **same tunnel ID**, attach it to a conversation and inspect discovery. Obtain the actual registered app identifier. See [official connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt) for current UI/eligibility.

The generator accepts `asdk_app_` plus 32 lowercase hex characters, its `plugin_asdk_app_` form, or a ChatGPT `/apps/` or `/plugins/` URL with exactly one such ID. Unrelated `plugins_` aliases are rejected. Format validation cannot establish that an app exists or belongs to you.

## 4. Generate and import the Plugin

In another terminal while startup runs:

```powershell
$RelayRoot = Join-Path $env:USERPROFILE 'ProjectRelay'
$RelayAppId = Read-Host 'Registered ChatGPT app ID or app URL'
python "$RelayRoot\maintenance\connect_chatgpt.py" --root "$RelayRoot" package --app-id $RelayAppId
```

Import `connection\generated\Project-Relay-Plugin-v0.2.0-preview.2.zip` from your installation root using **Plugin Creator**. Complete account binding/review. Update an existing Project Relay/Project Workbench plugin identity when available. The stable internal name is `project-workbench` and app key `workbench-mcp`. `.app.json` is generated automatically; no JSON editing/re-zipping is needed. See [official packaging instructions](https://developers.openai.com/plugins/build/plugins).

Identical generation preserves the package. Use `package --app-id <new-actual-id> --replace` when deliberately changing binding, then update the account plugin. The generated ZIP has four files: `plugin.json`, `.app.json`, `skills/project-workbench/SKILL.md` and `LICENSE`. The universal Template is source material, not a connected Plugin package.

## 5. Verify in ChatGPT

Ask: “Use Project Relay to list projects, read sandbox/README.md, show sandbox Git status and return sandbox capabilities. Do not start a task.” Tool arguments use project ID `sandbox` and relative path `README.md`. Compare with your local smoke receipt. The optional Tasks panel displays results/logs inline in the selected task. Follow [ACCEPTANCE.md](ACCEPTANCE.md) for actual task/UI/ZIP acceptance.

## Health port and daily operation

Tunnel health binds to `127.0.0.1:8080` by default. Check whether your chosen port is occupied **before the first configure**:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue
```

If occupied, append `--health-port 8092` (or another free port) to that first configure command. The saved health port cannot be changed by rerunning configure; use a fresh installation for a different binding in this Preview. This health listener belongs to tunnel-client. Optional `Start-HTTP.ps1` uses a separate loopback API port, default 8765, and is unnecessary for the stdio tunnel route.

With the same configured root, startup is:

```powershell
$RelayRoot = Join-Path $env:USERPROFILE 'ProjectRelay'
& "$RelayRoot\Start-Relay.ps1"
```

Keep that terminal open. Prompt mode asks for the runtime key on each doctor/start; explicit file mode reads the selected file afresh. Neither route uses an ambient key. In a second terminal, use the **saved health port**:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/readyz'
Invoke-RestMethod 'http://127.0.0.1:8080/livez'
```

Ready/live is a local client check; verify ChatGPT tools separately. To stop, press Ctrl+C once in the service terminal and allow shutdown to finish. D-004 Windows testing observed all processes/locks gone after 9.5 seconds; that is an observation, not a maximum-time guarantee. Real Ctrl+Break also passed. Verify the client/children exited and the health port closed before restart or rollback. Closing/killing the terminal may leave recovery work.

An abandoned `state\facade.lock` or `connection\tunnel-process.lock` is preserved for inspection. Use the error's recovery guidance, inspect the recorded PIDs and children plus the health listener, and remove a lock manually only after establishing the owning processes are gone. Do not bypass an active or uncertain owner. See [rollback](INSTALL.md#rollback).

The external full tunnel-client may start its own Codex app-server companion. That upstream behavior is separate from Relay executor configuration; no-Codex-host behavior has not yet been tested on Windows.

## Restart and upgrade

The same installation/tunnel/app reuses its Plugin after restart. Run one client for a tunnel. A changed app ID requires package regeneration/account update. A changed installation/tunnel/client requires a fresh bootstrap here; account configuration must point at the intended tunnel. Preview validation targets matching Local/Plugin versions. The `.1` host pairing passed; the `.2` private account Plugin update/readback and live project smoke also passed on the retained baseline service. A Plugin update uses the existing account identity through Plugin Creator, not a new app by default.

## Troubleshooting

| Symptom | Inspect |
|---|---|
| Local check fails | Sanitized receipt, installed hashes/config |
| Bootstrap rejects binding | Full client, ownership locks, immutable profile |
| Client exits/not ready | Official diagnostics, network/key permissions |
| Old tools appear | App tunnel ID, actual startup command, refresh discovery |
| Widget will not open | Host error plus server resource diagnostics |
| Task completes without reply | Stored result and Events subscription/notification chat |

See [VERIFICATION.md](VERIFICATION.md): `.1` real Windows bootstrap and the ChatGPT/Claude/R path passed, and D-004 shutdown passed separately. `.2` carries those changes with refreshed release metadata and licensing; its exact ZIP Windows check and private Plugin import both passed. The live app remains on the retained `.1 + D-004` installation; the isolated `.2` root was not started on the live tunnel.
