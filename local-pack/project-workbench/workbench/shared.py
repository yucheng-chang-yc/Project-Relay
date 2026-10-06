"""Durable folder grants, live revocation and bounded exact file exchange. No executor/Git.

The app-only approval tools use the same host visibility boundary as single-file snapshots.
Windows pins every directory with a handle that denies delete sharing. POSIX walks dir_fd
with O_NOFOLLOW. This is a file API boundary, not containment of local trusted processes.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .core import WorkbenchError, canonical, digest
from . import filesnap

MAX_BYTES = 20 * 1024 * 1024
CHUNK = 262144
TTL = 3600
PUBLICATION_FIX = "D005-v1"


def publication_rejected(error):
    """Only documented local filesystem rejection codes qualify, never unknown I/O.

    Even these codes require an independent unchanged-target check before releasing
    the attempt. A successful syscall followed by cleanup/receipt failure never qualifies.
    """
    return isinstance(error, OSError) and (
        error.errno in (errno.EACCES, errno.EPERM, errno.EEXIST, errno.EBUSY,
                        errno.EROFS, errno.EXDEV, errno.ENOENT, errno.ENOTDIR) or
        getattr(error, "winerror", None) in (2, 3, 5, 17, 19, 32, 33, 80, 183))


def folder_path(raw):
    path = filesnap.normalize_path(raw)
    if Path(path) == Path(Path(path).anchor):
        raise WorkbenchError("Choose a dedicated folder, not a drive/filesystem root")
    if any(p.lower() == ".git" for p in Path(path).parts):
        raise WorkbenchError("Git metadata cannot be a shared-folder root")
    return path


def relative_path(raw, directory=False):
    if directory and raw in ("", "."):
        return []
    if not isinstance(raw, str) or not raw or len(raw) > 1000:
        raise WorkbenchError("Use a bounded relative file path")
    if raw.startswith(("/", "\\")) or "\\" in raw or ":" in raw:
        raise WorkbenchError("Use a relative path with forward slashes")
    parts = raw.split("/")
    for p in parts:
        if (not p or p in (".", "..") or p.lower() == ".git" or p != p.rstrip(" .") or
                filesnap.RESERVED.fullmatch(p) or any(ord(c) < 32 or c in '*?"<>|' for c in p)):
            raise WorkbenchError("Path escape, Git metadata, links and device names are not allowed")
    return parts


def safe_info(info, directory=False):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkbenchError("Links and junctions are not allowed")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise WorkbenchError("A real directory is required")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkbenchError("Only ordinary files with one hard link are allowed")


class Directory:
    """Pinned directory capability; keep all ancestor guards alive for the operation."""
    def __init__(self, path, parent=None, name=None):
        self.path, self.fd, self.handle = Path(path), None, None
        if os.name == "nt":
            import ctypes
            handle = filesnap._k.CreateFileW(str(self.path), filesnap._GENERIC_READ,
                filesnap._FILE_SHARE_READ | filesnap._FILE_SHARE_WRITE, None, filesnap._OPEN_EXISTING,
                0x02000000 | 0x00200000, None)  # BACKUP_SEMANTICS + OPEN_REPARSE_POINT; no delete share.
            if handle in (None, filesnap._INVALID):
                raise WorkbenchError("The folder cannot be opened or is locked")
            self.handle = handle
            try:
                info = filesnap._Info()
                if not filesnap._k.GetFileInformationByHandle(handle, ctypes.byref(info)):
                    raise WorkbenchError("Folder identity cannot be confirmed")
                if not info.attributes & 0x10 or info.attributes & 0x400:
                    raise WorkbenchError("Links and junctions are not allowed")
                buf = ctypes.create_unicode_buffer(32768)
                if not filesnap._k.GetFinalPathNameByHandleW(handle, buf, len(buf), 0):
                    raise WorkbenchError("Folder location cannot be confirmed")
                final = buf.value[4:] if buf.value.startswith("\\\\?\\") else buf.value
                if os.path.normcase(final) != os.path.normcase(str(self.path)):
                    raise WorkbenchError("The folder resolves to a different location")
                st = os.stat(self.path, follow_symlinks=False)
                self.identity = canonical([st.st_dev, st.st_ino])
            except BaseException:
                self.close()
                raise
        else:
            try:
                self.fd = os.open(name if parent else str(self.path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=parent.fd if parent else None)
            except OSError as e:
                raise WorkbenchError("The folder cannot be opened; links and missing folders are refused") from e
            st = os.fstat(self.fd)
            self.identity = canonical([st.st_dev, st.st_ino])

    def close(self):
        if self.handle is not None:
            filesnap._k.CloseHandle(self.handle)
            self.handle = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def info(self, name):
        return os.stat(self.path / name, follow_symlinks=False) if os.name == "nt" else os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def open_file(self, name, flags, mode=0o600):
        if os.name == "nt":
            return os.open(self.path / name, flags | os.O_BINARY, mode)
        return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=self.fd)

    def unlink(self, name):
        if os.name == "nt":
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self.fd)


@contextlib.contextmanager
def pin(path, relative=()):
    """Pin the root's ancestors and then the requested descendant directories."""
    root = Path(path)
    if os.name == "nt" and filesnap._k.GetDriveTypeW(root.anchor) not in (2, 3, 6):
        raise WorkbenchError("Only writable local drives are supported")
    dirs = []
    try:
        current = Directory(root.anchor)
        dirs.append(current)
        for name in root.parts[1:]:
            current = Directory(current.path / name, current, name)
            dirs.append(current)
        root_id = current.identity
        for name in relative:
            current = Directory(current.path / name, current, name)
            dirs.append(current)
        yield current, root_id
    finally:
        for d in reversed(dirs):
            d.close()


def read_exact(directory, name):
    """Read one bounded ordinary file, detecting replacement and concurrent writers."""
    info = directory.info(name)
    safe_info(info)
    if info.st_size > MAX_BYTES:
        raise WorkbenchError("Shared files are limited to 20 MiB")
    if os.name == "nt":
        source = filesnap._WindowsSource(str(directory.path / name))
        try:
            opened = os.fstat(source.fd)
            safe_info(opened)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise WorkbenchError("The file changed while opening")
            before = source.info()
            with os.fdopen(os.dup(source.fd), "rb") as f:
                raw = f.read(MAX_BYTES + 1)
            if source.info() != before:
                raise WorkbenchError("The file changed during reading; retry")
        finally:
            source.close()
    else:
        fd = directory.open_file(name, os.O_RDONLY)
        with os.fdopen(fd, "rb") as f:
            before = os.fstat(f.fileno())
            safe_info(before)
            if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                raise WorkbenchError("The file changed while opening")
            raw = f.read(MAX_BYTES + 1)
            after = os.fstat(f.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise WorkbenchError("The file changed during reading; retry")
    final = directory.info(name)
    safe_info(final)
    if len(raw) > MAX_BYTES or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
        raise WorkbenchError("The file changed during reading; retry")
    return raw


class SharedFolderStore:
    def __init__(self, runtime):
        self.runtime = runtime
        self.uploads = runtime.data / "shared_uploads"
        with runtime.connection(write=True) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS shared_requests (
                    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, path TEXT NOT NULL,
                    access TEXT NOT NULL, state TEXT NOT NULL, approval_hash TEXT, expires REAL NOT NULL,
                    grant_id TEXT);
                CREATE TABLE IF NOT EXISTS shared_grants (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, access TEXT NOT NULL, identity TEXT NOT NULL,
                    created REAL NOT NULL, revoked REAL);
                CREATE TABLE IF NOT EXISTS shared_writes (
                    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, path TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE NOT NULL, expected_sha256 TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL,
                    expires REAL NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS shared_publish_attempts (
                    write_id TEXT PRIMARY KEY, started REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS shared_write_resolutions (
                    write_id TEXT PRIMARY KEY, at REAL NOT NULL, observed_sha256 TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS shared_publish_failures (
                    id INTEGER PRIMARY KEY, write_id TEXT NOT NULL, at REAL NOT NULL, phase TEXT NOT NULL,
                    error_type TEXT NOT NULL, errno INTEGER, winerror INTEGER, outcome TEXT NOT NULL);
            """)

    def request(self, path, access, idempotency_key):
        path = folder_path(path)
        if access not in ("read_only", "read_write") or not filesnap.KEY.fullmatch(idempotency_key):
            raise WorkbenchError("Invalid access or idempotency key")
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT * FROM shared_requests WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if (os.path.normcase(row["path"]), row["access"]) != (os.path.normcase(path), access):
                    raise WorkbenchError("This key belongs to a different folder/access request")
            else:
                ident = "sfr_" + secrets.token_hex(16)
                c.execute("INSERT INTO shared_requests VALUES(?,?,?,?,'pending',NULL,?,NULL)",
                          (ident, idempotency_key, path, access, time.time() + 900))
                row = c.execute("SELECT * FROM shared_requests WHERE id=?", (ident,)).fetchone()
            return self.describe(c, row)

    def describe(self, c, row):
        if row["state"] == "pending" and row["expires"] <= time.time():
            c.execute("UPDATE shared_requests SET state='expired',approval_hash=NULL WHERE id=?", (row["id"],))
            row = c.execute("SELECT * FROM shared_requests WHERE id=?", (row["id"],)).fetchone()
        grant = c.execute("SELECT * FROM shared_grants WHERE id=?", (row["grant_id"],)).fetchone()
        return {"request_id": row["id"], "path": row["path"], "access": row["access"],
                "status": "revoked" if grant and grant["revoked"] else ("awaiting_user_approval" if row["state"] == "pending" else row["state"]),
                "grant_id": row["grant_id"], "approval_expires_at": row["expires"],
                "scope": "This folder and all current/future subfolders and files, until revoked. No deletion or execution. Secrets in this folder are readable.",
                "host_boundary": "The computer running Project Relay; this does not select the ChatGPT device."}

    def get(self, request_id):
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT * FROM shared_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise WorkbenchError("Unknown shared-folder request")
            return self.describe(c, row)

    def prepare(self, request_id):
        token = secrets.token_urlsafe(32)
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT * FROM shared_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise WorkbenchError("Unknown shared-folder request")
            data = self.describe(c, row)
            if data["status"] == "awaiting_user_approval":
                c.execute("UPDATE shared_requests SET approval_hash=? WHERE id=?", (digest(token.encode()), request_id))
                data["approval_token"] = token
            return data

    def exclude_private_roots(self, path):
        root = Path(path)
        forbidden = [self.runtime.data, self.runtime.config_path.parent, Path(__file__).resolve().parents[1]]
        forbidden += [Path(p["root"]) for p in self.runtime.projects.values()]
        for other in forbidden:
            if root == other or root.is_relative_to(other) or other.is_relative_to(root):
                raise WorkbenchError("Choose a dedicated shared folder separate from runtime, config and registered projects")

    def approve(self, request_id, approval_token, decision):
        if decision not in ("approve", "deny"):
            raise WorkbenchError("Decision must be approve or deny")
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT * FROM shared_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise WorkbenchError("Unknown shared-folder request")
            data = self.describe(c, row)
            if data["status"] != "awaiting_user_approval" or not row["approval_hash"] or not hmac.compare_digest(digest(approval_token.encode()), row["approval_hash"]):
                raise WorkbenchError("This approval card is expired or invalid; refresh its status")
            if decision == "deny":
                c.execute("UPDATE shared_requests SET state='denied',approval_hash=NULL WHERE id=?", (request_id,))
            else:
                self.exclude_private_roots(row["path"])
                with pin(row["path"]) as (_, identity):
                    grant = "sfg_" + secrets.token_hex(16)
                    c.execute("INSERT INTO shared_grants VALUES(?,?,?,?,?,NULL)",
                              (grant, row["path"], row["access"], identity, time.time()))
                c.execute("UPDATE shared_requests SET state='active',grant_id=?,approval_hash=NULL WHERE id=?", (grant, request_id))
            return self.describe(c, c.execute("SELECT * FROM shared_requests WHERE id=?", (request_id,)).fetchone())

    def list(self):
        with self.runtime.connection() as c:
            return [{"grant_id": r["id"], "path": r["path"], "access": r["access"], "created": r["created"]}
                    for r in c.execute("SELECT * FROM shared_grants WHERE revoked IS NULL ORDER BY created DESC")]

    def revoke(self, grant_id):
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT * FROM shared_grants WHERE id=?", (grant_id,)).fetchone()
            if not row:
                raise WorkbenchError("Unknown shared-folder grant")
            if row["revoked"]:
                return {"grant_id": grant_id, "status": "revoked", "original_files": "unchanged"}
            c.execute("UPDATE shared_grants SET revoked=? WHERE id=?", (time.time(), grant_id))
            for row in c.execute("SELECT id FROM shared_writes WHERE grant_id=? AND state='pending'", (grant_id,)).fetchall():
                (self.uploads / row["id"]).unlink(missing_ok=True)
            c.execute("UPDATE shared_writes SET state=CASE WHEN id IN (SELECT write_id FROM shared_publish_attempts) "
                "THEN 'uncertain' ELSE 'revoked' END WHERE grant_id=? AND state='pending'", (grant_id,))
        return {"grant_id": grant_id, "status": "revoked", "original_files": "unchanged"}

    def grant(self, c, grant_id, write=False):
        row = c.execute("SELECT * FROM shared_grants WHERE id=?", (grant_id,)).fetchone()
        if not row or row["revoked"]:
            raise WorkbenchError("Shared-folder grant is absent or revoked")
        if write and row["access"] != "read_write":
            raise WorkbenchError("This folder is shared read-only")
        self.exclude_private_roots(row["path"])
        return row

    @contextlib.contextmanager
    def directory(self, grant, parts):
        with pin(grant["path"], parts) as (directory, identity):
            if identity != grant["identity"]:
                raise WorkbenchError("The authorized folder was replaced; request a new grant")
            yield directory

    def list_directory(self, grant_id, path="", offset=0, limit=100):
        parts = relative_path(path, directory=True)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise WorkbenchError("Invalid directory window")
        with self.runtime.connection(write=True) as c:
            grant = self.grant(c, grant_id)
            with self.directory(grant, parts) as d:
                entries = []
                with os.scandir(str(d.path) if os.name == "nt" else d.fd) as scan:
                    for scanned, item in enumerate(scan):
                        if scanned >= 10000:
                            raise WorkbenchError("Folder exceeds the 10,000-entry listing limit")
                        try:
                            relative_path(item.name)
                            info = item.stat(follow_symlinks=False)
                            safe_info(info, directory=stat.S_ISDIR(info.st_mode))
                        except (WorkbenchError, OSError):
                            continue
                        entries.append({"name": item.name, "type": "directory" if stat.S_ISDIR(info.st_mode) else "file", "size_bytes": info.st_size})
                entries.sort(key=lambda e: e["name"])
        page = entries[offset:offset+limit]
        return {"entries": page, "next_offset": offset+len(page) if offset+len(page) < len(entries) else None,
                "listing_sha256": digest(canonical(entries).encode()), "consistency": "live listing; concurrent changes may change pages"}

    def read(self, grant_id, path, expected_sha256=None, offset=0, limit=CHUNK):
        parts = relative_path(path)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= CHUNK:
            raise WorkbenchError("Invalid byte window")
        with self.runtime.connection(write=True) as c:
            grant = self.grant(c, grant_id)
            with self.directory(grant, parts[:-1]) as d:
                raw = read_exact(d, parts[-1])
            sha = digest(raw)
            if expected_sha256 is not None and expected_sha256 != sha:
                raise WorkbenchError("The live shared file changed; restart reading with its current SHA")
            if offset > len(raw):
                raise WorkbenchError("Offset is outside the file")
            chunk = raw[offset:offset+limit]
            return {"grant_id": grant_id, "path": path, "sha256": sha, "size_bytes": len(raw), "offset": offset,
                    "next_offset": offset+len(chunk), "eof": offset+len(chunk) == len(raw),
                    "chunk_sha256": digest(chunk), "base64": base64.b64encode(chunk).decode()}

    def mkdir(self, grant_id, path):
        parts = relative_path(path)
        with self.runtime.connection(write=True) as c:
            grant = self.grant(c, grant_id, write=True)
            with self.directory(grant, parts[:-1]) as d:
                try:
                    if os.name == "nt":
                        os.mkdir(d.path / parts[-1])
                    else:
                        os.mkdir(parts[-1], mode=0o700, dir_fd=d.fd)
                except FileExistsError:
                    safe_info(d.info(parts[-1]), directory=True)
        return {"grant_id": grant_id, "path": path, "type": "directory"}

    def expire(self, c):
        for r in c.execute("SELECT id FROM shared_writes WHERE state='pending' AND expires<=?", (time.time(),)).fetchall():
            (self.uploads / r["id"]).unlink(missing_ok=True)
            attempted = c.execute("SELECT 1 FROM shared_publish_attempts WHERE write_id=?", (r["id"],)).fetchone()
            c.execute("UPDATE shared_writes SET state=? WHERE id=?", ("uncertain" if attempted else "expired", r["id"]))

    def begin_write(self, grant_id, path, expected_sha256, sha256, size_bytes, idempotency_key):
        relative_path(path)
        if (not re.fullmatch(r"[0-9a-f]{64}", sha256) or (expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)) or
                type(size_bytes) is not int or not 0 <= size_bytes <= MAX_BYTES or not filesnap.KEY.fullmatch(idempotency_key)):
            raise WorkbenchError("Exact content SHA, existing SHA (empty means create), bounded size and key are required")
        with self.runtime.connection(write=True) as c:
            self.expire(c)
            grant = self.grant(c, grant_id, write=True)
            uncertain = c.execute("SELECT w.id FROM shared_writes w JOIN shared_grants g ON g.id=w.grant_id "
                "JOIN shared_publish_attempts a ON a.write_id=w.id WHERE g.path=? COLLATE NOCASE AND w.path=? COLLATE NOCASE "
                "AND w.state IN ('pending','uncertain')",
                (grant["path"], path)).fetchone()
            if uncertain:
                raise WorkbenchError("An uncertain publication for this path requires local recovery: " + uncertain["id"])
            row = c.execute("SELECT * FROM shared_writes WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                if (row["grant_id"], row["path"], row["expected_sha256"], row["sha256"], row["size"]) != (grant_id, path, expected_sha256, sha256, size_bytes):
                    raise WorkbenchError("This key belongs to another write")
            else:
                used = c.execute("SELECT COALESCE(SUM(size),0) FROM shared_writes WHERE state='pending'").fetchone()[0]
                if used + size_bytes > 100 * 1024 * 1024:
                    raise WorkbenchError("Pending shared writes exceed the 100 MiB limit")
                self.uploads.mkdir(exist_ok=True)
                ident = "sfw_" + secrets.token_hex(16)
                (self.uploads / ident).touch(mode=0o600)
                c.execute("INSERT INTO shared_writes VALUES(?,?,?,?,?,?,?,0,?,'pending')", (ident, grant_id, path, idempotency_key, expected_sha256, sha256, size_bytes, time.time()+TTL))
                row = c.execute("SELECT * FROM shared_writes WHERE id=?", (ident,)).fetchone()
            return self.write_receipt(row)

    def write_receipt(self, row):
        return {"write_id": row["id"], "grant_id": row["grant_id"], "path": row["path"], "status": row["state"],
                "next_offset": row["offset"], "size_bytes": row["size"], "sha256": row["sha256"]}

    def write_row(self, c, write_id):
        self.expire(c)
        row = c.execute("SELECT * FROM shared_writes WHERE id=?", (write_id,)).fetchone()
        if not row:
            raise WorkbenchError("Unknown shared write")
        self.grant(c, row["grant_id"], write=True)
        return row

    def write_chunk(self, write_id, offset, base64_data, chunk_sha256):
        try:
            if len(base64_data) > 349528:
                raise ValueError()
            raw = base64.b64decode(base64_data, validate=True)
        except (ValueError, TypeError) as e:
            raise WorkbenchError("Invalid bounded Base64 chunk") from e
        if not raw or len(raw) > CHUNK or digest(raw) != chunk_sha256 or type(offset) is not int or offset < 0:
            raise WorkbenchError("Invalid chunk, offset or chunk SHA")
        with self.runtime.connection(write=True) as c:
            row = self.write_row(c, write_id)
            if row["state"] != "pending" or offset+len(raw) > row["size"]:
                raise WorkbenchError("Write is not pending or chunk exceeds declared size")
            with (self.uploads / row["id"]).open("r+b") as f:
                if offset < row["offset"]:
                    f.seek(offset)
                    if offset+len(raw) > row["offset"] or f.read(len(raw)) != raw:
                        raise WorkbenchError("Retried chunk differs from stored bytes")
                elif offset == row["offset"]:
                    f.seek(offset)
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                    c.execute("UPDATE shared_writes SET offset=? WHERE id=?", (offset+len(raw), write_id))
                else:
                    raise WorkbenchError("Chunk offset must match next_offset")
            return self.write_receipt(c.execute("SELECT * FROM shared_writes WHERE id=?", (write_id,)).fetchone())

    def commit_write(self, write_id):
        progress = {"phase": "validation", "publication_invoked": False}
        try:
            return self._commit_write(write_id, progress)
        except Exception as error:
            # Runtime.connection has rolled back/closed first. Recovery metadata must
            # survive that rollback, and must not erase a genuine crash barrier.
            try:
                outcome, row = self.record_publication_failure(write_id, progress, error)
            except Exception:
                raise WorkbenchError("Shared write failed; diagnostic persistence could not be confirmed. "
                    "Inspect locally before retrying. write_id=" + write_id) from error
            codes = "error=" + type(error).__name__ + ", errno=" + str(getattr(error, "errno", None)) + ", winerror=" + str(getattr(error, "winerror", None))
            if outcome == "committed":
                receipt = self.write_receipt(row)
                receipt["warning"] = "Publication committed; follow-up step failed (phase=" + progress["phase"] + ", " + codes + ")"
                return receipt
            if outcome == "not_published":
                if progress["phase"] == "validation":
                    raise  # Preserve specific SHA/grant/size validation guidance.
                raise WorkbenchError("Shared write was not published. write_id=" + write_id + ", phase=" + progress["phase"] + ", " + codes +
                    ". Correct the local cause, then retry the same write ID while pending; grant reset is unnecessary.") from error
            if outcome == "uncertain":
                raise WorkbenchError("Shared publication is uncertain. write_id=" + write_id + ", phase=" + progress["phase"] + ", " + codes +
                    ". Do not replay. Inspect exact target/SHA locally and use maintenance/resolve_shared_write.py. "
                    "Renewing the grant or restarting Relay will not clear this state.") from error
            raise  # Unknown write, revoked grant or non-publication validation failure.

    def record_publication_failure(self, write_id, progress, error):
        """Persist only bounded codes/phase, never error text, file contents or secrets."""
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT w.*,g.path AS root,g.identity,g.revoked FROM shared_writes w "
                "JOIN shared_grants g ON g.id=w.grant_id WHERE w.id=?", (write_id,)).fetchone()
            if not row:
                return "unknown", None
            attempt = c.execute("SELECT 1 FROM shared_publish_attempts WHERE write_id=?", (write_id,)).fetchone()
            if row["state"] == "committed" and progress["phase"] in ("database_receipt", "staging_cleanup"):
                outcome = "committed"
            elif row["state"] == "committed":
                outcome = "validation_error"
            elif row["state"] == "resolved_not_replayed":
                outcome = "resolved_not_replayed"
            else:
                # No syscall invoked is decisive for this invocation. For syscall
                # rejection require known codes AND intact original bytes/identity.
                # An older attempt must never be cleared by a validation-only retry.
                rejected = progress["phase"] == "publication" and publication_rejected(error)
                if rejected:
                    try:
                        parts = relative_path(row["path"])
                        with pin(row["root"], parts[:-1]) as (d, identity):
                            if identity != row["identity"]:
                                raise WorkbenchError("Folder identity changed")
                            try:
                                observed = digest(read_exact(d, parts[-1]))
                            except FileNotFoundError:
                                observed = ""
                            rejected = observed == row["expected_sha256"]
                    except Exception:
                        rejected = False
                safe = progress.get("own_attempt", False) and (not progress["publication_invoked"] or rejected)
                outcome = "not_published" if not attempt or safe else "uncertain"
                if attempt and safe:
                    c.execute("DELETE FROM shared_publish_attempts WHERE write_id=?", (write_id,))
                    if row["state"] == "uncertain":
                        # Revocation/expiry can happen between journal and reacquiring
                        # the transaction; their staging deletion must remain final.
                        state = "revoked" if row["revoked"] else ("expired" if row["expires"] <= time.time() else "pending")
                        c.execute("UPDATE shared_writes SET state=? WHERE id=?", (state, write_id))
            c.execute("INSERT INTO shared_publish_failures(write_id,at,phase,error_type,errno,winerror,outcome) VALUES(?,?,?,?,?,?,?)",
                (write_id, time.time(), progress["phase"], type(error).__name__[:100],
                 getattr(error, "errno", None), getattr(error, "winerror", None), outcome))
            return outcome, row

    def _commit_write(self, write_id, progress):
        with self.runtime.connection(write=True) as c:
            row = self.write_row(c, write_id)
            if row["state"] == "committed":
                return self.write_receipt(row)
            if row["state"] != "pending" or row["offset"] != row["size"]:
                raise WorkbenchError("All declared bytes are required before commit")
            if c.execute("SELECT 1 FROM shared_publish_attempts WHERE write_id=?", (write_id,)).fetchone():
                raise WorkbenchError("Prior publication is uncertain; inspect locally and use maintenance/resolve_shared_write.py. "
                    "Grant reset/restart will not clear it. Do not replay. write_id=" + write_id)
            raw = (self.uploads / row["id"]).read_bytes()
            if len(raw) != row["size"] or digest(raw) != row["sha256"]:
                raise WorkbenchError("Staged file SHA/size differs from declared content")
            parts = relative_path(row["path"])
            grant = self.grant(c, row["grant_id"], write=True)
            with self.directory(grant, parts[:-1]) as d:
                name = parts[-1]
                try:
                    old = read_exact(d, name)
                    existing = digest(old)
                except FileNotFoundError:
                    existing = ""
                if existing != row["expected_sha256"]:
                    raise WorkbenchError("Existing file SHA changed; no overwrite was performed")
                temp = ".relay-write-" + secrets.token_hex(16)
                progress["phase"] = "temporary_file"
                fd = d.open_file(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                        f.flush()
                        os.fsync(f.fileno())
                    # Recheck immediately before atomic replacement. Local writers are trusted;
                    # filesystem APIs do not offer an atomic SHA-conditioned replacement.
                    try:
                        current = digest(read_exact(d, name))
                    except FileNotFoundError:
                        current = ""
                    if current != existing:
                        raise WorkbenchError("File changed before replacement; no overwrite was performed")
                    # Persist the attempt before changing the target. A process crash between
                    # filesystem publication and the DB receipt blocks replay rather than guessing.
                    c.execute("INSERT INTO shared_publish_attempts VALUES(?,?)", (write_id, time.time()))
                    progress["own_attempt"] = True
                    progress["phase"] = "prepublication"
                    c.commit()
                    c.execute("BEGIN IMMEDIATE")
                    live = self.write_row(c, write_id)
                    if live["state"] != "pending":
                        raise WorkbenchError("The write was revoked or expired before publication")
                    try:
                        latest = digest(read_exact(d, name))
                    except FileNotFoundError:
                        latest = ""
                    if latest != existing:
                        raise WorkbenchError("File changed before replacement; no overwrite was performed")
                    progress["phase"] = "publication"
                    progress["publication_invoked"] = True
                    if existing == "":
                        # Hard-link publication refuses a concurrent create (no clobber).
                        if os.name == "nt":
                            os.link(d.path / temp, d.path / name)
                        else:
                            os.link(temp, name, src_dir_fd=d.fd, dst_dir_fd=d.fd, follow_symlinks=False)
                    elif os.name == "nt":
                        os.replace(d.path / temp, d.path / name)
                    else:
                        os.replace(temp, name, src_dir_fd=d.fd, dst_dir_fd=d.fd)
                except BaseException:
                    # Preserve the first failure and its phase even if private-temp
                    # cleanup also fails. A process interruption keeps the attempt.
                    try:
                        d.unlink(temp)
                    except Exception:
                        pass
                    raise
                else:
                    progress["phase"] = "temporary_cleanup"
                    try:
                        d.unlink(temp)
                    except FileNotFoundError:
                        pass  # Atomic overwrite already moved this file to the target.
            progress["phase"] = "database_receipt"
            c.execute("UPDATE shared_writes SET state='committed' WHERE id=?", (write_id,))
            receipt = self.write_receipt(c.execute("SELECT * FROM shared_writes WHERE id=?", (write_id,)).fetchone())
        progress["phase"] = "staging_cleanup"
        (self.uploads / row["id"]).unlink(missing_ok=True)
        return receipt

    def resolve_write_locally(self, write_id, observed_sha256, note):
        """Local operator only, never exposed as MCP: resolve without replaying or changing originals."""
        if not note.strip() or (observed_sha256 and not re.fullmatch(r"[0-9a-f]{64}", observed_sha256)):
            raise WorkbenchError("Inspect locally and provide observed SHA (empty means absent) and a substantive note")
        with self.runtime.connection(write=True) as c:
            row = c.execute("SELECT w.*,g.identity,g.path AS root FROM shared_writes w JOIN shared_grants g "
                "ON g.id=w.grant_id WHERE w.id=?", (write_id,)).fetchone()
            if not row or row["state"] not in ("pending", "uncertain") or not c.execute("SELECT 1 FROM shared_publish_attempts WHERE write_id=?", (write_id,)).fetchone():
                raise WorkbenchError("Only an uncertain pending publication can be resolved")
            parts = relative_path(row["path"])
            with pin(row["root"], parts[:-1]) as (d, identity):
                if identity != row["identity"]:
                    raise WorkbenchError("Folder identity changed; inspect and recover the original folder locally")
                try:
                    actual = digest(read_exact(d, parts[-1]))
                except FileNotFoundError:
                    actual = ""
                if actual != observed_sha256:
                    raise WorkbenchError("Observed target SHA differs from current bytes")
            c.execute("UPDATE shared_writes SET state='resolved_not_replayed' WHERE id=?", (write_id,))
            c.execute("INSERT INTO shared_write_resolutions VALUES(?,?,?,?)", (write_id, time.time(), actual, note[:1000]))
        (self.uploads / row["id"]).unlink(missing_ok=True)
        return {"write_id": write_id, "status": "resolved_not_replayed", "observed_sha256": actual,
                "operator_note": note[:1000], "original_files": "unchanged by recovery"}
