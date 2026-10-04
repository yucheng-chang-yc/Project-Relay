# Single-file snapshot — development and operations notes

Status (2026-10-03): implemented in 0.2.0-spike.4. Local tests pass. Real ChatGPT acceptance passed in a regular chat for card approval, a 97,520-byte ZIP (SHA match + extraction), immutability after a source change, same-key retry and deny. Not verified in ChatGPT: expiry, or files larger than 97,520 bytes.

## What it does

A user names one local file in ChatGPT. Workbench shows an approval card with that exact path. After the user approves once, Workbench copies the file into an immutable snapshot. GPT then reads the snapshot's bytes in bounded Base64 windows and verifies the whole-file SHA-256. No project registration, Git repository or executor task is involved.

## Tools

| Tool | Caller | Purpose |
|---|---|---|
| `request_file_snapshot(path, idempotency_key)` | model | Creates a pending request and renders the approval card. Reads nothing. |
| `prepare_file_snapshot_approval(request_id)` | widget only (`openai/visibility: private`, `ui.visibility: ["app"]`) | Called by the card on load: current status plus, while pending, a fresh single-use approval token. |
| `approve_file_snapshot(request_id, approval_token, decision)` | widget only (same visibility) | Checks the user's decision and takes the snapshot. |
| `get_file_snapshot(request_id \| snapshot_id)` | model | Returns status: `awaiting_user_approval`, `capturing`, `denied`, `failed`, `expired` or `ready` (with `snapshot_id`, size, SHA-256, `taken_at`, `expires_at`, `consistency`, `capture_ms`). |
| `read_file_snapshot_bytes(snapshot_id, expected_sha256, offset, limit≤262144)` | model | Returns `base64`, `chunk_sha256`, `next_offset`, `eof`. Needs no further approval. |

Fallback when the card cannot call the tool: `python scripts/approve_file_snapshot.py --config <config> --request fsr_… [--approve|--deny]`. The pending status tells the model this as `if_card_does_not_open`.

## How the user's approval becomes a decision the server can verify

1. `request_file_snapshot` creates a pending request. Its model-visible result carries only the non-secret `request_id`; no token, and no custom result `_meta`.
2. The card, on load, calls the app-only `prepare_file_snapshot_approval`.
   - The server creates a random single-use approval token and stores only its SHA-256.
   - Each call issues a new token, so an older card stops working.
3. The approving tool is also hidden from the model. The card calls it with the token after the user clicks 「授權這個檔案一次」.
   - The server compares the token in constant time and checks that the request is still pending and inside its approval window (15 minutes).
4. Whatever the model sends (for example `authorized: true`, a guessed token, or a call to a hidden tool) is not enough. Without a card-fetched token, the request stays pending.
5. A local operator can decide with the script instead, because local access to the runtime is itself the authority.

**Why the token is no longer delivered in tool-result `_meta` (changed 2026-10-03):**
- OpenAI has acknowledged that ChatGPT's MCP Apps bridge can drop custom tool-result `_meta`.
- Its recommended workaround is a non-secret opaque key in `structuredContent` that the widget uses to re-fetch state.
- In real ChatGPT, the card also failed to open while the result carried a custom `_meta` key.

**Trust boundary:** both decision tools rely on the host keeping private (app-only) tools away from the model, the same mechanism `read_artifact_chunk` uses. Real ChatGPT acceptance can confirm the card opens and approval works. It cannot prove the model can never invoke a private tool.

## Scope of one approval

- Exactly one regular file at exactly the shown path, read once into one snapshot.
- No parent-folder listing, no other files, no modification, no execution.
- **Retries:** reusing the same `idempotency_key` returns the same request or snapshot. Re-sampling the same file needs a new key and a new approval. Reusing a key with a different path is refused.
- **Expiry:** a snapshot expires after 24 hours by default. Its bytes are then deleted, and it cannot be read again or re-captured under the same request.

## Path rules (Windows)

**Accepted:** an absolute path on a local drive, such as `C:\Users\name\Downloads\file.zip`. Forward slashes and one pair of surrounding quotes are normalized away.

**Refused:**

| Category | Examples |
|---|---|
| Relative paths and drive-relative forms | `C:file` |
| UNC and network paths | `\\server\share` |
| Device and long-path prefixes | `\\?\`, `\\.\` |
| Network or unknown drive types | |
| Wildcards | `* ? " < > \|` |
| Alternate data streams | `file:stream` |
| `.` / `..` components | |
| Names ending in a space or dot | |
| Reserved device names | `CON`, `NUL`, `COM1`, `LPT1`, … |
| Folders | |
| A link or junction in the file or any parent folder | default in this version |

**Why refuse network paths and reparse points:** both make it hard to state what the user actually approved. A junction or symlink can point the shown path at a different file. A network share can change underneath the snapshot or reveal credentials. They can be added later behind explicit operator configuration.

## Single-handle capture and change detection

1. The parents are checked with `lstat`.
2. The file is opened once with `CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT)`. The open first asks for share mode READ only, which keeps other writers out while it is held.
3. Checks on the opened handle:
   - It must be a disk file.
   - It must not be a directory or reparse point.
   - `GetFinalPathNameByHandle` must equal the requested path. Short 8.3 names, substituted drives and a path swapped between check and open all fail here.
4. All bytes are read from that same handle, at most one byte past the size seen at the start, so a growing file cannot be chased.

**Consistency levels:**
- `writers_excluded_during_read`: no other program could write during the copy.
- `metadata_unchanged_during_read`: another program already had the file open for writing, so writers could not be excluded. The read is accepted only if size, last-write time and file identity were unchanged before and after, and the byte count matched. Otherwise it retries up to 3 times and then fails. This level is weaker: an in-place rewrite that changes neither size nor timestamp cannot be detected.

## Errors

| Situation | Error |
|---|---|
| File missing | `File not found` |
| Access denied | `Access to the file is denied` |
| Locked by another program | `The file is locked by another program` |
| Folder or device | `Only a regular file can be snapshotted…` |
| Link or junction | `…passes through a link or junction…` |
| Path resolves somewhere else | `The path resolves to a different location…` |
| Over the size limit | `The file is N bytes; the snapshot limit is M bytes` |
| File keeps changing | `The file kept changing while it was read…` |
| Store full | `The snapshot store is full…` |
| Snapshot expired | `This snapshot has expired…` |
| Bad offset | `Offset is outside the snapshot (0..N)` |
| Window too large | `Snapshot byte window must be 1..262144 bytes` |
| Wrong SHA | `expected_sha256 differs from this snapshot` |
| Stored bytes corrupted | `Stored snapshot is inconsistent; it will not be served` |

A failed or denied request is final; asking again needs a new key.

## Storage and configuration

- **Bytes:** `<data_dir>/file_snapshots/fss_<id>.bin`, read-only. A `.partial` file is never served.
- **Database tables:** `file_snapshot_requests`, `file_snapshots`.
- **Optional config:** a `file_snapshots` object in `config.json`. Every key is an integer, validated against its bounds.

| Key | Default | Allowed range |
|---|---|---|
| `max_bytes` | 20 MiB | up to 500 MiB |
| `snapshot_ttl_seconds` | 86400 | 60 s – 7 days |
| `approval_ttl_seconds` | 900 | 60 – 3600 |
| `max_store_bytes` | 1 GiB | up to 10 GiB |

Expired bytes are deleted the next time any snapshot tool is called.

## Capacity

The transport path is the same as `read_artifact_bytes`: GPT receives Base64 in tool results and decodes it in its own code tool. The server limit is not the end-to-end limit. Real host throughput and output limits decide what works. Only sizes actually tested in ChatGPT support a capacity claim: 5,951 bytes passed via `read_artifact_bytes`; 97,520 bytes passed via this tool in 4 windows (about 2 minutes). ChatGPT reported that one full-size window exceeded its code-tool text limit, so smaller windows are needed.
