# Project Relay v0.2.0-preview.3 candidate

This candidate adds shared folders, task archives and durable partial execution evidence. Runtime core and Plugin package both identify as `0.2.0-preview.3`; Tasks UI is `0.2.0-ui.4`. It has not replaced the running installation or been accepted through a live ChatGPT trial.

## Shared folders

Ask in the connected conversation:

> Request read and write access to `C:\RelayShared`. Show the exact folder approval card.

Create that dedicated folder locally first. Approve the displayed folder and permission in the inline card. All existing and future descendant files/folders are included, including any secrets placed there. The grant lasts until revoked and survives runtime restarts and conversations. It targets the computer running Relay; using ChatGPT on another device does not redirect it to that device. Neither Git, registered projects nor executors are needed.

Use the conversation for listing, exact file reads, folder creation and binary/text writes. The Tasks view has a **Shared folders** tab listing path, access and **Revoke access**. Revocation prevents new reads/writes and cancels pending uploads; original files stay in place. Bytes already delivered to a conversation cannot be recalled. Operations already in flight may complete before the serialized revocation commits.

The read and write file limit is 20 MiB. Byte windows are 256 KiB. First read obtains the live SHA; pin that SHA for all later windows and independently verify reconstructed bytes. A changing file requires restarting the read. This differs from the immutable single-file snapshot feature, which remains available.

Writes use begin → bounded chunks → commit. The expected existing SHA is required for overwrite; empty means create. Whole staged SHA/size must match before any original file changes. Identical chunk/commit retries are supported. Pending uploads expire after one hour and are cleaned on the next write operation. Their total declared capacity is 100 MiB. Create parent folders with `create_shared_directory` first. There is no shared-folder deletion or executable/task capability.

Links, junctions, reparse points, hard-linked files, network/device paths, ADS, traversal and `.git` paths are refused. Runtime/config/registered project roots and overlapping ancestors/descendants are refused, so sharing cannot bypass project task/worktree coordination. Use an external dedicated folder. Windows pins ancestor directories against relocation; POSIX walks directory descriptors without following links. These checks are an API boundary, not an OS sandbox. Existing-file overwrite checks SHA immediately before atomic replacement; trusted local writers may still race the final check. Concurrent-create publication refuses clobbering. A publication interrupted before its DB receipt is recorded is treated as uncertain and blocks automatic replay. An uncertain attempt blocks new writes to that path, including after expiry or revocation. Inspect the local target and resolve it using the local-only maintenance helper before starting a new write:

```powershell
python "$RelayRoot\maintenance\resolve_shared_write.py" --root "$RelayRoot" --write <write_id> --observed-sha256 <actual-current-sha-or-empty> --note "Inspected target locally; no replay"
```

Recovery verifies the observed current SHA and records a note, without modifying the original or replaying the upload. This helper has no MCP tool.

The app-only prepare/approve tools depend on the host keeping private tools and their result tokens unavailable to the model, as the existing single-file snapshot mechanism does. No model-supplied `authorized` flag grants access. Approval tokens are hashed at rest, one-use, rotated by a freshly prepared card and cleared on a recorded decision or expired request inspection. No approval argument/token value is recorded in MCP diagnostic logs.

## Tasks

**Archive**, **Restore**, **Current / Archived** and **Archive finished tasks** control the durable list. Batch archive includes completed, failed, cancelled and timed-out tasks; queued/running/cancelling/interrupted tasks remain visible. Evidence, worktrees and review records are retained. Archive never means Reviewed or Accepted. Each page returns up to 100 tasks, with Older tasks / Latest tasks navigation and counts scoped to the shown page; batch archive can hide all settled tasks in the selected projects. Physical data deletion/retention controls are outside this candidate. ChatGPT's historical conversation cards are managed by ChatGPT.

The worker persists actual stage receipts before postprocessing, saves parsed executor reports before Git capture, and copies declared output files before building the Git diff. A later failure retains previously captured files and records the error phase. A zero CLI exit with parsing/capture failure remains an incomplete failed task; it cannot be accepted/applied using the completed-task integration route. Integration remains a separate explicit operation. Earlier-stage outcomes of old tasks without receipts stay unknown. This addresses misleading all-or-nothing results; it does not repair the underlying Windows file permission/locking cause.

## Install and trial

Use the normal fresh-install route into a new empty directory. No in-place updater or data import is added. Do not use the legacy `upgrade_runtime.py`, which only supports historical 0.1.x layouts. Retain the current installation as rollback; existing tasks and grants are not automatically carried into a fresh install.

New tools and a new Tasks resource URI require one runtime switch/restart and one MCP rescan. Generate the matching private Plugin package and update the existing account Plugin; preserve app identity. Once installed, folder approval/revocation and task archives need no restart/rescan. Windows deployment and live card approval/read-write/revocation remain separate acceptance gates. See the supplied `CLAUDE_CODE_INSTRUCTIONS.md` for the operator handoff and `VERIFICATION_PREVIEW3.md` for actual evidence.
