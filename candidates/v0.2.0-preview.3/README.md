# Project Relay

**Local AI Project Runtime for ChatGPT, Codex and Claude**

Project Relay lets you stay in ChatGPT while working with an authorized local project. From the Project Relay plugin, ChatGPT can inspect local files and Git state, delegate heavier work to Codex or Claude, run approved project commands, bring generated artifacts back for review, and continue from those exact results.

> **Status:** `v0.2.0-preview.3` candidate adds shared folders, task archives and partial execution evidence. Earlier Windows installation and ChatGPT → Claude → R → ZIP evidence belongs to preview.1/preview.2. This candidate requires its own Windows and live ChatGPT verification; see [candidate evidence](docs/VERIFICATION_PREVIEW3.md).

## How It Fits with Existing Tools

For the user, the main working surface stays in the **Project Relay plugin inside ChatGPT**. The Local Runtime works behind that window to connect ChatGPT with the authorized project, Codex or Claude, approved project commands, and the artifacts those tasks produce.

```text
You stay in ChatGPT
       │
       ▼
Project Relay Plugin  ◄──────────────────────────────────────┐
       │                                                     │
       ▼                                                     │
Project Relay Local Runtime                                  │
       │                                                     │
       ├── Local project: read / search / edit / Git         │
       ├── Codex / Claude: delegated code work               │
       ├── Project runtime: R / approved commands            │
       └── Artifacts: CSV / models / plots / PDF / ZIP       │
       │                                                     │
       ▼                                                     │
Execution evidence + exact artifacts                         │
       │                                                     │
       └──► ChatGPT review → decide → follow up ─────────────┘
```

That loop is the product: **reasoning, local execution, evidence, review, and continuation stay connected without requiring the user to move between separate AI interfaces for every step.**

![Project Relay in use: ChatGPT conversation with the Project Relay task panel](https://github.com/yucheng-chang-yc/Project-Relay/raw/refs/heads/main/docs/images/project-relay-chatgpt-task-review.png)

*Project Relay in use: ChatGPT remains the working surface while local executor tasks, results, and review actions stay visible in the Project Relay panel.*

Project Relay does not replace the surrounding tools:

| Tool | Role |
| --- | --- |
| **ChatGPT + Project Relay Plugin** | Main working window for reasoning, orchestration, review, and follow-up |
| **Project Relay Local Runtime** | Connects ChatGPT to the authorized project, executors, commands, task state, and artifacts |
| **Codex / Claude** | Perform delegated code work, debugging, and execution |
| **R / project commands** | Run actual project computation under configured runtime rules |
| **Git / GitHub** | Durable version history, remote collaboration, CI, and releases |

GitHub can remain the durable remote without being the mandatory shuttle for every local AI-assisted iteration.

## Quick Start

For the packaged Preview on Windows:

```powershell
Expand-Archive .\Project-Relay-Local-v0.2.0-preview.3.zip -DestinationPath .\ProjectRelay-preview
Set-Location .\ProjectRelay-preview\Project-Relay-Local-v0.2.0-preview.3

$RelayRoot = Join-Path $env:USERPROFILE 'ProjectRelay'

python .\tools\verify_package.py --package .
python .\install.py --root "$RelayRoot" --demo
python "$RelayRoot\maintenance\check_install.py" --root "$RelayRoot" --self-test
python "$RelayRoot\maintenance\smoke_test.py" --root "$RelayRoot" --project-id sandbox
```

Then connect the Local Runtime to ChatGPT using [docs/CONNECT_CHATGPT.md](docs/CONNECT_CHATGPT.md).

> Windows note: if `python` resolves to the Microsoft Store alias rather than your Python 3.11+ installation, use `py -3` or the full Python executable path.

## Why Project Relay

The useful pieces already exist: ChatGPT can reason, Codex and Claude can work on code, and local tools can run analyses. The friction is between them.

| Workflow friction | What Project Relay changes |
| --- | --- |
| ChatGPT cannot reliably act on the current local project | Gives controlled access to authorized local files, metadata, Git state, and task evidence |
| Users repeatedly copy instructions between ChatGPT and local executors | Turns executor work into tracked tasks with explicit inputs, results, logs, and follow-up lineage |
| Results come back as loose files or summaries | Captures artifacts with identity, size, SHA-256, and task provenance |
| Follow-up work loses the exact model, data, plot, or bundle from the previous run | Lets later tasks reuse prior artifacts directly |
| GitHub becomes a required shuttle between web AI and local work | Keeps routine execution local-first while GitHub remains the durable remote |
| “The task finished” becomes “the result is accepted” | Separates execution, evidence retrieval, review, and integration |

The goal is simple: **the human should make project decisions, not act as a file courier, completion notifier, or context relay between AI tools.**

## What the Preview Can Do

From ChatGPT, Project Relay can currently support the core loop:

- read, search, and edit authorized local project files;
- inspect directories, metadata, Git status, and repository identity;
- delegate heavier work to Codex or Claude;
- run approved project commands such as R workflows;
- capture logs and artifacts such as CSV, model objects, plots, PDFs, and ZIPs;
- reuse exact prior artifacts as inputs to later tasks;
- keep executor completion separate from review and human acceptance.

### Validated computational workflow

In an earlier private runtime trial, Project Relay was tested with a deliberately failing R analysis:

```text
ChatGPT
  → stage exact project input
  → Claude task
  → run Rscript
  → capture intentional failure
  → inspect stderr
  → repair the script
  → rerun successfully
  → produce CSV + RDS + PNG + ZIP
  → capture artifact hashes
  → return the ZIP to ChatGPT byte-identically
  → run a follow-up task using prior artifacts
```

The resulting ZIP matched the local artifact SHA-256, and a later task reused the prior model and result without a manual download/re-upload cycle.

This is the product proof behind Project Relay. It is **integration evidence from the earlier runtime trial**. A separate `v0.2.0-preview.1` clean installation also completed ChatGPT → Claude → R → CSV/RDS/PNG/ZIP retrieval with independent hash checks. That release trial did not repeat the earlier follow-up task; neither result establishes that every installation will work.

## Product Structure

Project Relay has two layers.

### Local Runtime

Runs on the user's machine and handles authorized project access, executor tasks, computational commands, artifacts, task state, and integration controls.

### ChatGPT Plugin

Provides the ChatGPT-facing tools and UI used to inspect projects, dispatch tasks, retrieve results, review evidence, and continue work.

The reusable Local Runtime and Plugin Template are packaged separately. The connected Plugin ZIP is generated for an individual registered MCP app; it contains app mapping, not user credentials.

ChatGPT connects through that app's stable OpenAI tunnel binding; the local tunnel-client launches the runtime over stdio. The Plugin contains no per-session URL or secret. Restarting the same installation/tunnel/app preserves the binding; a new app ID requires regeneration and an account Plugin update.

The current Preview connection route is:

```text
Local installation
→ user-created OpenAI Secure MCP Tunnel
→ ChatGPT developer-mode app
→ generated Project Relay Plugin ZIP
→ Plugin Creator import/update
```

See [docs/CONNECT_CHATGPT.md](docs/CONNECT_CHATGPT.md) for the release-specific connection procedure.

## Getting Started

### Requirements

| Capability | Requirement |
| --- | --- |
| Windows Local Runtime / snapshots | Python 3.11+; Windows 10/11 target, Windows 11 tested |
| Registered Git projects / demo tasks | Git and a committed HEAD at the exact project root |
| ChatGPT connection | Eligible developer-mode MCP connection plus OpenAI Secure MCP Tunnel |
| Claude tasks | Installed and authenticated Claude Code |
| Codex tasks | Installed and authenticated Codex CLI |
| R computation | Optional `Rscript`, required packages, and explicit native-execution opt-in |
| JavaScript UI tests | Node, when needed |

Claude, Codex, and R are optional for direct project inspection and the supplied smoke test.

### Install

The Quick Start above installs into `%USERPROFILE%\ProjectRelay` by default and creates an isolated committed demo project named `sandbox`.

For a custom empty root:

```powershell
python .\install.py --root "C:\Path\To\ProjectRelay" --demo
```

For shared-folder or single-file exchange without a Git project, omit `--demo`.

### Connect ChatGPT

Use the connection helper and the full instructions in [docs/CONNECT_CHATGPT.md](docs/CONNECT_CHATGPT.md).

At a high level:

```text
configure the tunnel/app binding
→ run doctor
→ start Project Relay
→ register the MCP app in ChatGPT
→ generate the Project Relay Plugin ZIP
→ import/update it with Plugin Creator
→ run the account smoke prompt
```

The normal connected workflow stays in the ChatGPT conversation. The tunnel/runtime terminal must remain running in this Preview.

After initial configuration, start with `& "$RelayRoot\Start-Relay.ps1"`; stop with Ctrl+C and confirm the client has exited before restarting or rolling back. Tunnel health defaults to `http://127.0.0.1:8080/readyz`; choose a free `--health-port` **on the first configure** if 8080 is busy. This is separate from the optional local HTTP API on port 8765.

Choose a hidden prompt or an explicit private key file when configuring. Ambient `CONTROL_PLANE_API_KEY` is ignored. Keep credentials outside registered projects. See [connection and daily operation](docs/CONNECT_CHATGPT.md) and [rollback](docs/INSTALL.md#rollback).

### Register a project

Register an existing committed Git root:

```powershell
python "$RelayRoot\maintenance\register_project.py" --root "$RelayRoot" --project "C:\Projects\my-analysis" --project-id my-analysis
```

Registration is read-only by default; add `--writable` to allow edits/tasks. Restart Project Relay after local configuration changes.

### Shared folder access

Create a dedicated folder such as `C:\RelayShared`, then ask ChatGPT to request read-only or read/write access to that exact folder. Approve the displayed scope in the inline card. Access includes current and future descendants until revoked; no Git or project registration is needed. Use **Shared folders** in the Tasks view to revoke access. See [shared folders and task improvements](docs/PREVIEW3_CHANGES.md) for exact limits and trust boundaries.

### Account smoke test

With Project Relay enabled in ChatGPT, ask:

> List registered projects. For `sandbox`, read `README.md`, show Git status, return project capabilities, and run `preflight_task` with executor `command` and profile `smoke`. Do not create a task or edit files.

This verifies basic connectivity and project access. Executor authentication and computational E2E are separate checks.

## Safety and Trust Model

Project Relay deliberately separates local execution from review and integration.

Key Preview boundaries:

- project APIs are restricted to registered roots and reject path traversal/link escapes;
- writes and tasks require explicit writable registration;
- Codex/Claude tasks use detached worktrees where applicable;
- unrestricted Claude Bash/PowerShell is not exposed by default;
- native computation uses configured command/runtime rules and bounded task controls;
- task completion does not automatically mean review or human acceptance;
- `.env`, gitignored files, and other in-root secrets are **not automatically excluded** from authorized reads.

> **Important:** the current Windows computational runtime is a **trusted local execution mode**, not an OS-level sandbox. Native R and other approved local processes still run with the user's local-account privileges.

Project Relay itself does not provide a cloud credential store or a default Project Relay telemetry/upload service. Explicit ChatGPT reads, AI-provider calls, Git operations, tunnel traffic, events, and native tools may still use the network.

Tunnel authentication and the optional installation-wide local HTTP token are separate. Protect local config, state, logs and credential files; the Preview has no encrypted credential vault. See [docs/SECURITY.md](docs/SECURITY.md) for authentication, network, path and command boundaries.

> Project Relay Preview is intended for use with projects and executors you trust on your own machine. The current Windows computational runtime does not provide OS-level containment.

## Current Preview Scope

The strongest validated product path is:

```text
ChatGPT
→ authorized local project
→ Codex / Claude or approved runtime
→ exact artifacts
→ ChatGPT review
→ follow-up from prior results
```

Windows installation, private Plugin binding, ChatGPT project inspection, a short task, and the Claude/R artifact loop passed on `v0.2.0-preview.1`. The shutdown patch passed real Ctrl+C/Ctrl+Break stop/restart checks. For `v0.2.0-preview.3`, exact-ZIP Windows installation and private Plugin import passed. Live project smoke used the retained `.1 + D-004` service; the isolated `.2` installation was checked over stdio and has not taken over the live tunnel. Tasks-card visual behavior, originating-conversation Events continuation and snapshots above 97,520 bytes are not yet verified. See [verification scope](docs/VERIFICATION.md).

Still being refined:

- simpler project onboarding and bootstrap;
- richer project re-entry/session continuity;
- read-only task concurrency;
- stronger Windows containment;
- smoother task-completion handoff to the originating conversation.

Project Relay is not an IDE, a replacement for Codex or Claude, a replacement for GitHub, or a generic unrestricted remote shell.

## Release Packaging

The release candidate contains:

| Artifact | Purpose |
| --- | --- |
| `Project-Relay-Local-v0.2.0-preview.3.zip` | Reusable runtime, installer, connection tools, and documentation |
| `Project-Relay-Plugin-Template-v0.2.0-preview.3.zip` | Unbound manifest/skill template with no user app mapping or credentials |
| `SHA256SUMS.txt` | SHA-256 of the release ZIPs and accompanying files |
| `README.md`, `INSTALL.md`, `RELEASE_NOTES.md` | Product, setup, and release information |
| `LICENSE` | MIT license for Project Relay source |

The account-specific connected Plugin ZIP is generated locally after MCP app registration.

Verify downloads before extraction:

```powershell
Get-FileHash .\Project-Relay-Local-v0.2.0-preview.3.zip -Algorithm SHA256
Get-FileHash .\Project-Relay-Plugin-Template-v0.2.0-preview.3.zip -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

Preview upgrades use a new empty installation root; automatic migration and in-place updates are not provided. Configuration, registered projects, task DB and snapshots are not imported. Retain the old installation for [rollback](docs/INSTALL.md#rollback) until the new one is verified. Update the existing account Plugin through Plugin Creator for a new version; a runtime restart with the same tunnel/app needs no re-import.

The matching candidate is:

```text
Local package / Plugin: 0.2.0-preview.3
Bundled runtime core:    0.2.0-preview.3
```

Preview validation targets matching Local Runtime package and Plugin versions. Other compatibility combinations are not currently established. Preview.3 uses matching package, core and Plugin identities. Its Windows and live-host acceptance status is recorded separately in [candidate evidence](docs/VERIFICATION_PREVIEW3.md).

## Development and Release Status

Project Relay is an MVP / Preview. API/config changes and clean reinstallation may still occur; production-ready status and backward compatibility are not claimed.

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for source layout, reproducible build/test commands, dependencies and contribution policy. Project Relay source is released under the [MIT LICENSE](LICENSE). Python, Git, tunnel-client, Codex, Claude and R are external prerequisites; their executables are not bundled.

Internal `project-workbench` / `workbench-mcp` identities remain for compatibility. Exact installation and account evidence is summarized in [docs/VERIFICATION.md](docs/VERIFICATION.md).

---

**Project Relay**  
*Local AI Project Runtime for ChatGPT, Codex and Claude*
