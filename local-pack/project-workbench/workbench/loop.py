"""Trial orchestration identities and exact bytes; not a project authority layer."""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import time
from urllib.parse import urlsplit
import uuid

from .core import WorkbenchError, canonical, digest, hash_file, run_git, safe_path

MAX_INPUT = 20 * 1024 * 1024
MAX_TASK_INPUTS = 100 * 1024 * 1024
MAX_STAGED_STORE = 1024 * 1024 * 1024
MAX_RESOURCE = 20 * 1024 * 1024
MAX_CHUNK = 256 * 1024
INPUT_PREFIX = "wb_inputs/"


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WorkbenchError("An exact lowercase SHA-256 is required")
    return value


def pinned_download(url, allowed_hosts, target, expected_size, expected_sha256):
    """No proxy, redirects, local addresses, DNS second lookup, or URL-bearing errors."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except (ValueError, TypeError):
        raise WorkbenchError("Malformed file URL") from None
    reason = ("not_https" if parsed.scheme != "https" else "missing_host" if not host else
              "embedded_credentials" if parsed.username or parsed.password else "fragment" if parsed.fragment else
              "non_default_port" if port not in (None, 443) else "host_not_in_allowlist" if host not in allowed_hosts else None)
    if reason:
        # Report only the bare hostname and reason so the operator can authorize the exact host.
        # Path, query (signed parameters), credentials and file contents are never echoed.
        shown = host if host and len(host) <= 253 and all(c.isalnum() or c in ".-" for c in host) else "unrepresentable"
        raise WorkbenchError(f"File download requires an explicitly authorized HTTPS hostname "
                             f"(download_host={shown}; reason={reason})")
    connection = None
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global or ipaddress.ip_address(a[4][0]).is_multicast for a in addresses):
            raise WorkbenchError("File URL DNS must resolve only to public addresses")
        ip = addresses[0][4][0]
        connection = http.client.HTTPSConnection(host, timeout=15, context=ssl.create_default_context())
        # Connect to the address we checked, keeping the original TLS SNI/certificate hostname.
        raw = socket.create_connection((ip, 443), timeout=15)
        connection.sock = connection._context.wrap_socket(raw, server_hostname=host)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"Accept-Encoding": "identity"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise WorkbenchError("File download refused: status/encoding/redirect")
        length = response.getheader("Content-Length")
        if length is not None and int(length) != expected_size:
            raise WorkbenchError("File download length differs from the explicit input contract")
        h, size, started = hashlib.sha256(), 0, time.monotonic()
        with target.open("xb") as output:
            while True:
                if time.monotonic() - started > 60:
                    raise WorkbenchError("File download exceeded 60 seconds")
                block = response.read(min(65536, expected_size - size + 1))
                if not block:
                    break
                size += len(block)
                if size > expected_size or size > MAX_INPUT:
                    raise WorkbenchError("File download exceeds its byte contract")
                h.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if size != expected_size or h.hexdigest() != expected_sha256:
            raise WorkbenchError("File download SHA/size mismatch; no input published")
    except WorkbenchError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise WorkbenchError("File download failed; signed URLs and remote errors are withheld") from None
    finally:
        if connection:
            connection.close()


class LoopStore:
    def __init__(self, runtime):
        self.runtime = runtime
        with runtime.connection(write=True) as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS loop_inputs (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, request_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, manifest TEXT NOT NULL, UNIQUE(project,request_key));
              CREATE TABLE IF NOT EXISTS loop_units (id TEXT PRIMARY KEY, project TEXT NOT NULL, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS loop_snapshots (
                task_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL, body TEXT NOT NULL, sha256 TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS loop_reviews (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, request_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(task_id,request_key));
              CREATE TRIGGER IF NOT EXISTS loop_inputs_update BEFORE UPDATE ON loop_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
              CREATE TRIGGER IF NOT EXISTS loop_inputs_delete BEFORE DELETE ON loop_inputs BEGIN SELECT RAISE(ABORT,'immutable input'); END;
              CREATE TRIGGER IF NOT EXISTS loop_snapshots_update BEFORE UPDATE ON loop_snapshots BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
              CREATE TRIGGER IF NOT EXISTS loop_snapshots_delete BEFORE DELETE ON loop_snapshots BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
              CREATE TRIGGER IF NOT EXISTS loop_reviews_update BEFORE UPDATE ON loop_reviews BEGIN SELECT RAISE(ABORT,'immutable review'); END;
              CREATE TRIGGER IF NOT EXISTS loop_reviews_delete BEFORE DELETE ON loop_reviews BEGIN SELECT RAISE(ABORT,'immutable review'); END;
            """)

    def input(self, project_id, input_id):
        project, _ = self.runtime.project(project_id, writable=True)
        self.runtime.assert_config_current(project_id)
        with self.runtime.connection() as c:
            row = c.execute("SELECT * FROM loop_inputs WHERE id=? AND project=?", (input_id, project_id)).fetchone()
        if not row:
            raise WorkbenchError("Unknown project input")
        item = json.loads(row["manifest"])
        if item["project_binding_sha256"] != self.runtime.binding_hash(project):
            raise WorkbenchError("Input project policy changed; stage under the new binding")
        path = safe_path(self.runtime.data, item["stored_name"])
        if not path.is_file() or path.stat().st_size != item["size_bytes"] or hash_file(path) != item["sha256"]:
            raise WorkbenchError("Input bytes missing or changed; execution refused")
        return item, path

    def _stage(self, project_id, source_identity, size_bytes, sha256, idempotency_key, writer):
        project, _ = self.runtime.project(project_id, writable=True)
        self.runtime.assert_config_current(project_id)
        sha(sha256)
        if type(size_bytes) is not int or not 0 <= size_bytes <= MAX_INPUT:
            raise WorkbenchError("Input must be at most 20 MiB")
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise WorkbenchError("Stable input idempotency key required")
        semantic = {"source": source_identity, "size_bytes": size_bytes, "sha256": sha256,
                    "project_binding_sha256": self.runtime.binding_hash(project)}
        fingerprint = digest(canonical(semantic).encode())
        ident = "input_" + uuid.uuid4().hex
        directory = self.runtime.data / "loop" / "inputs" / ident
        with self.runtime.connection(write=True) as c:
            existing = c.execute("SELECT * FROM loop_inputs WHERE project=? AND request_key=?", (project_id, idempotency_key)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise WorkbenchError("Input key reused for a different source/byte identity")
                item = json.loads(existing["manifest"])
                _, existing_path = self.input(project_id, item["input_id"])
                return item
            # Count actual storage, including crash orphans, rather than only successful records.
            base = self.runtime.data / "loop" / "inputs"
            used = sum(p.stat().st_size for p in base.rglob("*") if p.is_file()) if base.exists() else 0
            if used + size_bytes > MAX_STAGED_STORE:
                raise WorkbenchError("Staged input store reached 1 GiB; operator retention action required")
            directory.mkdir(parents=True)
            temporary = directory / "incoming.part"
            final = directory / "blob"
            try:
                writer(temporary)
                if temporary.stat().st_size != size_bytes or hash_file(temporary) != sha256:
                    raise WorkbenchError("Staged input SHA/size differs from contract")
                temporary.replace(final)
                item = {"schema": "wb.input.v1", "input_id": ident, "project_id": project_id,
                        **semantic, "stored_name": final.relative_to(self.runtime.data).as_posix(),
                        "created_at": time.time(), "durability": "copied_task_store", "untrusted": True}
                c.execute("INSERT INTO loop_inputs VALUES(?,?,?,?,?)", (ident, project_id, idempotency_key, fingerprint, canonical(item)))
                return item
            except BaseException:
                shutil.rmtree(directory)
                raise

    def stage_binary_input(self, project_id, file, expected_size_bytes, expected_sha256, idempotency_key):
        # Signed URL intentionally does not enter identity, logs, database or task packets.
        if not isinstance(file, dict) or not isinstance(file.get("file_id"), str) or not isinstance(file.get("download_url"), str):
            # Field names/types only, so host payload-shape mismatches are diagnosable without values.
            shape = ({k: type(v).__name__ for k, v in list(file.items())[:10] if isinstance(k, str) and len(k) <= 64}
                     if isinstance(file, dict) else type(file).__name__)
            raise WorkbenchError(f"Host fileParams value required (received={canonical(shape)})")
        hosts = self.runtime.config.get("binary_transfer", {}).get("allowed_download_hosts", [])
        current = json.loads(self.runtime.config_path.read_text(encoding="utf-8-sig"))
        if current.get("binary_transfer", {}) != self.runtime.config.get("binary_transfer", {}):
            raise WorkbenchError("Binary transfer authorization changed; restart the facade")
        return self._stage(project_id, {"kind": "host_file", "file_id": file["file_id"]},
            expected_size_bytes, expected_sha256, idempotency_key,
            lambda path: pinned_download(file["download_url"], hosts, path, expected_size_bytes, expected_sha256))

    def stage_local(self, project_id, path, expected_sha256, idempotency_key):
        """Operator/test-only ingress, deliberately absent from MCP tools."""
        path = Path(path)
        return self._stage(project_id, {"kind": "operator_file", "name": path.name}, path.stat().st_size,
                           expected_sha256, idempotency_key, lambda output: shutil.copyfile(path, output))

    def stage_project_input(self, project_id, path, expected_sha256, idempotency_key):
        _, root = self.runtime.project(project_id, writable=True)
        source = safe_path(root, path)
        if not source.is_file():
            raise WorkbenchError("Primary input must be an authorized regular project file")
        return self._stage(project_id, {"kind": "primary_project", "path": path}, source.stat().st_size,
                           expected_sha256, idempotency_key, lambda output: shutil.copyfile(source, output))

    def stage_prior_artifact(self, project_id, task_id, artifact_id, expected_result_sha256, expected_sha256, idempotency_key):
        row = self.runtime.get_row(task_id)
        if row["project"] != project_id or row["status"] != "completed" or row["result_sha256"] != sha(expected_result_sha256):
            raise WorkbenchError("Prior artifact requires a completed same-project exact result")
        self.runtime.validate_task_binding(row)
        path, item = self.runtime.artifact_path(task_id, artifact_id)
        if item["sha256"] != sha(expected_sha256):
            raise WorkbenchError("Prior artifact SHA differs from the explicitly selected bytes")
        return self._stage(project_id, {"kind": "prior_artifact", "task_id": task_id,
            "artifact_id": artifact_id, "result_sha256": row["result_sha256"]}, item["size_bytes"],
            item["sha256"], idempotency_key, lambda output: shutil.copyfile(path, output))

    def prepare(self, project_id, kind, spec):
        spec = json.loads(canonical(spec))
        inputs = spec.get("inputs", [])
        if inputs and kind not in ("codex", "claude"):
            raise WorkbenchError("Input staging requires an isolated agent task")
        if not isinstance(inputs, list) or len(inputs) > 30:
            raise WorkbenchError("At most 30 explicitly selected inputs are permitted")
        total, destinations = 0, set()
        for entry in inputs:
            if not isinstance(entry, dict) or set(entry) != {"input_id", "destination"}:
                raise WorkbenchError("Task inputs require input_id and destination")
            destination = entry["destination"]
            if not isinstance(destination, str) or destination.startswith("/"):
                raise WorkbenchError("Inputs require a project-relative destination")
            parts = destination.split("/")
            if any(not p or p in (".", "..") or p.rstrip(" .") != p or
                   re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", p) for p in parts):
                raise WorkbenchError("Input destination is not a portable Windows file path")
            # New worktree directory is created later; portable safe_path rejects escapes/metadata.
            safe_path(self.runtime.data / "future-workspace", destination)
            folded = destination.casefold().rstrip(" .")
            if any(folded == old or folded.startswith(old + "/") or old.startswith(folded + "/") for old in destinations):
                raise WorkbenchError("Input destinations collide on Windows")
            destinations.add(folded)
            item, _ = self.input(project_id, entry["input_id"])
            total += item["size_bytes"]
        if total > MAX_TASK_INPUTS:
            raise WorkbenchError("Task input snapshot exceeds 100 MiB")
        parent = spec.get("parent_task_id")
        if parent:
            row = self.runtime.get_row(parent)
            if row["project"] != project_id or row["status"] != "completed" or not row["result_sha256"]:
                raise WorkbenchError("Parent requires a sealed completed same-project task")
            if spec.get("parent_result_sha256") and spec["parent_result_sha256"] != row["result_sha256"]:
                raise WorkbenchError("Parent result differs from the selected exact lineage identity")
            self.snapshot(parent)
        if spec.get("compute_runtime"):
            if kind != "claude":
                raise WorkbenchError("Restricted computational executor adapter currently targets Claude only")
            project, _ = self.runtime.project(project_id, writable=True)
            from .restricted import validate_policy
            validate_policy(project.get("compute_runtime"))
            if any(rule == "Bash" or rule.startswith("Bash(") for rule in project.get("claude_allowed_tools", [])):
                raise WorkbenchError("Computational Claude tasks cannot enable native Bash")
        return spec

    def bind(self, connection, task_id, project_id, base_head, spec, project_binding_sha256):
        inputs, parents = [], {}
        parent = spec.get("parent_task_id")
        if parent:
            parents[parent] = self.runtime.get_row(parent)["result_sha256"]
        for entry in spec.get("inputs", []):
            item, _ = self.input(project_id, entry["input_id"])
            inputs.append({**entry, "sha256": item["sha256"], "size_bytes": item["size_bytes"], "source": item["source"]})
            if item["source"]["kind"] == "prior_artifact":
                parents[item["source"]["task_id"]] = item["source"]["result_sha256"]
        unit = None
        if parent:
            unit = self.snapshot(parent)["work_unit_id"]
        if not unit:
            unit = "unit_" + uuid.uuid4().hex
            connection.execute("INSERT INTO loop_units VALUES(?,?,?)", (unit, project_id, time.time()))
        body = {"schema": "wb.input_snapshot.v1", "task_id": task_id, "project_id": project_id,
                "work_unit_id": unit, "base_head": base_head, "project_binding_sha256": project_binding_sha256,
                "task_contract_sha256": digest(canonical(spec).encode()), "inputs": inputs,
                "parents": [{"task_id": k, "result_sha256": v} for k, v in sorted(parents.items())]}
        encoded = canonical(body)
        connection.execute("INSERT INTO loop_snapshots VALUES(?,?,?,?)", (task_id, unit, encoded, digest(encoded.encode())))

    def snapshot(self, task_id):
        self.runtime.get_row(task_id)
        with self.runtime.connection() as c:
            row = c.execute("SELECT * FROM loop_snapshots WHERE task_id=?", (task_id,)).fetchone()
        if not row or digest(row["body"].encode()) != row["sha256"]:
            raise WorkbenchError("Task has no verified trial input snapshot")
        return {**json.loads(row["body"]), "snapshot_sha256": row["sha256"]}

    def has_snapshot(self, task_id):
        with self.runtime.connection() as c:
            return c.execute("SELECT 1 FROM loop_snapshots WHERE task_id=?", (task_id,)).fetchone() is not None

    def materialize(self, task_id, workspace):
        row = self.runtime.get_row(task_id)
        snapshot = self.snapshot(task_id)
        spec = json.loads(row["spec"])
        if snapshot["task_contract_sha256"] != digest(canonical(spec).encode()):
            raise WorkbenchError("Task contract changed after snapshot")
        for entry in snapshot["inputs"]:
            item, source = self.input(row["project"], entry["input_id"])
            if item["sha256"] != entry["sha256"]:
                raise WorkbenchError("Input identity changed")
            target = safe_path(workspace, entry["destination"])
            if target != workspace / entry["destination"] or target.is_dir():
                raise WorkbenchError("Input destination traverses an alias or is a directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            if hash_file(target) != entry["sha256"]:
                raise WorkbenchError("Input materialization SHA mismatch")
        return snapshot

    def artifact_chunk(self, task_id, artifact_id, expected_sha256, offset=0, limit=MAX_CHUNK):
        path, item = self.runtime.artifact_path(task_id, artifact_id)
        if item["sha256"] != sha(expected_sha256) or type(offset) is not int or not 0 <= offset <= item["size_bytes"]:
            raise WorkbenchError("Artifact identity/window mismatch")
        if type(limit) is not int or not 1 <= limit <= MAX_CHUNK:
            raise WorkbenchError("Artifact byte window exceeds 256 KiB")
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(limit)
        return {"artifact_id": item.get("artifact_identity", f"{task_id}:{artifact_id}"),
                "sha256": item["sha256"], "size_bytes": item["size_bytes"], "offset": offset,
                "next_offset": offset + len(data), "chunk_sha256": digest(data), "base64": base64.b64encode(data).decode("ascii")}

    def artifact_resource(self, task_id, artifact_id, expected_sha256):
        path, item = self.runtime.artifact_path(task_id, artifact_id)
        if item["sha256"] != sha(expected_sha256):
            raise WorkbenchError("Artifact SHA differs from requested resource")
        uri = f"workbench-artifact://{task_id}/{artifact_id}/{item['sha256']}"
        return {"uri": uri, "name": item["name"], "mimeType": item.get("mime_type") or mimetypes.guess_type(item["name"])[0] or "application/octet-stream",
                "size_bytes": item["size_bytes"], "sha256": item["sha256"], "host_materialization": "unverified",
                "full_resource_limit_bytes": MAX_RESOURCE}

    def read_resource(self, uri):
        match = re.fullmatch(r"workbench-artifact://(task_[0-9a-f]{32})/([0-9]+)/([0-9a-f]{64})", uri)
        if not match:
            raise WorkbenchError("Unknown artifact resource URI")
        task_id, artifact_id, expected = match.groups()
        meta = self.artifact_resource(task_id, int(artifact_id), expected)
        path, _ = self.runtime.artifact_path(task_id, int(artifact_id))
        if path.stat().st_size > MAX_RESOURCE:
            raise WorkbenchError("Binary resource exceeds 20 MiB; bounded app transfer required")
        return {"uri": uri, "mimeType": meta["mimeType"], "blob": base64.b64encode(path.read_bytes()).decode("ascii")}

    def evidence(self, task_id):
        row = self.runtime.get_row(task_id)
        self.runtime.validate_task_binding(row)
        if row["status"] != "completed" or not row["retrieved"] or not row["result_sha256"]:
            raise WorkbenchError("Retrieve a sealed completed result before review")
        result = json.loads(row["result"])
        if digest(canonical(result).encode()) != row["result_sha256"]:
            raise WorkbenchError("Stored result identity changed")
        snapshot = self.snapshot(task_id)
        artifacts = []
        for index, _ in enumerate(result.get("artifacts", [])):
            _, item = self.runtime.artifact_path(task_id, index)
            artifacts.append({"artifact_id": item.get("artifact_identity", f"{task_id}:{index}"), "sha256": item["sha256"], "size_bytes": item["size_bytes"]})
        log = self.runtime.task_dir(task_id) / "executor.log"
        log_sha = hash_file(log) if log.is_file() else None
        if result.get("executor_log_sha256") != log_sha:
            raise WorkbenchError("Executor log differs from the completed result evidence")
        if row["worktree"] and result.get("diff_sha256"):
            if run_git(Path(row["worktree"]), ["rev-parse", "HEAD"]) != row["base_head"]:
                raise WorkbenchError("Review workspace HEAD moved from the bound baseline")
            live, _ = self.runtime.snapshot_diff(Path(row["worktree"]), self.runtime.task_dir(task_id),
                input_paths=[e["destination"] for e in snapshot["inputs"]])
            if digest(live) != result["diff_sha256"]:
                raise WorkbenchError("Worktree changed after completion; review identity invalid")
        if "command_runs" in result:
            from .restricted import capture_runs
            if capture_runs(self.runtime, task_id) != result["command_runs"]:
                raise WorkbenchError("Command run evidence changed after completion")
        return {"task_id": task_id, "work_unit_id": snapshot["work_unit_id"], "result_sha256": row["result_sha256"],
                "input_snapshot_sha256": snapshot["snapshot_sha256"], "base_head": row["base_head"],
                "project_binding_sha256": row["project_binding_sha256"], "diff_sha256": result.get("diff_sha256"),
                "executor_log_sha256": log_sha, "artifacts": artifacts,
                "command_runs_sha256": digest(canonical(result.get("command_runs", [])).encode()),
                "source_identities": [{"kind": "git_tree_and_diff", "baseline_git_head": row["base_head"], "diff_sha256": result.get("diff_sha256")},
                                      {"kind": "input_snapshot", "sha256": snapshot["snapshot_sha256"]}],
                "review_surface": "sealed_task_result_baseline_diff_inputs_logs_all_declared_artifacts"}

    def get_work_unit(self, work_unit_id):
        with self.runtime.connection() as c:
            unit = c.execute("SELECT * FROM loop_units WHERE id=?", (work_unit_id,)).fetchone()
            if not unit:
                raise WorkbenchError("Unknown Work Unit")
            self.runtime.project(unit["project"])
            tasks = c.execute("SELECT t.* FROM tasks t JOIN loop_snapshots s ON s.task_id=t.id WHERE s.unit_id=? ORDER BY t.created", (work_unit_id,)).fetchall()
            reviews = c.execute("SELECT r.id,r.task_id FROM loop_reviews r JOIN loop_snapshots s ON s.task_id=r.task_id WHERE s.unit_id=? ORDER BY r.rowid", (work_unit_id,)).fetchall()
        artifacts = []
        for row in tasks:
            for item in (json.loads(row["result"]) if row["result"] else {}).get("artifacts", []):
                artifacts.append({"artifact_id": item.get("artifact_identity"), "task_id": row["id"], "sha256": item["sha256"], "result_sha256": row["result_sha256"]})
        return {"work_unit_id": work_unit_id, "project_id": unit["project"],
                "substantive_question": json.loads(tasks[0]["spec"])["goal"] if tasks else None,
                "tasks": [{"task_id": r["id"], "status": r["status"], "result_sha256": r["result_sha256"]} for r in tasks],
                "reviews": [dict(r) for r in reviews], "artifacts": artifacts, "next_action": "inspect_evidence_and_choose_next_task",
                "authority": "navigation_only"}

    def record_review_object(self, task_id, expected_result_sha256, expected_snapshot_sha256, verdict, note, idempotency_key):
        if verdict not in ("PASS", "REVISE") or not isinstance(note, str) or not note.strip() or len(note) > 5000:
            raise WorkbenchError("Review requires PASS/REVISE and a substantive bounded note")
        sha(expected_result_sha256)
        sha(expected_snapshot_sha256)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise WorkbenchError("Stable review key required")
        evidence = self.evidence(task_id)
        if evidence["result_sha256"] != expected_result_sha256 or evidence["input_snapshot_sha256"] != expected_snapshot_sha256:
            raise WorkbenchError("Review target differs from explicitly supplied evidence")
        semantic = {"schema": "wb.review.v1", "evidence": evidence, "verdict": verdict, "note": note,
                    "authority": "evidence_review_only", "human_acceptance": False}
        fingerprint = digest(canonical(semantic).encode())
        with self.runtime.connection(write=True) as c:
            old = c.execute("SELECT * FROM loop_reviews WHERE task_id=? AND request_key=?", (task_id, idempotency_key)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise WorkbenchError("Review key reused for different evidence or disposition")
                return json.loads(old["body"])
            body = {**semantic, "created_at": time.time()}
            ident = "review_" + digest(canonical(body).encode())
            body["review_id"] = ident
            c.execute("INSERT INTO loop_reviews VALUES(?,?,?,?,?)", (ident, task_id, idempotency_key, fingerprint, canonical(body)))
            return body

    def latest_review(self, task_id):
        with self.runtime.connection() as c:
            row = c.execute("SELECT body FROM loop_reviews WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            raise WorkbenchError("Trial task requires an immutable Review Object before apply")
        body = json.loads(row["body"])
        identity = body.pop("review_id")
        if "review_" + digest(canonical(body).encode()) != identity or body["evidence"] != self.evidence(task_id):
            raise WorkbenchError("Review Object identity/evidence changed")
        return {**body, "review_id": identity}

    def get_review_object(self, review_id):
        with self.runtime.connection() as c:
            row = c.execute("SELECT body,task_id FROM loop_reviews WHERE id=?", (review_id,)).fetchone()
        if not row:
            raise WorkbenchError("Unknown Review Object")
        self.runtime.get_row(row["task_id"])
        body = json.loads(row["body"])
        unsigned = {k: v for k, v in body.items() if k != "review_id"}
        if "review_" + digest(canonical(unsigned).encode()) != review_id:
            raise WorkbenchError("Review Object content identity changed")
        try:
            current = body["evidence"] == self.evidence(row["task_id"])
            reason = None if current else "Evidence identity differs"
        except WorkbenchError as e:
            current, reason = False, str(e)
        return {"object": body, "current_evidence_valid": current, "invalidation_reason": reason}
