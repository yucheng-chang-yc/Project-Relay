# Project Relay v0.2.0-preview.2

Preview release integration candidate. This revision consolidates the Windows-tested `.1` bootstrap, Windows-tested D-004 shutdown maintenance, frozen product README and MIT distribution notices.

## Changes

- D-001: shared Windows command encoding fixes tunnel init, including spaces, Unicode and apostrophes.
- D-002: cross-installation local ownership inspection refuses conflicting tunnel clients; a different health port does not bypass it.
- D-003: explicit private key file or hidden prompt; inherited `CONTROL_PLANE_API_KEY` is ignored.
- D-004: restore inherited Windows console Ctrl+C handling, handle Ctrl+Break, and release facade ownership after confirmed child exit; repeat interrupts do not bypass cleanup. Installation checks distinguish package/core versions and configured Rscript from PATH discovery.
- English neutral light/dark UI with selected-task results/logs and conversation-specific file approval, retained from `.1`; visual host acceptance remains separate.
- Product README uses the frozen trimmed narrative, with setup/security/development detail in `docs/`.
- MIT LICENSE included in source, installed runtime, universal Template, generated private Plugin ZIP and accompanying release files.

Local Pack / Plugin Template / generated Plugin: `0.2.0-preview.2`. Bundled runtime core: `0.2.0-spike.4`. UI revision: `0.2.0-ui.3`. Internal `project-workbench` and `workbench-mcp` identities are retained.

## Evidence and limits

`v0.2.0-preview.1` passed Windows 11 fresh installation, full-client init path cases, ownership/key-source behavior, readiness and private Plugin generation. ChatGPT then inspected the actual projects and completed a short task and a Claude/R calculation; independently retrieved ZIP bytes and output checks matched, and the main project Git state was unchanged. D-004 subsequently passed real Windows Ctrl+C and Ctrl+Break shutdown/restart without manual lock deletion after patching.

The `.2` core computation and UI bytes remain unchanged from `.1`; shutdown support retains the D-004 implementation. New package/Plugin version metadata, LICENSE distribution and documentation are checked on the candidate itself. The exact `.2` ZIP passed Windows fresh installation and its private Plugin update was accepted, read back and followed by a live project smoke against the retained `.1 + D-004` service. The isolated `.2` installation did not take over the live tunnel. This is inherited functional evidence plus candidate packaging checks, not a claim that `.2` completed another full host E2E. See [VERIFICATION.md](VERIFICATION.md).

Unverified items include an isolated `.2` live-tunnel takeover, Tasks-card visual/scroll behavior, automatic continuation in the originating Events conversation, snapshot expiry and snapshot transfers above 97,520 bytes, other Windows versions and a no-Codex Windows host. No OS-level Windows containment is claimed.

## Install, upgrade and rollback

Install into an empty directory. Config, project registration, task database, snapshots and subscriptions are not imported automatically. Retain the stopped old installation for rollback; follow [INSTALL.md](INSTALL.md) and [CONNECT_CHATGPT.md](CONNECT_CHATGPT.md). No in-place update or backward compatibility guarantee is provided. Preview validation targets matching Local/Plugin versions; other combinations are not established.

## Distribution

Project Relay source is MIT-licensed. Python/Git/R/Node/agent/tunnel executables are external prerequisites, not bundled binaries. Local and Template artifacts are reusable; the connected Plugin is generated for a particular registered MCP app and kept private. OpenAI Platform tunnel creation and ChatGPT developer-mode app/Plugin Creator steps remain manual Preview setup. A GitHub release does not constitute public ChatGPT directory approval.
