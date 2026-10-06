# Capabilities and known limits

Runtime baseline: `0.2.0-spike.4`. Release Integration Candidate: `0.2.0-preview.2`. Installation and connection tooling are versioned with this Preview.

## Available capabilities

- Registered project reads/search/writes, metadata and Git operations with scoped review/integration.
- Durable command, Codex and Claude tasks with idempotency, worktrees, saved logs/results and artifacts.
- Computational input/artifact lineage and explicitly configured container/native R routes.
- Exact binary windows with chunk and whole-file hashes.
- Single-file snapshots outside registered projects, with a separate user decision for each file/request.
- MCP task UI and webhook `task.finished` Events.

## Existing real-host evidence

The source runtime was tested locally on Windows (93 tests: 91 passed, 2 POSIX-only skipped, as reported by the local operator). Existing live tests covered Codex/Claude execution and native R. A regular ChatGPT chat verified:

| Test | Result |
|---|---|
| Outside-project ZIP | 97,520 bytes, independently matched SHA-256 and successful extraction |
| Source modified after capture | Snapshot bytes and hash stayed unchanged |
| Same-key retry | Existing snapshot reused |
| Denial | Request denied; no snapshot created |
| Events notification | One completed task produced a reply in a separate subscription-bound notification conversation |

The denied request was a later replacement test after an earlier file was accidentally approved; the final denied state was checked independently. The clean package contains generalized results without private device/account identifiers. Detailed original evidence remains in the prior private package.

## Known limits

- Windows fresh-install and new connection acceptance for this exact package require the supplied local acceptance procedure. Linux checks do not prove Windows PowerShell or ChatGPT rendering.
- Snapshot approval cards in a Work conversation, files larger than 97,520 bytes, and real ChatGPT expiry after 24 hours remain unverified.
- The configured snapshot size ceiling is 20 MiB; this is a server limit, not a tested ChatGPT capacity claim.
- Events automatically continuing the original working conversation remain unverified. A separate notification conversation has one real success; long-term reliability is not established.
- CLI discovery does not prove local login, model availability or a successful agent task.
- Worktrees/tool permissions are not an OS sandbox. Native R explicitly uses local-account permissions.
- Separate installations do not share writer coordination, task history, subscriptions or state.
- Private account setup depends on the user's available developer-mode/tunnel permissions.

The broader PM requirements contain later collaboration/session features. This package delivers the current runtime and usable installation, rather than claiming the complete future roadmap.

## Preview.3 candidate

Shared-folder grants (exact-path card approval, read-only/read-write, until revoked), directory listing/creation, exact 20 MiB file reads and staged binary/text writes in 256 KiB windows are implemented. Local fixture roundtrips and denial/revocation/path defenses are tested; live ChatGPT approval/transport remains pending. Archives retain evidence; task failures retain known earlier-stage evidence without becoming completed/accepted. Runtime, package and Plugin: 0.2.0-preview.3. See PREVIEW3_CHANGES.md and VERIFICATION_PREVIEW3.md.
