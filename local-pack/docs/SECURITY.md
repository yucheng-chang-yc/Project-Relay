# Security and trust boundaries

The current Windows computational runtime is a trusted local execution mode, not an OS-level security sandbox. The boundaries below describe the shipped source and separately tested host behavior.

## Authentication and secrets

The optional HTTP API uses one installation-wide bearer token, generated randomly into plaintext `config/config.json`. There is no per-user/project HTTP identity. Rotate it manually while stopped, then restart and update HTTP clients. Stdio trusts local process/pipe access; ChatGPT connectivity uses OpenAI tunnel authentication rather than that HTTP token.

The tunnel runtime API key is generated/rotated in OpenAI Platform. The helper passes it to tunnel-client from an explicitly selected credential file or a hidden prompt, and saves only source references. Inherited `CONTROL_PLANE_API_KEY` is ignored; a missing/invalid file fails without fallback. File rotation is read at the next start, with the client stopped and restarted. The key is absent from generated Plugin ZIPs, connection receipts and Project Relay configuration. Project Relay operates no cloud credential store; OpenAI account/tunnel credential retention is outside this package.

HTTP/MCP diagnostics omit raw headers, approval tokens and tool argument/result values. Executor logs can contain secrets printed by programs; event signing secrets persist in the local state database. Protect config/state/logs/backups, tunnel profiles and CLI credentials. Windows ACLs are inherited from the chosen directory; this Preview has no encrypted credential vault. The public artifacts contain source/templates, with user credentials created or supplied after installation.

## Network behavior

HTTP binds to `127.0.0.1`, default port `8765`, with local Host/Origin checks. Stdio opens no HTTP listener; an external Secure MCP Tunnel provides the ChatGPT route. Implemented outbound routes include authorized HTTPS input downloads, signed HTTPS event callbacks and requested Git remote operations. The binary download hostname allowlist starts empty.

Codex/Claude/provider requests and native computation can also use the network. Project Relay imposes no native-process network firewall. Reviewed source has no Project Relay telemetry service or default upload to a Project Relay-managed server; explicit ChatGPT reads, provider calls, Git and tunnel/event traffic can leave the machine.

## Project and file boundaries

Project file APIs resolve within the registered root and reject absolute/drive/ADS paths, `..`, Git metadata and resolved link escapes. Writes/tasks require explicit writable registration. `.env`, gitignored files and other in-root secrets are **not automatically excluded** from authorized reads.

A separate inline Allow once/Decline card authorizes one exact external file snapshot, with no directory/write/execute permission. Approval depends on ChatGPT preserving app-only tool visibility; authenticated raw MCP clients and the local operator are trusted. Staging retains exact hash-verified bytes as explicit task inputs; content remains untrusted.

Codex/Claude tasks use detached worktrees from committed HEAD plus selected staged inputs. Fixed-profile command tasks run in the registered root. Worktrees separate changes for review and do not provide OS confinement.

## Command execution

The supplied Claude adapter disables unrestricted Bash/PowerShell and exposes bounded file tools plus dedicated computation run/status/terminate tools. Ordinary command tasks select an operator-configured fixed argv, potentially any operator-selected executable; model-supplied arbitrary executable argv is not accepted through that interface.

Native computation accepts only a configured absolute `Rscript` and project-relative `.R` script, with `--rscript` and `--ack-native-r` opt-in. Launch is shell-free; R retains local-account file/process/network rights and can spawn children. `native_trusted` with `os_containment=false` means these application checks do not prevent off-project access.

Tasks have bounded timeouts/cancellation. Computation has 1–120-second limits, bounded output, process-tree termination and durable receipts. Uncertain termination retains the project lock for local recovery. These controls do not form an OS sandbox.

> Project Relay Preview is intended for use with projects and executors you trust on your own machine. The current Windows computational runtime does not provide OS-level containment.

## License and private data

Project Relay source is MIT-licensed. The release ZIPs contain source, synthetic fixtures and templates, without configured installations, developer credentials, task databases or captured user artifacts. Generated per-app Plugin ZIPs include the MIT notice and app mapping; keep them private. Licensing does not restrict which files an authorized local process can read.
