# Preview.3 candidate verification

Candidate: package/core/Plugin `0.2.0-preview.3`, Tasks UI `0.2.0-ui.4`.

Baseline: current GitHub main runtime code (Git commit `05abe69aa8ef5da8072c9ff0a4fd46769da2746c`) and current README. Existing preview.2 release assets remain unchanged. New tests use temporary files/databases, fake executors and mocked host APIs; they do not invoke real models, credentials or the live Relay service.

| Check | Actual status |
| --- | --- |
| Shared-folder runtime tests | 18 passed locally on Linux |
| Task stages / partial result / archive tests | 9 passed locally on Linux |
| Mock DOM/host UI tests | 26 passed locally |
| Full runtime regression | 120 run: 111 passed, 9 skipped (native R dependencies unavailable) |
| Installer/bootstrap/shutdown regression | 63 run: 62 passed, 1 Windows-console test skipped |
| Build, package integrity, deterministic rebuild | PASS: manifest/file identities verified; deterministic rebuild checked at freeze |
| Extracted-package fresh install and smoke | PASS locally on Linux: install-check 7/7, 54 tools, actual core preview.3; project smoke 5/5; README SHA/HEAD independently matched |
| Windows CI | Pending candidate branch CI at package freeze; see the external handoff report for subsequent results |
| User Windows deployment / live tunnel / new cards | Not tested; existing service unchanged |
| Live ChatGPT shared-folder bytes, approval and revocation | Not tested |

The injected Git capture error runs a real local fake Codex subprocess, producing fixture files and exiting zero. `snapshot_diff` then raises the supplied permission-error signature. The terminal task remains failed/incomplete, preserving exit zero, parsed summary, stage receipts and a copied SHA-verified artifact. Completed-task review/apply is blocked. A missing later artifact also retains previously captured output.

Archive tests verify result hash/review preservation, restart persistence, active/uncertain refusal, bulk archive, migration and navigation beyond 100 retained tasks. Shared-folder tests cover token rotation/denial, model authorization-flag refusal, durable grants, exact binary roundtrip over 1 MiB, retry/SHA mismatch, changed live files, read-only enforcement/revoke, links/junctions/hardlinks, path/private-root exclusions, root replacement, staging expiry and nonreplayed local recovery after uncertain publication. Windows-specific behavior requires Windows execution. Mock host tests do not prove real card rendering.

No Product ACCEPTED or human acceptance claim is made. This is a source candidate for Windows/live-host trial.
