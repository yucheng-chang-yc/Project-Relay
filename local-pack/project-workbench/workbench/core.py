from __future__ import annotations

import contextlib
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

ACTIVE = ("queued", "running", "cancelling", "interrupted")
TERMINAL = ("completed", "failed", "cancelled", "timed_out", "interrupted")
MAX_TEXT = 256 * 1024
MAX_DIRECTORY_ENTRIES = 10000
MAX_HASH_BYTES = 500 * 1024 * 1024


# Windows: the detached worker has no console, so each console child (git, CLIs) would get a new console
# (measured 0.6-0.9 s per spawn). CREATE_NO_WINDOW avoids it; stdio stays piped.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class WorkbenchError(Exception):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(data: bytes):
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def process_alive(pid):
    """Conservative local check: unknown/reused/live PIDs keep an uncertain writer locked."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() != 87  # invalid PID; access denial is uncertain
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError, IndexError):
        return True


def canonical_path(path):
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def metadata_part(part):
    # Use the Windows-equivalent form consistently on both platforms.
    return part.rstrip(" .").casefold() == ".git"


def safe_path(root: Path, relative: str):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise WorkbenchError("A nonempty project-relative path is required")
    # Use portable slash paths and disallow Windows drive/ADS syntax everywhere.
    if "\\" in relative or ":" in relative or relative.startswith("/"):
        raise WorkbenchError("Use a relative slash-separated path")
    parts = Path(relative).parts
    if any(p == ".." or metadata_part(p) for p in parts):
        raise WorkbenchError("Path is outside the authorized file surface")
    try:
        target = root.joinpath(relative).resolve()
    except (OSError, RuntimeError) as e:
        raise WorkbenchError("Path cannot be resolved safely") from e
    if not target.is_relative_to(root.resolve()):
        raise WorkbenchError("Symlink/path escapes the registered project")
    if any(metadata_part(p) for p in target.relative_to(root.resolve()).parts):
        raise WorkbenchError("Resolved path enters Git metadata")
    return target


def run_git(root, args, *, env=None, binary=False, timeout=20):
    run_env = os.environ.copy()
    run_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_LITERAL_PATHSPECS": "1"})
    if env:
        run_env.update(env)
    try:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           timeout=timeout, env=run_env, shell=False, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WorkbenchError(f"Git could not finish: {type(e).__name__}") from e
    if p.returncode:
        raise WorkbenchError(p.stderr.decode("utf-8", "replace")[:2000].strip() or "Git failed")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace").strip()


class Runtime:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self.data = Path(self.config["data_dir"]).expanduser().resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = self.data / "workbench.sqlite3"
        self.projects = {}
        roots = set()
        for project in self.config.get("projects", []):
            ident = project["id"]
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", ident) or ident in self.projects:
                raise WorkbenchError("Project IDs must be unique lowercase portable names")
            root = Path(project["root"]).expanduser().resolve()
            if not root.is_dir():
                raise WorkbenchError(f"Registered project directory does not exist: {ident}")
            if self.data == root or self.data.is_relative_to(root) or root.is_relative_to(self.data):
                raise WorkbenchError("Runtime data and project roots must be separate")
            identity = canonical_path(root)
            if identity in roots:
                raise WorkbenchError("Multiple Project IDs cannot share a canonical project root")
            roots.add(identity)
            self.projects[ident] = {**project, "root": str(root)}
        with self.connection() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL, spec TEXT NOT NULL,
                status TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending',
                review_verdict TEXT, review_note TEXT, created REAL NOT NULL, started REAL,
                finished REAL, heartbeat REAL, worker_pid INTEGER, child_pid INTEGER,
                cancel_requested INTEGER NOT NULL DEFAULT 0, base_head TEXT, worktree TEXT,
                exit_code INTEGER, error TEXT, result TEXT, result_sha256 TEXT,
                retrieved INTEGER NOT NULL DEFAULT 0, applied INTEGER NOT NULL DEFAULT 0,
                project_root TEXT, repo_identity TEXT, project_binding_sha256 TEXT,
                UNIQUE(project,idempotency_key));
              CREATE TABLE IF NOT EXISTS task_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at REAL NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS task_project_status ON tasks(project,status);
            """)
        # Add nullable columns without inventing authority for historical rows.
        with self.connection(write=True) as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            for name in ("project_root", "repo_identity", "project_binding_sha256"):
                if name not in columns:
                    c.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
            if "archived" not in columns:
                c.execute("ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            c.execute("""CREATE TABLE IF NOT EXISTS task_progress (
                task_id TEXT PRIMARY KEY, phase TEXT NOT NULL, stages TEXT NOT NULL,
                partial_result TEXT, executor_exit_code INTEGER, updated REAL NOT NULL)""")
        from .loop import LoopStore
        self.loop = LoopStore(self)
        from .events import EventStore
        self.events = EventStore(self)
        from .filesnap import FileSnapshotStore
        self.files = FileSnapshotStore(self)
        from .shared import SharedFolderStore
        self.shared = SharedFolderStore(self)

    @contextlib.contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        if write:
            c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def project(self, ident, writable=False):
        if ident not in self.projects:
            raise WorkbenchError("Unknown registered project")
        p = self.projects[ident]
        if writable and not p.get("writable", False):
            raise WorkbenchError("Project is registered read-only")
        return p, Path(p["root"])

    def ensure_idle(self, c, project):
        _, root = self.project(project)
        repo = self.repository_identity(root)
        placeholders = ",".join("?" for _ in ACTIVE)
        row = c.execute(f"SELECT id,status FROM tasks WHERE (project=? OR project_root=? OR repo_identity=?"
                        " OR project_binding_sha256 IS NULL)"
                        f" AND status IN ({placeholders})", (project, canonical_path(root), repo, *ACTIVE)).fetchone()
        if row:
            raise WorkbenchError(f"Project has an active/uncertain task: {row['id']} ({row['status']})")

    @staticmethod
    def repository_identity(root, required=False):
        try:
            common = Path(run_git(root, ["rev-parse", "--git-common-dir"]))
            return canonical_path(common if common.is_absolute() else root / common)
        except WorkbenchError:
            if required:
                raise
            return None

    @staticmethod
    def binding_hash(project):
        fields = ("writable", "profiles", "codex_command", "claude_command",
                  "claude_allowed_tools", "claude_max_turns")
        policy = {k: project.get(k) for k in fields}
        if project.get("compute_runtime") is not None:
            policy["compute_runtime"] = project["compute_runtime"]
        policy["root"] = canonical_path(project["root"])
        return digest(canonical(policy).encode("utf-8"))

    def assert_config_current(self, project_id):
        project, _ = self.project(project_id)
        try:
            current = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            matches = [p for p in current.get("projects", []) if p.get("id") == project_id]
            valid = (canonical_path(current["data_dir"]) == canonical_path(self.data) and
                     len(matches) == 1 and self.binding_hash(matches[0]) == self.binding_hash(project))
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            valid = False
        if not valid:
            raise WorkbenchError("Project configuration changed; restart the facade and resolve its binding")

    def validate_task_binding(self, row):
        if not row["project_binding_sha256"]:
            raise WorkbenchError("Legacy task has no verified project binding; integration requires local review")
        project, root = self.project(row["project"], writable=True)
        self.assert_config_current(row["project"])
        if (row["project_root"] != canonical_path(root) or
                row["project_binding_sha256"] != self.binding_hash(project) or
                row["repo_identity"] != self.repository_identity(root, required=True)):
            raise WorkbenchError("Task project/executor binding changed; execution or integration stopped")

    @staticmethod
    def event(c, task_id, event, data=None):
        c.execute("INSERT INTO task_events(task_id,at,event,data) VALUES(?,?,?,?)",
                  (task_id, time.time(), event, canonical(data or {})))

    def list_projects(self):
        return [{"id": p["id"], "name": p.get("name", p["id"]), "root": p["root"],
                 "writable": p.get("writable", False), "profiles": list(p.get("profiles", {})),
                 "executors": [k for k in ("codex", "claude") if p.get(k + "_command")]}
                for p in self.projects.values()]

    def get_project_capabilities(self, project_id):
        from . import __version__
        from .adapters import configured_command, claude_policy, command_profile
        p, _ = self.project(project_id)
        self.assert_config_current(project_id)
        executors = {}
        for kind in ("codex", "claude"):
            item = {"configured": False, "authentication": "unknown", "model_usable": "unknown"}
            try:
                configured_command(kind, p)
                if kind == "claude":
                    policy = claude_policy(p)
                    bash = "unrestricted" if "Bash" in policy["allowed"] else (
                        "scoped" if "Bash" in policy["tools"] else "disabled")
                    # Tool names are fixed; rules and raw argv may contain credentials.
                    permissions = {tool: "unrestricted" if tool in policy["allowed"] else
                                   "scoped" if any(v.startswith(tool + "(") for v in policy["allowed"]) else
                                   "disabled" for tool in policy["tools"]}
                    item.update({"enabled_builtin_tools": policy["tools"], "permission_summary": permissions,
                                 "bash_policy": bash, "max_turns": policy["max_turns"],
                                 "permission_mode": "dontAsk"})
                else:
                    item["sandbox"] = "workspace-write"
                item["configured"] = True
            except WorkbenchError:
                item["configuration_issue"] = "missing_or_invalid_executor_policy"
            executors[kind] = item
        profiles = {}
        for name in p.get("profiles", {}):
            try:
                command_profile(p, name)
                profiles[name] = "configured"
            except WorkbenchError:
                profiles[name] = "invalid"
        compute = {"configured": False}
        if p.get("compute_runtime") is not None:
            from .restricted import NATIVE_BACKEND, validate_policy
            try:
                policy = validate_policy(p["compute_runtime"])
                native = policy.get("backend") == NATIVE_BACKEND
                # Engine paths/images stay local; report only what the planner needs to know.
                compute = {"configured": True, "backend": policy["backend"], "executor": "claude",
                           "profiles": ["Rscript"] if native else ["Rscript", "Python"],
                           "os_containment": False if native else "operator_attested_container",
                           "trust": "local_account_without_containment" if native else "container"}
            except WorkbenchError:
                compute = {"configured": False, "configuration_issue": "invalid_compute_runtime_policy"}
        from .filesnap import settings as snapshot_settings
        snapshots = snapshot_settings(self)
        return {"project_id": project_id, "runtime_version": __version__,
                "computational_runtime": compute,
                "project_binding_sha256": self.binding_hash(p), "writable": bool(p.get("writable", False)),
                "features": {"text_read": True, "text_write": True, "text_search": True,
                    "directory_listing": True, "file_metadata": True, "git_read": True,
                    "command_tasks": True, "codex_tasks": True, "claude_tasks": True,
                    "task_results": True, "reviewed_integration": True,
                    "reference_inputs": True, "binary_host_transfer": False,
                    "explicit_input_snapshot": True, "prior_artifact_staging": True,
                    "artifact_resources": True, "immutable_review_objects": True,
                    "computational_runtime_verified": False,
                    "native_trusted_r": compute.get("trust") == "local_account_without_containment",
                    "project_sessions": False,
                    "registry_mutation": False, "readonly_task_concurrency": False, "mcp_events": True,
                    "single_file_snapshot": True, "shared_folders": True,
                    "task_archives": True, "partial_task_evidence": True},
                "shared_folder_detail": {"project_required": False, "git_required": False,
                    "authorization": "exact_folder_and_scope_user_approval_until_revoked",
                    "max_bytes": 20971520, "chunk_bytes": 262144, "chatgpt_acceptance": "unverified",
                    "os_containment": False},
                "mcp_events_detail": {"server_implementation": "implemented", "protocol_version": "2026-07-28",
                    "discovery": "server/discover capabilities.events", "events": ["task.finished"], "delivery": ["webhook"],
                    "payload": ["task_id", "status", "result_sha256"],
                    "chatgpt_notification": "verified_once_in_separate_notification_conversation",
                    "original_conversation_continuation": "unverified"},
                "single_file_snapshot_detail": {"project_required": False,
                    "authorization": "per_file_user_approval_in_widget_with_server_verified_single_use_token",
                    "tools": ["request_file_snapshot", "get_file_snapshot", "read_file_snapshot_bytes"],
                    "max_bytes": snapshots["max_bytes"], "chunk_bytes": 262144,
                    "snapshot_ttl_seconds": snapshots["snapshot_ttl_seconds"],
                    "chatgpt_acceptance": "verified_2026-10-03", "chatgpt_verified_max_bytes": 97520,
                    "chatgpt_verified": ["card_approval", "zip_sha_and_extract", "immutable_after_source_change",
                                         "same_key_retry", "deny"],
                    "not_verified_in_chatgpt": ["expiry", "files_over_97520_bytes"]},
                "executors": executors, "command_profiles": profiles,
                "limits": {"text_window_bytes": MAX_TEXT, "utf8_boundary_extra_bytes": 3,
                    "directory_page_entries": 200, "directory_scan_entries": MAX_DIRECTORY_ENTRIES,
                    "file_hash_bytes": MAX_HASH_BYTES, "task_timeout_seconds": 3600,
                    "input_bytes": 20971520, "task_input_bytes": 104857600,
                    "binary_chunk_bytes": 262144, "full_resource_bytes": 20971520},
                "task_baseline": "committed_HEAD_plus_explicit_staged_inputs", "all_tasks_require_writable_project": True,
                "writer_lock_scope": "shared_runtime_database_and_git_common_directory",
                "local_binary_artifact_download": True,
                "external_execution_state": "unknown; no model or executor was launched"}

    def preflight_task(self, project_id, executor, required_capabilities=None, profile=None):
        from .adapters import configured_command, claude_policy, command_profile
        p, root = self.project(project_id)
        capabilities = self.get_project_capabilities(project_id)
        checks = []
        def check(name, passed, detail):
            checks.append({"name": name, "status": "pass" if passed else "blocked", "detail": detail})
        check("writable_project", bool(p.get("writable", False)), "All current task kinds require a writable registration")
        command = None
        try:
            if executor == "command":
                command = command_profile(p, profile)
            elif executor in ("codex", "claude"):
                command = configured_command(executor, p)
                if executor == "claude":
                    claude_policy(p)
            else:
                raise WorkbenchError("Choose command, codex or claude")
            check("executor_policy", True, "Configured argv and adapter policy are valid")
        except WorkbenchError as e:
            check("executor_policy", False, str(e))
        if command:
            check("executable_available", shutil.which(command[0]) is not None,
                  "Executable lookup in the current runtime environment; authentication is unverified")
        try:
            head = run_git(root, ["rev-parse", "HEAD"])
            exact = canonical_path(run_git(root, ["rev-parse", "--show-toplevel"])) == canonical_path(root)
            check("committed_repository_root", exact, "Task baseline requires committed HEAD and the exact Git root")
        except WorkbenchError:
            head = None
            check("committed_repository_root", False, "A committed Git HEAD at the exact registered root is required")
        with self.connection() as c:
            try:
                self.ensure_idle(c, project_id)
                check("writer_idle", True, "No active or uncertain task shares the project/root/Git common directory")
            except WorkbenchError as e:
                check("writer_idle", False, str(e))
        for name in required_capabilities or []:
            if name == "shell_execution":
                if executor == "command":
                    check(name, False, "Command tasks only run their named fixed argv; no general shell is exposed")
                elif executor == "claude" and capabilities["executors"]["claude"].get("bash_policy") == "disabled":
                    check(name, False, "Claude Bash is disabled by the configured adapter policy")
                else:
                    checks.append({"name": name, "status": "unknown", "detail":
                        "Specific commands depend on CLI permissions/sandbox; preflight does not interpret or run them"})
            elif name in ("authentication", "model_usable"):
                checks.append({"name": name, "status": "unknown", "detail": "No model or authentication probe is configured"})
            else:
                supported = capabilities["features"].get(name, False)
                if name == "text_write":
                    supported = supported and bool(p.get("writable", False))
                check(name, supported, "Runtime feature support; unknown feature names are blocked")
        statuses = {item["status"] for item in checks}
        return {"project_id": project_id, "executor": executor, "profile": profile,
                "status": "blocked" if "blocked" in statuses else "unknown" if "unknown" in statuses else "eligible",
                "checks": checks, "base_head": head,
                "project_binding_sha256": capabilities["project_binding_sha256"],
                "authentication": "unknown", "model_usable": "unknown", "task_created": False,
                "eligibility_scope": "Known local prerequisites only; start/worker/apply recheck binding. CLI success remains unverified."}

    def read_file(self, project_id, path, offset=0, limit=65536):
        _, root = self.project(project_id)
        p = safe_path(root, path)
        if not p.is_file():
            raise WorkbenchError("File does not exist")
        return self.read_text(p, offset, limit)

    @staticmethod
    def read_text(p, offset=0, limit=65536):
        if offset < 0 or not 1 <= limit <= MAX_TEXT:
            raise WorkbenchError("Invalid byte window")
        with p.open("rb") as f:
            f.seek(offset)
            b = f.read(limit)
            size = os.fstat(f.fileno()).st_size
            if offset and b and 0x80 <= b[0] <= 0xBF:
                # A standalone invalid continuation byte is still a lossy preview boundary.
                end = f.tell()
                start = max(0, offset - 3)
                f.seek(start)
                nearby = f.read(offset - start + 4)
                boundary = offset - start
                for index in range(boundary):
                    lead = nearby[index]
                    length = 2 if 0xC2 <= lead <= 0xDF else 3 if 0xE0 <= lead <= 0xEF else 4 if 0xF0 <= lead <= 0xF4 else 0
                    if index < boundary < index + length:
                        try:
                            nearby[index:index + length].decode("utf-8", "strict")
                        except UnicodeDecodeError:
                            continue
                        raise WorkbenchError("Offset starts inside a UTF-8 sequence; use a returned next_offset")
                f.seek(end)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            text = decoder.decode(b, final=False)
            # Complete one trailing character; a page may exceed limit by at most 3 bytes.
            for _ in range(3):
                if not decoder.getstate()[0]:
                    break
                extra = f.read(1)
                if not extra:
                    break
                b += extra
                text += decoder.decode(extra, final=False)
            text += decoder.decode(b"", final=True)
        try:
            b.decode("utf-8", "strict")
            lossy = False
        except UnicodeDecodeError:
            lossy = True
        return {"text": text, "offset": offset, "lossy": lossy, "representation": "utf-8-preview",
                "next_offset": offset + len(b), "size_bytes": size,
                "truncated": offset + len(b) < size, "sha256": hash_file(p)}

    def stat_file(self, project_id, path, include_sha256=False):
        _, root = self.project(project_id)
        target = safe_path(root, path)
        try:
            info = target.stat()
        except OSError as e:
            raise WorkbenchError("Authorized file or directory cannot be inspected") from e
        kind = "file" if stat.S_ISREG(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else None
        if not kind:
            raise WorkbenchError("Only regular files and directories are supported")
        result = {"path": path, "kind": kind, "size_bytes": info.st_size if kind == "file" else None,
                  "mtime_ns": info.st_mtime_ns, "is_alias": root / path != target, "sha256": None}
        if include_sha256:
            if kind != "file" or info.st_size > MAX_HASH_BYTES:
                raise WorkbenchError("Hash requires a regular file no larger than 500 MiB")
            result["sha256"] = hash_file(target)
            after = target.stat()
            if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise WorkbenchError("File changed while hashing; repeat stat_file")
        return result

    def list_directory(self, project_id, path=".", cursor=None, limit=100):
        _, root = self.project(project_id)
        directory = safe_path(root, path)
        if not directory.is_dir() or not 1 <= limit <= 200:
            raise WorkbenchError("Choose a directory and an entry limit from 1 to 200")
        entries, scanned, filtered = [], 0, 0
        with os.scandir(directory) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > MAX_DIRECTORY_ENTRIES:
                    raise WorkbenchError("Directory exceeds the 10000-entry listing limit")
                relative = (directory / entry.name).relative_to(root).as_posix()
                try:
                    item = self.stat_file(project_id, relative)
                except (WorkbenchError, OSError):
                    filtered += 1
                    continue
                item["name"] = entry.name
                entries.append(item)
        entries.sort(key=lambda item: item["name"])
        fingerprint = digest(canonical({"project_id": project_id, "path": path, "entries": entries}).encode("utf-8"))
        start = 0
        if cursor is not None:
            match = re.fullmatch(r"([0-9a-f]{64}):(\d{1,5})", cursor)
            if not match or match[1] != fingerprint:
                raise WorkbenchError("Directory cursor is stale or belongs to another listing")
            start = int(match[2])
            if start > len(entries):
                raise WorkbenchError("Directory cursor is outside the listing")
        page, encoded = [], 0
        for item in entries[start:start + limit]:
            size = len(canonical(item).encode("utf-8"))
            if page and encoded + size > MAX_TEXT:
                break
            page.append(item)
            encoded += size
        end = start + len(page)
        return {"path": path, "entries": page, "total_entries": len(entries), "filtered_entries": filtered,
                "snapshot_sha256": fingerprint, "next_cursor": f"{fingerprint}:{end}" if end < len(entries) else None}

    def write_file(self, project_id, path, text, expected_sha256):
        _, root = self.project(project_id, writable=True)
        self.assert_config_current(project_id)
        b = text.encode("utf-8")
        if len(b) > MAX_TEXT:
            raise WorkbenchError("Write exceeds the bounded text limit")
        p = safe_path(root, path)
        with self.connection(write=True) as c:
            self.ensure_idle(c, project_id)
            current = hash_file(p) if p.is_file() else ""
            if expected_sha256 != current:
                raise WorkbenchError("File precondition changed; read it again before writing")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".workbench-" + uuid.uuid4().hex)
            try:
                tmp.write_bytes(b)
                os.replace(tmp, p)
            finally:
                tmp.unlink(missing_ok=True)
        return {"path": path, "sha256": digest(b), "size_bytes": len(b)}

    def search_files(self, project_id, query, limit=50):
        _, root = self.project(project_id)
        if not query or not 1 <= limit <= 100:
            raise WorkbenchError("A query and bounded match limit are required")
        results = []
        scanned = 0
        for parent, dirs, files in os.walk(root, followlinks=False):
            # Junctions need the same resolved root and metadata fence as explicit reads.
            allowed = []
            for name in dirs:
                path = Path(parent) / name
                if name in ("node_modules", ".venv", "__pycache__") or path.is_symlink():
                    continue
                try:
                    safe_path(root, path.relative_to(root).as_posix())
                    allowed.append(name)
                except (WorkbenchError, OSError):
                    pass
            dirs[:] = allowed
            for name in files:
                scanned += 1
                if scanned > 10000:
                    return {"matches": results, "truncated": True, "files_scanned": scanned - 1}
                p = Path(parent) / name
                try:
                    safe_path(root, p.relative_to(root).as_posix())
                    if p.is_symlink() or not p.is_file() or p.stat().st_size > 1024 * 1024:
                        continue
                    content = p.read_text(encoding="utf-8")
                except (WorkbenchError, UnicodeError, OSError):
                    continue
                for n, line in enumerate(content.splitlines(), 1):
                    if query.casefold() in line.casefold():
                        results.append({"path": p.relative_to(root).as_posix(), "line": n, "text": line[:500]})
                        if len(results) == limit:
                            return {"matches": results, "truncated": True, "files_scanned": scanned}
        return {"matches": results, "truncated": False, "files_scanned": scanned}

    def git_status(self, project_id):
        _, root = self.project(project_id)
        head = run_git(root, ["rev-parse", "HEAD"])
        branch = run_git(root, ["branch", "--show-current"])
        status = run_git(root, ["status", "--porcelain=v1", "-z"], binary=True)
        return {"head": head, "branch": branch, "clean": not status,
                "porcelain": status.decode("utf-8", "replace").replace("\x00", "\n"),
                "remotes": run_git(root, ["remote"] ).splitlines()}

    def git_diff(self, project_id):
        _, root = self.project(project_id)
        raw = run_git(root, ["diff", "HEAD", "--"], binary=True)
        return {"diff": raw[:MAX_TEXT].decode("utf-8", "replace"), "truncated": len(raw) > MAX_TEXT,
                "sha256": digest(raw), "untracked_included": False}

    def start_task(self, project_id, kind, spec, idempotency_key):
        from .adapters import configured_command, claude_policy, command_profile
        p, root = self.project(project_id, writable=True)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise WorkbenchError("A stable portable idempotency key is required")
        if not 1 <= spec.get("timeout_seconds", 300) <= 3600:
            raise WorkbenchError("Task timeout must be between 1 and 3600 seconds")
        if not spec.get("goal") or not spec.get("acceptance"):
            raise WorkbenchError("Goal and acceptance criteria are required")
        if kind == "command":
            command_profile(p, spec.get("profile"))
        elif kind in ("codex", "claude"):
            configured_command(kind, p)
            if kind == "claude":
                claude_policy(p)
        else:
            raise WorkbenchError("Unknown task kind")
        for a in spec.get("artifacts", []):
            safe_path(root, a)
        spec = self.loop.prepare(project_id, kind, spec)
        fingerprint = digest(canonical({"kind": kind, "spec": spec}).encode())
        ident = "task_" + uuid.uuid4().hex
        with self.connection(write=True) as c:
            existing = c.execute("SELECT * FROM tasks WHERE project=? AND idempotency_key=?",
                                 (project_id, idempotency_key)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise WorkbenchError("Idempotency key reused for a different task")
                return self.public_task(existing)
            self.assert_config_current(project_id)
            self.ensure_idle(c, project_id)
            base = run_git(root, ["rev-parse", "HEAD"])
            gitroot = Path(run_git(root, ["rev-parse", "--show-toplevel"])).resolve()
            if canonical_path(gitroot) != canonical_path(root):
                raise WorkbenchError("PoC task roots must be the exact Git repository root")
            c.execute("INSERT INTO tasks(id,project,kind,idempotency_key,fingerprint,spec,status,created,base_head,"
                      "project_root,repo_identity,project_binding_sha256) VALUES(?,?,?,?,?,?, 'queued',?,?,?,?,?)",
                      (ident, project_id, kind, idempotency_key, fingerprint, canonical(spec), time.time(), base,
                       canonical_path(root), self.repository_identity(root, required=True), self.binding_hash(p)))
            self.loop.bind(c, ident, project_id, base, spec, self.binding_hash(p))
            self.event(c, ident, "task.queued")
        self.task_dir(ident).mkdir(parents=True)
        try:
            flags = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            with (self.task_dir(ident) / "worker.log").open("ab") as log:
                worker = subprocess.Popen([sys.executable, "-m", "workbench.worker", "--config",
                    str(self.config_path), "--task", ident], cwd=str(Path(__file__).resolve().parents[1]),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, **flags)
            with self.connection(write=True) as c:
                c.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (worker.pid, ident))
            # Reap only. Detached worker durability never depends on this daemon thread.
            threading.Thread(target=worker.wait, name="reap-" + ident, daemon=True).start()
        except OSError as e:
            self.finish(ident, "failed", error=f"Worker launch failed: {type(e).__name__}")
        return self.get_task(ident)

    def task_dir(self, ident):
        if not re.fullmatch(r"task_[0-9a-f]{32}", ident):
            raise WorkbenchError("Invalid task ID")
        return self.data / "runs" / ident

    def get_row(self, ident):
        self.task_dir(ident)
        with self.connection() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (ident,)).fetchone()
        if not row:
            raise WorkbenchError("Unknown task")
        self.project(row["project"])
        return row

    def public_task(self, row):
        d = dict(row)
        d["spec"] = json.loads(d["spec"])
        d.pop("result", None)
        d.pop("fingerprint", None)
        with self.connection() as c:
            progress = c.execute("SELECT phase,stages,executor_exit_code FROM task_progress WHERE task_id=?", (d["id"],)).fetchone()
        d["progress"] = {"phase": progress["phase"], "stages": json.loads(progress["stages"]),
                         "executor_exit_code": progress["executor_exit_code"]} if progress else None
        d["heartbeat_stale"] = d["status"] in ACTIVE and time.time() - (d["heartbeat"] or d["created"]) > 15
        return d

    def reconcile_interrupted(self):
        with self.connection() as c:
            rows = c.execute("SELECT id,status,heartbeat,created,worker_pid FROM tasks WHERE status IN ('queued','running','cancelling')").fetchall()
        for row in rows:
            if time.time() - (row["heartbeat"] or row["created"]) < 30:
                continue
            if row["worker_pid"] and process_alive(row["worker_pid"]):
                continue
            with self.connection(write=True) as c:
                current = c.execute("SELECT status,heartbeat,created FROM tasks WHERE id=?", (row["id"],)).fetchone()
                if current["status"] not in ("queued", "running", "cancelling") or time.time() - (current["heartbeat"] or current["created"]) < 30:
                    continue
                c.execute("UPDATE tasks SET status='interrupted',error=? WHERE id=?",
                    ("Worker disappeared before durable completion. Writer remains locked; inspect child process and recover locally. No automatic replay.", row["id"]))
                self.event(c, row["id"], "task.interrupted")
                self.events.enqueue(c, row["id"], "interrupted", None)

    def resolve_interrupted_locally(self, task_id, note):
        """Operator-only recovery; deliberately absent from MCP tools."""
        self.reconcile_interrupted()
        row = self.get_row(task_id)
        if row["status"] != "interrupted" or not note.strip():
            raise WorkbenchError("Only an interrupted task with an operator note can be resolved")
        if process_alive(row["worker_pid"]) or process_alive(row["child_pid"]):
            raise WorkbenchError("Owned/reused/uncertain process ID remains live; inspect and stop it locally first")
        if json.loads(row["spec"]).get("compute_runtime"):
            from .restricted import stop_owned
            stop_owned(self, task_id)
        self.finish(task_id, "failed", error="Interrupted execution resolved locally without replay: " + note[:2000])
        return self.get_task(task_id)

    def get_task(self, task_id):
        self.reconcile_interrupted()
        return self.public_task(self.get_row(task_id))

    def list_tasks(self, project_id=None, limit=50, archived=False, before_task_id=None):
        self.reconcile_interrupted()
        if project_id:
            self.project(project_id)
        if not 1 <= limit <= 100:
            raise WorkbenchError("Invalid task limit")
        ids = [project_id] if project_id else list(self.projects)
        if not ids:
            return []
        cursor = self.get_row(before_task_id) if before_task_id else None
        if cursor and project_id and cursor["project"] != project_id:
            raise WorkbenchError("Task cursor belongs to another project")
        window = " AND (created<? OR (created=? AND id<?))" if cursor else ""
        position = (cursor["created"], cursor["created"], cursor["id"]) if cursor else ()
        with self.connection() as c:
            rows = c.execute(f"SELECT * FROM tasks WHERE project IN ({','.join('?' for _ in ids)}) AND archived=?"
                             + window + " ORDER BY created DESC,id DESC LIMIT ?", (*ids, int(archived), *position, limit)).fetchall()
        tasks = [self.public_task(r) for r in rows]
        for task in tasks:
            task["spec"] = {"goal": task["spec"]["goal"][:1000]}
        return tasks

    def archive_task(self, task_id, archived=True):
        self.get_row(task_id)
        with self.connection(write=True) as c:
            row = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row["status"] not in ("completed", "failed", "cancelled", "timed_out"):
                raise WorkbenchError("Only settled tasks can be archived; resolve uncertain execution first")
            c.execute("UPDATE tasks SET archived=? WHERE id=?", (int(archived), task_id))
        return self.get_task(task_id)

    def archive_finished_tasks(self, project_id=None):
        ids = [project_id] if project_id else list(self.projects)
        if project_id:
            self.project(project_id)
        with self.connection(write=True) as c:
            count = 0
            for ident in ids:
                count += c.execute("UPDATE tasks SET archived=1 WHERE project=? AND archived=0 "
                    "AND status IN ('completed','failed','cancelled','timed_out')", (ident,)).rowcount
        return {"archived_count": count, "retained": "results, logs, artifacts, worktrees and review records"}

    def checkpoint_task(self, ident, phase, state, result=None, exit_code=None):
        """Persist actual stage evidence before postprocessing. Terminal result identity stays write-once."""
        if state not in ("running", "completed", "failed", "cancelled", "timed_out"):
            raise WorkbenchError("Invalid task stage state")
        with self.connection(write=True) as c:
            task = c.execute("SELECT status FROM tasks WHERE id=?", (ident,)).fetchone()
            if not task or task["status"] not in ACTIVE:
                raise WorkbenchError("Cannot change progress of a settled task")
            old = c.execute("SELECT * FROM task_progress WHERE task_id=?", (ident,)).fetchone()
            stages = json.loads(old["stages"]) if old else {}
            stages[phase] = {"status": state, "at": time.time()}
            c.execute("INSERT OR REPLACE INTO task_progress VALUES(?,?,?,?,?,?)", (ident, phase,
                canonical(stages), canonical(result) if result is not None else (old["partial_result"] if old else None),
                exit_code if exit_code is not None else (old["executor_exit_code"] if old else None), time.time()))

    def heartbeat(self, ident):
        with self.connection(write=True) as c:
            c.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time(), ident))
            row = c.execute("SELECT cancel_requested FROM tasks WHERE id=?", (ident,)).fetchone()
        return bool(row and row[0])

    def finish(self, ident, status, exit_code=None, result=None, error=None):
        with self.connection(write=True) as c:
            progress = c.execute("SELECT * FROM task_progress WHERE task_id=?", (ident,)).fetchone()
            if progress:
                exit_code = exit_code if exit_code is not None else progress["executor_exit_code"]
                if result is None and progress["partial_result"]:
                    result = json.loads(progress["partial_result"])
                stages = json.loads(progress["stages"])
                if error and stages.get(progress["phase"], {}).get("status") == "running":
                    stages[progress["phase"]] = {"status": "failed", "at": time.time()}
                if result is not None:
                    result = {**result, "stages": stages, "error_phase": progress["phase"] if error else None,
                              "execution_completed": stages.get("execution", {}).get("status") == "completed",
                              "integration": "not_performed", "complete": status == "completed"}
                c.execute("UPDATE task_progress SET stages=? WHERE task_id=?", (canonical(stages), ident))
            encoded = canonical(result) if result is not None else None
            current = c.execute("SELECT status,result,exit_code,error FROM tasks WHERE id=?", (ident,)).fetchone()
            if current and current["status"] in ("completed", "failed", "cancelled", "timed_out"):
                if (current["status"], current["result"], current["exit_code"], current["error"]) == (status, encoded, exit_code, error):
                    return
                raise WorkbenchError("Terminal task identity is write-once; create a follow-up task")
            result_sha256 = digest(encoded.encode()) if encoded else None
            c.execute("UPDATE tasks SET status=?,finished=?,heartbeat=?,exit_code=?,result=?,result_sha256=?,error=?"
                      " WHERE id=?", (status, time.time(), time.time(), exit_code, encoded,
                      result_sha256, error, ident))
            self.event(c, ident, "task." + status, {"exit_code": exit_code})
            # Same transaction: the terminal state and its MCP event outbox rows commit together.
            self.events.enqueue(c, ident, status, result_sha256)

    def cancel_task(self, task_id):
        self.get_row(task_id)
        with self.connection(write=True) as c:
            c.execute("UPDATE tasks SET cancel_requested=1,status=CASE WHEN status IN ('queued','running')"
                      " THEN 'cancelling' ELSE status END WHERE id=?", (task_id,))
            self.event(c, task_id, "task.cancel_requested")
        return self.get_task(task_id)

    def get_task_result(self, task_id):
        row = self.get_row(task_id)
        if row["status"] not in TERMINAL:
            raise WorkbenchError("Task has not reached a terminal/attention state")
        with self.connection(write=True) as c:
            c.execute("UPDATE tasks SET retrieved=1 WHERE id=?", (task_id,))
            self.event(c, task_id, "result.retrieved")
        task = self.public_task(row)
        task["retrieved"] = 1
        return {"task": task, "result": json.loads(row["result"]) if row["result"] else None,
                "result_sha256": row["result_sha256"], "error": row["error"]}

    def get_task_log(self, task_id, offset=0, limit=65536):
        self.get_row(task_id)
        p = self.task_dir(task_id) / "executor.log"
        return self.read_text(p, offset, limit) if p.exists() else {"text": "", "offset": 0, "next_offset": 0}

    def artifact_path(self, task_id, artifact_id):
        row = self.get_row(task_id)
        result = json.loads(row["result"]) if row["result"] else {}
        artifacts = result.get("artifacts", [])
        if type(artifact_id) is not int or not 0 <= artifact_id < len(artifacts):
            raise WorkbenchError("Unknown artifact")
        a = artifacts[artifact_id]
        p = safe_path(self.task_dir(task_id), a["stored_name"])
        if not p.is_file() or hash_file(p) != a["sha256"]:
            raise WorkbenchError("Artifact is missing or changed after completion")
        return p, a

    def read_task_artifact(self, task_id, artifact_id, offset=0, limit=65536):
        p, a = self.artifact_path(task_id, artifact_id)
        return {"artifact": a, **self.read_text(p, offset, limit)}

    def mark_task_reviewed(self, task_id, expected_result_sha256, verdict, note):
        row = self.get_row(task_id)
        if row["status"] != "completed" or not row["retrieved"]:
            raise WorkbenchError("Retrieve a completed task result before recording review")
        if row["result_sha256"] != expected_result_sha256:
            raise WorkbenchError("Result identity differs from the reviewed object")
        if verdict not in ("accept_changes", "revise") or not note.strip():
            raise WorkbenchError("A review verdict and substantive note are required")
        if self.loop.has_snapshot(task_id):
            snapshot = self.loop.snapshot(task_id)
            self.loop.record_review_object(task_id, expected_result_sha256, snapshot["snapshot_sha256"],
                "PASS" if verdict == "accept_changes" else "REVISE", note, "legacy-review-" + uuid.uuid4().hex)
        with self.connection(write=True) as c:
            c.execute("UPDATE tasks SET review_status='reviewed',review_verdict=?,review_note=? WHERE id=?",
                      (verdict, note, task_id))
            self.event(c, task_id, "review.recorded", {"verdict": verdict})
        return self.get_task(task_id)

    def snapshot_diff(self, worktree, run_dir, exclude_inputs=False, input_paths=None):
        index = Path(run_dir) / "snapshot.index"
        index.unlink(missing_ok=True)
        env = {"GIT_INDEX_FILE": str(index)}
        run_git(worktree, ["read-tree", "HEAD"], env=env)
        run_git(worktree, ["add", "-A", "--", "."], env=env)
        paths = input_paths or (["wb_inputs"] if exclude_inputs else [])
        if paths:
            run_git(worktree, ["reset", "--quiet", "HEAD", "--", *paths], env=env)
        diff = run_git(worktree, ["diff", "--cached", "--binary", "--full-index", "HEAD", "--"], env=env, binary=True)
        names = run_git(worktree, ["diff", "--cached", "--name-only", "-z", "HEAD", "--"], env=env, binary=True)
        return diff, [n.decode("utf-8", "strict") for n in names.split(b"\x00") if n]

    def apply_task_changes(self, task_id, expected_diff_sha256):
        row = self.get_row(task_id)
        self.validate_task_binding(row)
        _, root = self.project(row["project"], writable=True)
        result = json.loads(row["result"]) if row["result"] else {}
        if row["kind"] not in ("codex", "claude") or row["status"] != "completed" or row["review_verdict"] != "accept_changes":
            raise WorkbenchError("Only reviewed, accepted agent worktree changes can be applied")
        if not result.get("diff_sha256") or expected_diff_sha256 != result["diff_sha256"]:
            raise WorkbenchError("Diff identity differs from the reviewed object")
        if self.loop.has_snapshot(task_id) and self.loop.latest_review(task_id)["verdict"] != "PASS":
            raise WorkbenchError("Immutable Review Object requires revision; apply stopped")
        if row["applied"]:
            return {"task_id": task_id, "applied": True, "already_applied": True}
        worktree = Path(row["worktree"])
        with self.connection(write=True) as c:
            self.validate_task_binding(row)
            self.ensure_idle(c, row["project"])
            if run_git(root, ["rev-parse", "HEAD"]) != row["base_head"] or run_git(root, ["status", "--porcelain"]):
                raise WorkbenchError("Primary repository changed or is dirty; integration stopped")
            if run_git(worktree, ["rev-parse", "HEAD"]) != row["base_head"]:
                raise WorkbenchError("Executor changed worktree HEAD; integration stopped")
            live, names = self.snapshot_diff(worktree, self.task_dir(task_id),
                input_paths=[e["destination"] for e in json.loads(row["spec"]).get("inputs", [])])
            if digest(live) != expected_diff_sha256:
                raise WorkbenchError("Worktree changed after review; retrieve/review new evidence")
            for name in names:
                safe_path(root, name)
            if live:
                patch = self.task_dir(task_id) / "apply.patch"
                patch.write_bytes(live)
                run_git(root, ["apply", "--check", "--binary", str(patch)])
                run_git(root, ["apply", "--binary", str(patch)])
            c.execute("UPDATE tasks SET applied=1 WHERE id=?", (task_id,))
            self.event(c, task_id, "changes.applied")
        return {"task_id": task_id, "applied": True, "changed_files": names}

    def git_commit(self, project_id, paths, message, expected_head):
        _, root = self.project(project_id, writable=True)
        self.assert_config_current(project_id)
        if not paths or not message.strip() or len(message) > 500:
            raise WorkbenchError("Named files and a bounded commit message are required")
        paths = list(dict.fromkeys(paths))
        for path in paths:
            safe_path(root, path)
        with self.connection(write=True) as c:
            self.ensure_idle(c, project_id)
            if run_git(root, ["rev-parse", "HEAD"]) != expected_head:
                raise WorkbenchError("HEAD changed before commit")
            if run_git(root, ["diff", "--cached", "--name-only"]):
                raise WorkbenchError("Existing staged changes must be resolved before a scoped commit")
            try:
                run_git(root, ["add", "--", *paths])
                staged = run_git(root, ["diff", "--cached", "--name-only", "-z"], binary=True)
                names = {n.decode("utf-8") for n in staged.split(b"\x00") if n}
                if not names or not names.issubset(set(paths)):
                    raise WorkbenchError("Staged file set differs from the authorized file set")
                run_git(root, ["commit", "-m", message])
            except BaseException:
                run_git(root, ["reset", "-q", "HEAD", "--", *paths])
                raise
            head = run_git(root, ["rev-parse", "HEAD"])
        return {"head": head, "committed_files": sorted(names), "status": self.git_status(project_id)}

    def git_push(self, project_id, remote, branch, expected_head):
        _, root = self.project(project_id, writable=True)
        self.assert_config_current(project_id)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", remote) or remote.startswith("-"):
            raise WorkbenchError("A configured remote name is required")
        run_git(root, ["check-ref-format", "refs/heads/" + branch])
        with self.connection(write=True) as c:
            self.ensure_idle(c, project_id)
            if run_git(root, ["rev-parse", "HEAD"]) != expected_head:
                raise WorkbenchError("HEAD changed before push")
            if run_git(root, ["branch", "--show-current"]) != branch:
                raise WorkbenchError("Only the current named branch can be pushed")
            if remote not in run_git(root, ["remote"]).splitlines():
                raise WorkbenchError("Remote is not configured")
            run_git(root, ["push", "--porcelain", remote, "HEAD:refs/heads/" + branch], timeout=45)
            remote_head = run_git(root, ["ls-remote", remote, "refs/heads/" + branch], timeout=20).split()[0]
            if remote_head != expected_head:
                raise WorkbenchError("Remote verification differs from the intended commit")
        return {"remote": remote, "branch": branch, "head": remote_head, "verified": True}
