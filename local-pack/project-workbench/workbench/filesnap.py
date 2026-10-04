"""Project-independent single-file read-only snapshots, authorized once per file by a Human.

Authority model (no project registration, Git repository or executor task):
- The model may only *request* a snapshot of one exact absolute path. A request reads nothing.
- The Workbench widget (and only it) obtains a single-use approval token for that request from the
  app-only tool `prepare_file_snapshot_approval`; the server stores only its SHA-256. The model-visible
  result carries just the non-secret request_id (ChatGPT does not reliably forward result `_meta` to widgets).
  The approving tool is app-only too (hidden from the model) and checks the token.
- The Human sees the exact path in the widget and approves or denies it. A local operator can approve
  instead with scripts/approve_file_snapshot.py. Either way the server verifies the decision itself;
  a model-supplied "authorized" flag is never accepted.
- An approved request reads the file once into an immutable copy. Its opaque snapshot ID then grants
  bounded reads of exactly those bytes until expiry, without further approval. Nothing grants folder
  listing, other files, modification or execution. A new path, a new idempotency key, or an expired
  snapshot needs a new approval.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import stat
import time

from .core import WorkbenchError, digest

MAX_CHUNK = 256 * 1024
READ_BLOCK = 1024 * 1024
READ_ATTEMPTS = 3
CAPTURE_STALE_SECONDS = 600
DEFAULTS = {"max_bytes": 20 * 1024 * 1024, "snapshot_ttl_seconds": 24 * 3600,
            "approval_ttl_seconds": 15 * 60, "max_store_bytes": 1024 * 1024 * 1024}
BOUNDS = {"max_bytes": (1, 500 * 1024 * 1024), "snapshot_ttl_seconds": (60, 7 * 24 * 3600),
          "approval_ttl_seconds": (60, 3600), "max_store_bytes": (1, 10 * 1024 * 1024 * 1024)}
RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3]|conin\$|conout\$|clock\$)(\..*)?")
KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_on_block = None  # Test hook: called after each block read during capture.

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ, _FILE_SHARE_WRITE = 0x1, 0x2
    _OPEN_EXISTING = 3
    _FLAGS = 0x00200000 | 0x08000000  # FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN
    _ATTR_DIRECTORY, _ATTR_REPARSE = 0x10, 0x400
    _INVALID = wintypes.HANDLE(-1).value

    class _Info(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                    ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                    ("links", wintypes.DWORD), ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _k.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _k.CreateFileW.restype = wintypes.HANDLE
    _k.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Info))
    _k.GetFileInformationByHandle.restype = wintypes.BOOL
    _k.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    _k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _k.GetFileType.argtypes = (wintypes.HANDLE,)
    _k.GetFileType.restype = wintypes.DWORD
    _k.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    _k.GetDriveTypeW.restype = wintypes.UINT
    _k.CloseHandle.argtypes = (wintypes.HANDLE,)


def settings(runtime):
    configured = runtime.config.get("file_snapshots", {})
    if not isinstance(configured, dict):
        raise WorkbenchError("file_snapshots configuration must be an object")
    result = dict(DEFAULTS)
    for name, value in configured.items():
        low, high = BOUNDS.get(name, (None, None))
        if low is None or type(value) is not int or not low <= value <= high:
            raise WorkbenchError(f"Invalid file_snapshots.{name}")
        result[name] = value
    return result


def normalize_path(raw):
    """Return the canonical spelling of one absolute local file path, or refuse it."""
    if not isinstance(raw, str) or not raw or len(raw) > 1000 or any(ord(c) < 32 for c in raw):
        raise WorkbenchError("Give exactly one absolute local file path")
    text = raw[1:-1] if len(raw) > 1 and raw[0] == raw[-1] == '"' else raw
    if os.name != "nt":
        if not text.startswith("/") or any(p in (".", "..") for p in text.split("/")) or "//" in text or text.endswith("/"):
            raise WorkbenchError("Use an absolute path to one file")
        return text
    text = text.replace("/", "\\")
    if text.startswith("\\\\"):
        raise WorkbenchError("Network, device and \\\\?\\ paths are not accepted; use a local drive path")
    if not re.fullmatch(r"[A-Za-z]:\\.+", text):
        raise WorkbenchError("Use an absolute local path such as C:\\Users\\name\\file.zip")
    rest = text[3:]
    if any(c in rest for c in '*?"<>|'):
        raise WorkbenchError("Wildcards are not accepted; name exactly one file")
    if ":" in rest:
        raise WorkbenchError("Alternate data streams are not accepted")
    for part in rest.split("\\"):
        if not part:
            raise WorkbenchError("Name one file, not a folder")
        if part in (".", ".."):
            raise WorkbenchError("Relative path components are not accepted")
        if part != part.rstrip(" ."):
            raise WorkbenchError("Names ending with a space or dot are not accepted")
        if RESERVED.fullmatch(part):
            raise WorkbenchError("Device names are not accepted")
    return text[0].upper() + text[1:]


def _check_ancestors(path):
    """Every parent must be a real directory: no links, junctions or other reparse points."""
    if os.name == "nt":
        pure = PureWindowsPath(path)
        if _k.GetDriveTypeW(pure.anchor) not in (2, 3, 5, 6):  # removable, fixed, optical, RAM disk
            raise WorkbenchError("Only local drives are accepted; network and unknown drives are refused")
        current, parents = pure.anchor, pure.parts[1:-1]
    else:
        current, parents = "/", [p for p in path.split("/")[1:-1]]
    for part in parents:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise WorkbenchError("File not found") from None
        except PermissionError:
            raise WorkbenchError("Access to the file's folder is denied") from None
        except OSError:
            raise WorkbenchError("The file's folder cannot be inspected") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise WorkbenchError("The path passes through a link or junction; open the real location instead")
        if not stat.S_ISDIR(info.st_mode):
            raise WorkbenchError("File not found")


def _final_check(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise WorkbenchError("File not found") from None
    except PermissionError:
        raise WorkbenchError("Access to the file is denied") from None
    except OSError:
        raise WorkbenchError("The file cannot be inspected") from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorkbenchError("The path is a link or reparse point; name the real file")
    if not stat.S_ISREG(info.st_mode):
        raise WorkbenchError("Only a regular file can be snapshotted; folders and devices are refused")


class _WindowsSource:
    def __init__(self, path):
        self.path, self.consistency, handle = path, "writers_excluded_during_read", None
        for share in (_FILE_SHARE_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE):
            handle = _k.CreateFileW(path, _GENERIC_READ, share, None, _OPEN_EXISTING, _FLAGS, None)
            if handle not in (None, _INVALID):
                break
            error = ctypes.get_last_error()
            if error != 32:  # Only a sharing violation (an active writer) falls back to change detection.
                raise WorkbenchError({2: "File not found", 3: "File not found", 5: "Access to the file is denied",
                                      1920: "Access to the file is denied"}.get(error, f"The file could not be opened (Windows error {error})"))
            self.consistency = "metadata_unchanged_during_read"
        else:
            raise WorkbenchError("The file is locked by another program")
        self.handle = handle
        try:
            if _k.GetFileType(handle) != 1:
                raise WorkbenchError("Only a regular disk file can be snapshotted")
            info = self.info()
            if info[0] & (_ATTR_DIRECTORY | _ATTR_REPARSE):
                raise WorkbenchError("Only a regular file can be snapshotted; folders and links are refused")
            buffer = ctypes.create_unicode_buffer(32768)
            if not _k.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
                raise WorkbenchError("The opened file's location cannot be confirmed")
            final = buffer.value
            if final.startswith("\\\\?\\UNC\\"):
                raise WorkbenchError("Network files are not accepted")
            final = final[4:] if final.startswith("\\\\?\\") else final
            if os.path.normcase(final) != os.path.normcase(path):
                # Short names, substituted drives or a path swapped during opening all land here.
                raise WorkbenchError("The path resolves to a different location; give the file's full real path")
            self.fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _k.CloseHandle(handle)
            raise

    def info(self):
        value = _Info()
        if not _k.GetFileInformationByHandle(self.handle, ctypes.byref(value)):
            raise WorkbenchError("The opened file cannot be inspected")
        written = (value.written.dwHighDateTime << 32) | value.written.dwLowDateTime
        return (value.attributes, value.volume, (value.index_high << 32) | value.index_low,
                (value.size_high << 32) | value.size_low, written)

    def size(self, info):
        return info[3]

    def close(self):
        os.close(self.fd)  # Closes the underlying handle as well.


class _PosixSource:
    def __init__(self, path):
        self.path, self.consistency = path, "metadata_unchanged_during_read"
        try:
            self.fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            raise WorkbenchError("File not found") from None
        except PermissionError:
            raise WorkbenchError("Access to the file is denied") from None
        except OSError:
            raise WorkbenchError("The file could not be opened") from None
        if not stat.S_ISREG(os.fstat(self.fd).st_mode) or os.path.realpath(path) != path:
            os.close(self.fd)
            raise WorkbenchError("Only a regular file at its real path can be snapshotted")

    def info(self):
        s = os.fstat(self.fd)
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    def size(self, info):
        return info[2]

    def close(self):
        os.close(self.fd)


def capture(path, max_bytes, target):
    """Copy one file through a single verified handle; return size, SHA-256 and consistency."""
    _check_ancestors(path)
    _final_check(path)
    source = _WindowsSource(path) if os.name == "nt" else _PosixSource(path)
    try:
        for _ in range(READ_ATTEMPTS):
            before = source.info()
            expected = source.size(before)
            if expected > max_bytes:
                raise WorkbenchError(f"The file is {expected} bytes; the snapshot limit is {max_bytes} bytes")
            os.lseek(source.fd, 0, os.SEEK_SET)
            h, total = hashlib.sha256(), 0
            with open(target, "wb") as output:
                # Read at most one byte past the size seen at the start, so a growing file cannot be chased.
                while total <= expected:
                    block = os.read(source.fd, min(READ_BLOCK, expected + 1 - total))
                    if not block:
                        break
                    total += len(block)
                    h.update(block)
                    output.write(block)
                    if _on_block:
                        _on_block(path)
                output.flush()
                os.fsync(output.fileno())
            if total == expected and source.info() == before:
                return {"size_bytes": total, "sha256": h.hexdigest(), "consistency": source.consistency}
        raise WorkbenchError("The file kept changing while it was read; try again when it is no longer being written")
    finally:
        source.close()


def _iso(value):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


class FileSnapshotStore:
    def __init__(self, runtime):
        self.runtime = runtime
        self.directory = runtime.data / "file_snapshots"
        self._verified = set()
        with runtime.connection(write=True) as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS file_snapshot_requests (
                id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, path TEXT NOT NULL,
                path_key TEXT NOT NULL, approval_hash TEXT, created REAL NOT NULL, approval_expires REAL NOT NULL,
                state TEXT NOT NULL, decided_at REAL, approved_via TEXT, snapshot_id TEXT, error TEXT);
              CREATE TABLE IF NOT EXISTS file_snapshots (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL, file_name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, taken_at REAL NOT NULL, expires_at REAL NOT NULL,
                consistency TEXT NOT NULL, capture_ms INTEGER NOT NULL, state TEXT NOT NULL);
            """)

    # ---- model-facing --------------------------------------------------------------------------
    def request(self, path, idempotency_key):
        if not isinstance(idempotency_key, str) or not KEY.fullmatch(idempotency_key):
            raise WorkbenchError("A stable idempotency_key (1-128 letters, digits, . _ : -) is required")
        normalized = normalize_path(path)
        path_key, now, config = os.path.normcase(normalized), time.time(), settings(self.runtime)
        with self.runtime.connection(write=True) as c:
            self._expire(c, now)
            row = c.execute("SELECT * FROM file_snapshot_requests WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row and row["path_key"] != path_key:
                raise WorkbenchError("This idempotency_key was used for a different file; use a new key for a new file")
            if row:  # A retry renders the card again; the card fetches a fresh token itself.
                return self._describe(c, row)
            ident, expires = "fsr_" + secrets.token_hex(16), now + config["approval_ttl_seconds"]
            c.execute("""INSERT INTO file_snapshot_requests(id,idempotency_key,path,path_key,approval_hash,created,
                         approval_expires,state) VALUES(?,?,?,?,NULL,?,?,'pending')""",
                      (ident, idempotency_key, normalized, path_key, now, expires))
        return self._pending(ident, normalized, expires, config)

    def prepare(self, request_id):
        """Widget only: issue a fresh single-use approval token for a pending request (older cards stop working)."""
        token, now = secrets.token_urlsafe(32), time.time()
        with self.runtime.connection(write=True) as c:
            self._expire(c, now)
            row = c.execute("SELECT * FROM file_snapshot_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise WorkbenchError("Unknown file snapshot request")
            if row["state"] != "pending":
                return self._describe(c, row)
            c.execute("UPDATE file_snapshot_requests SET approval_hash=? WHERE id=?", (digest(token.encode()), request_id))
        return {**self._pending(row["id"], row["path"], row["approval_expires"], settings(self.runtime)), "approval_token": token}

    def get(self, request_id=None, snapshot_id=None):
        if (request_id is None) == (snapshot_id is None):
            raise WorkbenchError("Give exactly one of request_id or snapshot_id")
        with self.runtime.connection(write=True) as c:
            self._expire(c, time.time())
            if snapshot_id is not None:
                found = c.execute("SELECT request_id FROM file_snapshots WHERE id=?", (snapshot_id,)).fetchone()
                request_id = found["request_id"] if found else None
            row = c.execute("SELECT * FROM file_snapshot_requests WHERE id=?", (request_id,)).fetchone() if request_id else None
            if not row:
                raise WorkbenchError("Unknown file snapshot or request")
            return self._describe(c, row)

    def read(self, snapshot_id, expected_sha256, offset=0, limit=MAX_CHUNK):
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise WorkbenchError("An exact lowercase SHA-256 is required")
        with self.runtime.connection(write=True) as c:
            self._expire(c, time.time())
            row = c.execute("SELECT * FROM file_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise WorkbenchError("Unknown file snapshot")
        if row["state"] != "active":
            raise WorkbenchError("This snapshot has expired and its bytes were removed; ask the user to authorize a new snapshot")
        if row["sha256"] != expected_sha256:
            raise WorkbenchError("expected_sha256 differs from this snapshot")
        if type(offset) is not int or not 0 <= offset <= row["size_bytes"]:
            raise WorkbenchError(f"Offset is outside the snapshot (0..{row['size_bytes']})")
        if type(limit) is not int or not 1 <= limit <= MAX_CHUNK:
            raise WorkbenchError("Snapshot byte window must be 1..262144 bytes")
        stored = self.directory / (row["id"] + ".bin")
        try:
            if stored.stat().st_size != row["size_bytes"]:
                raise WorkbenchError("Stored snapshot is inconsistent; it will not be served")
            if row["id"] not in self._verified:
                if _hash(stored) != row["sha256"]:
                    raise WorkbenchError("Stored snapshot is inconsistent; it will not be served")
                self._verified.add(row["id"])
            with stored.open("rb") as f:
                f.seek(offset)
                data = f.read(limit)
        except FileNotFoundError:
            raise WorkbenchError("Stored snapshot bytes are missing; ask the user to authorize a new snapshot") from None
        return {"snapshot_id": row["id"], "file_name": row["file_name"], "sha256": row["sha256"],
                "size_bytes": row["size_bytes"], "offset": offset, "next_offset": offset + len(data),
                "eof": offset + len(data) == row["size_bytes"], "chunk_sha256": digest(data),
                "base64": base64.b64encode(data).decode("ascii")}

    # ---- Human decision (app-only tool or local operator) ----------------------------------------
    def approve(self, request_id, approval_token=None, decision="approve", via="widget"):
        if decision not in ("approve", "deny"):
            raise WorkbenchError("Decision must be approve or deny")
        now, config = time.time(), settings(self.runtime)
        with self.runtime.connection(write=True) as c:
            self._expire(c, now)
            row = c.execute("SELECT * FROM file_snapshot_requests WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise WorkbenchError("Unknown file snapshot request")
            if via != "local_operator":
                if not isinstance(approval_token, str) or not row["approval_hash"] or not hmac.compare_digest(
                        digest(approval_token.encode()), row["approval_hash"]):
                    raise WorkbenchError("This approval card is no longer valid; ask for the snapshot again")
            if row["state"] == "snapshotted" and decision == "approve":
                return self._describe(c, row)  # A repeated click returns the same snapshot.
            if row["state"] == "capturing":
                raise WorkbenchError("The snapshot is being taken; check get_file_snapshot")
            if row["state"] != "pending":
                raise WorkbenchError(f"This request is already {row['state']}; a new request is needed")
            if decision == "deny":
                c.execute("UPDATE file_snapshot_requests SET state='denied',approval_hash=NULL,decided_at=?,approved_via=? WHERE id=?",
                          (now, via, request_id))
                return {"request_id": request_id, "status": "denied", "path": row["path"]}
            c.execute("UPDATE file_snapshot_requests SET state='capturing',decided_at=?,approved_via=? WHERE id=?",
                      (now, via, request_id))
        snapshot_id = "fss_" + secrets.token_hex(16)
        self.directory.mkdir(parents=True, exist_ok=True)
        partial, final = self.directory / (snapshot_id + ".partial"), self.directory / (snapshot_id + ".bin")
        started = time.monotonic()
        try:
            captured = capture(row["path"], config["max_bytes"], partial)
            with self.runtime.connection(write=True) as c:
                self._expire(c, time.time())
                used = c.execute("SELECT COALESCE(SUM(size_bytes),0) FROM file_snapshots WHERE state='active'").fetchone()[0]
                if used + captured["size_bytes"] > config["max_store_bytes"]:
                    raise WorkbenchError("The snapshot store is full; wait for older snapshots to expire")
                os.replace(partial, final)
                os.chmod(final, stat.S_IREAD)
                taken = time.time()
                c.execute("INSERT INTO file_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,'active')",
                          (snapshot_id, request_id, row["path"], PureWindowsPath(row["path"]).name if os.name == "nt" else Path(row["path"]).name,
                           captured["size_bytes"], captured["sha256"], taken, taken + config["snapshot_ttl_seconds"],
                           captured["consistency"], int((time.monotonic() - started) * 1000)))
                c.execute("UPDATE file_snapshot_requests SET state='snapshotted',snapshot_id=? WHERE id=?", (snapshot_id, request_id))
                return self._describe(c, c.execute("SELECT * FROM file_snapshot_requests WHERE id=?", (request_id,)).fetchone())
        except BaseException as e:
            partial.unlink(missing_ok=True)
            if final.exists():  # Published bytes without a committed row are never served.
                os.chmod(final, stat.S_IREAD | stat.S_IWRITE)
                final.unlink()
            message = str(e)[:500] if isinstance(e, WorkbenchError) else "The snapshot could not be taken"
            with self.runtime.connection(write=True) as c:
                c.execute("UPDATE file_snapshot_requests SET state='failed',approval_hash=NULL,error=? WHERE id=? AND state='capturing'",
                          (message, request_id))
            if isinstance(e, WorkbenchError):
                raise
            raise WorkbenchError(message) from e

    # ---- internals -------------------------------------------------------------------------------
    def _expire(self, c, now):
        c.execute("UPDATE file_snapshot_requests SET state='expired',approval_hash=NULL WHERE state='pending' AND approval_expires<=?", (now,))
        c.execute("UPDATE file_snapshot_requests SET state='failed',approval_hash=NULL,error='Capture was interrupted' "
                  "WHERE state='capturing' AND decided_at<=?", (now - CAPTURE_STALE_SECONDS,))
        for row in c.execute("SELECT id FROM file_snapshots WHERE state='active' AND expires_at<=?", (now,)).fetchall():
            stored = self.directory / (row["id"] + ".bin")
            try:
                if stored.exists():
                    os.chmod(stored, stat.S_IREAD | stat.S_IWRITE)
                    stored.unlink()
            except OSError:
                continue  # Retried on the next call; the row stays active only until its bytes are gone.
            c.execute("UPDATE file_snapshots SET state='expired' WHERE id=?", (row["id"],))
            self._verified.discard(row["id"])

    def _pending(self, ident, path, expires, config):
        return {"request_id": ident, "status": "awaiting_user_approval", "path": path,
                "approval_expires_at": _iso(expires), "max_bytes": config["max_bytes"],
                "scope": "One read-only snapshot of this one file. No folder listing, other files, changes or execution.",
                "next_step": "The user approves or denies the exact path in the Workbench card. Then call get_file_snapshot "
                             "with this request_id; do not claim access before it reports status ready.",
                "if_card_does_not_open": "The user can decide on the Workbench computer instead: "
                                         f"python scripts/approve_file_snapshot.py --config <Workbench config> --request {ident} --approve "
                                         "(or --deny). Do not ask for the approval token; it is never shown to the model."}

    def _describe(self, c, row):
        base = {"request_id": row["id"], "path": row["path"]}
        state = row["state"]
        if state == "pending":
            return {**self._pending(row["id"], row["path"], row["approval_expires"], settings(self.runtime))}
        if state == "capturing":
            return {**base, "status": "capturing"}
        if state != "snapshotted":
            guidance = {"denied": "The user denied this file. Do not retry unless the user asks again.",
                        "expired": "The approval window closed. Request again with a new idempotency_key if the user still wants the file.",
                        "failed": "Report the error to the user; a new request with a new idempotency_key is needed."}[state]
            return {**base, "status": state, "error": row["error"], "next_step": guidance}
        snap = c.execute("SELECT * FROM file_snapshots WHERE id=?", (row["snapshot_id"],)).fetchone()
        ready = snap["state"] == "active"
        return {**base, "status": "ready" if ready else "expired", "snapshot_id": snap["id"], "file_name": snap["file_name"],
                "size_bytes": snap["size_bytes"], "sha256": snap["sha256"], "taken_at": _iso(snap["taken_at"]),
                "expires_at": _iso(snap["expires_at"]), "consistency": snap["consistency"], "capture_ms": snap["capture_ms"],
                "approved_via": row["approved_via"],
                "next_step": ("Call read_file_snapshot_bytes with snapshot_id and sha256 from offset 0, following next_offset until eof; "
                              "decode each base64 window in a code tool, check chunk_sha256, then verify the whole-file SHA-256."
                              if ready else "The snapshot expired and its bytes were removed; a new authorization is needed.")}


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(READ_BLOCK), b""):
            h.update(block)
    return h.hexdigest()
