# Release verification scope

Execution completion, developer evidence review and human acceptance are separate. Tests below do not auto-apply executor changes or mark project results accepted. Private account identifiers, credentials, task databases and user evidence bundles are excluded from release artifacts.

## Evidence by version

| Scope | Outcome | Evidence / limitation |
| --- | --- | --- |
| `.1` exact Local ZIP on Windows 11 | PASS | SHA `5dbd84d256c55d55c73b2ac72a1e3e95e4801fcbc48235aae24ad0980238c8ba`; fresh install/check/smoke; 40 tools; Python 3.14.3, Git 2.53.0 |
| `.1` real tunnel-client 0.0.11 | PASS | Three path cases including Unicode/spaces/apostrophes, active-old-client refusal, explicit key-file behavior, ready/live, one local client |
| `.1` generated private Plugin | PASS | Deterministic second generation; actual existing private app binding; account import and live project tools |
| `.1` ChatGPT project/short-task smoke | PASS | Actual project identity, README/hash, Git and capabilities; terminal result/exit code checked |
| `.1` ChatGPT → Claude → R → exact artifacts | PASS | Claude Code 2.1.251 / R 4.5.1; explicit native execution; ZIP/CSV/PNG/member hashes independently checked; base Git unchanged |
| D-004 Windows product launcher | PASS | Two real Ctrl+Break phases and one Ctrl+C stop; children exited, health closed, locks disappeared; Ctrl+C observed 9.5 seconds; restart ready; config/project/task/artifact baselines unchanged |
| `.2` exact candidate build/local fresh install | PASS | Linux checks plus Windows 11 exact ZIP SHA/size, manifest, installation 5/5, project smoke 5/5 and 40 tools; MIT/offline Plugin checks passed |
| `.2` private Plugin import/readback | PASS | Same private identity/app/skill; eight files read back exactly; license/version retained |
| Live project smoke after `.2` Plugin update | PASS | Five read-only tool calls against retained `.1 + D-004`; README SHA independently checked, Git clean, preflight eligible, no task |
| Isolated `.2` root on live tunnel | NOT_PERFORMED | Windows local stdio checks passed; existing service was retained |

The release trial ZIP was 5,848 bytes, SHA `4c95df4d1ea3744f7108aed6e719850b0cb5561d83e580ad5c5cff8689b16c97`. It contained summary CSV, coefficient CSV, R model and PNG. R executed its model round-trip check locally; ChatGPT independently recomputed retrieved bytes/member hashes and checked CSV/PNG, but did not rerun RDS in R. A task script repaired an accidental PowerShell executable-path failure before successful computation. No system PATH/allowlist change was required.

The earlier private computational trial additionally tested a deliberately failing R script and follow-up reuse of prior artifacts. That scenario was not repeated in the `.1` release trial.

## Remaining host/platform checks

- Attachment of the updated private Plugin in a new conversation.
- Tasks-card rendering, scrolling and selected-task results UX in the host.
- Events automatic continuation in the originating work conversation; no subscription was used for the latest computational E2E.
- Snapshot expiry and transfers larger than the demonstrated 97,520-byte ZIP. Configured capacity is not demonstrated capacity.
- Windows 10 and a Windows host without Codex installed.
- Different Local/Plugin versions, unattended/service mode, and automatic upgrade/state migration.

Single-file card approval, immutable snapshot/same-key retry and denial had separate real ChatGPT evidence on the runtime baseline. Later Work-mode snapshot retrieval also succeeded; this does not establish every host/card layout.

## Reproduce and retain evidence

Use [ACCEPTANCE.md](ACCEPTANCE.md) for exact-package local/account steps. Candidate build/test commands live in [DEVELOPMENT.md](DEVELOPMENT.md). Retain full release SHA, platform versions, exit codes and sanitized output receipts. Record absent tests as NOT_TESTED; local simulations are not ChatGPT evidence. Keep private paths, tokens, key files and account IDs out of public reports.

## Verification update after package freeze

The verified Local ZIP remains **244,100 bytes**, SHA `48f458447cf9cc8da8c9015799a3001708db3343fa9196e8b30fc8f9edcac6c0`. Windows 11 Home 10.0.26100 / Python 3.14.3 / Git 2.53.0 performed all five installation commands with exit 0. Evidence ZIP SHA: `74a56e52fe20552496d789489c5e6c0ef6dd891686da83992c29705f825f011a`. JSON receipts matched their command outputs; the CRLF README SHA was independently recomputed in evidence review.

Private Plugin metadata version `0.2.0-preview.2` was accepted by Plugin Creator and all eight account-source files read back exactly. The actual app mapping, scope, audience, workflow, default prompt and author remained unchanged. Live project inspection/preflight succeeded after the update using the existing baseline installation. No executor task, live-tunnel cutover or project integration was performed in this verification round.

The published accompanying documentation records these later results. Documentation inside the frozen Local ZIP reflects its build-time acceptance status; no executable/source payload was repackaged to update test-status prose. The runtime core remains `0.2.0-spike.4`.
