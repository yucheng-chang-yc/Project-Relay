"""MCP Events (webhook delivery) for Workbench task completion.

Implements the ChatGPT MCP Events contract (MCP 2.0, protocol 2026-07-28): events/list,
events/subscribe (with signed callback verification), events/unsubscribe, and Standard Webhooks
signed delivery of `task.finished`. Subscriptions and the delivery outbox live in the runtime
SQLite database, so they survive restarts. The event ID is deterministic per subscription, task and
terminal status: a task is announced once per subscription and retries keep the same ID.
The event data carries only identities; the subscribed chat reads results with get_task_result.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit

from .core import WorkbenchError, canonical

EVENT_NAME = "task.finished"
ENDED = ("completed", "failed", "cancelled", "timed_out", "interrupted")
PRINCIPAL = "workbench-local-operator"  # Single-user runtime; the tunnel authenticates the account.
DEFAULT_TTL = 7 * 24 * 3600
MIN_TTL, MAX_TTL = 3600, 30 * 24 * 3600
VERIFY_CACHE_SECONDS = 24 * 3600
ROTATION_WINDOW = 600
MAX_ATTEMPTS = 12
MAX_BODY = 256 * 1024
EVENT_DEFINITION = {
    "name": EVENT_NAME,
    "description": "A Workbench task reached a terminal state (completed, failed, cancelled, timed_out or interrupted). "
                   "The data identifies the task, its terminal status and its sealed result SHA-256; the full result is "
                   "available through the get_task_result tool.",
    "delivery": ["webhook"],
    "inputSchema": {"type": "object", "properties": {
        "project_id": {"type": "string", "description": "Registered Workbench project ID to monitor. Omit to monitor every registered project."}},
        "additionalProperties": False},
    "payloadSchema": {"type": "object", "properties": {
        "task_id": {"type": "string"},
        "status": {"type": "string", "enum": list(ENDED)},
        "result_sha256": {"type": ["string", "null"]}},
        "required": ["task_id", "status", "result_sha256"], "additionalProperties": False},
}


class EventsError(WorkbenchError):
    """JSON-RPC error with an explicit code and typed data (e.g. -32015 CallbackEndpointError)."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.data = code, data


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise EventsError(-32602, "Invalid params", {"reason": "secret_must_use_whsec_prefix"})
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except ValueError:
        raise EventsError(-32602, "Invalid params", {"reason": "secret_not_base64"}) from None
    if not 24 <= len(key) <= 64:
        raise EventsError(-32602, "Invalid params", {"reason": "secret_length_must_be_24_to_64_bytes"})
    return key


def sign(secrets_in_use, message_id, timestamp, body):
    """Standard Webhooks: v1,base64(HMAC-SHA256(key, "{id}.{timestamp}.{body}")); space-separated for rotation."""
    signed = f"{message_id}.{timestamp}.".encode() + body
    return " ".join("v1," + base64.b64encode(hmac.new(signing_key(s), signed, hashlib.sha256).digest()).decode()
                    for s in secrets_in_use)


def validate_callback_url(url):
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except (ValueError, TypeError):
        raise EventsError(-32602, "Invalid params", {"reason": "callback_url_malformed"}) from None
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
        raise EventsError(-32602, "Invalid params", {"reason": "callback_url_must_be_https_without_credentials"})
    return parsed, host, port or 443


def secure_post(url, body, headers, timeout=10):
    """POST to a public HTTPS address only: resolve once, reject non-public addresses, connect to the
    validated IP while keeping the hostname for SNI/certificate checks, never follow redirects."""
    parsed, host, port = validate_callback_url(url)
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise EventsError(-32015, "CallbackEndpointError", {"reason": "dns_failed"}) from None
    ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
        raise EventsError(-32015, "CallbackEndpointError", {"reason": "non_public_address"})
    connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    try:
        raw = socket.create_connection((str(ips[0]), port), timeout=timeout)
        connection.sock = connection._context.wrap_socket(raw, server_hostname=host)
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(64 * 1024)
    except socket.timeout:
        raise EventsError(-32015, "CallbackEndpointError", {"reason": "timeout"}) from None
    except (OSError, http.client.HTTPException, ssl.SSLError):
        raise EventsError(-32015, "CallbackEndpointError", {"reason": "unreachable"}) from None
    finally:
        connection.close()


class EventStore:
    def __init__(self, runtime):
        self.runtime = runtime
        self._dispatcher = None
        with runtime.connection(write=True) as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS event_subscriptions (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
                url TEXT NOT NULL, secret TEXT NOT NULL, previous_secret TEXT, previous_secret_until REAL,
                created REAL NOT NULL, updated REAL NOT NULL, refresh_before REAL, active INTEGER NOT NULL DEFAULT 1);
              CREATE TABLE IF NOT EXISTS event_callback_verifications (
                principal TEXT NOT NULL, url TEXT NOT NULL, verified_at REAL NOT NULL, PRIMARY KEY(principal, url));
              CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL, task_id TEXT NOT NULL, status TEXT NOT NULL,
                body BLOB NOT NULL, occurred_at REAL NOT NULL, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, lease_until REAL, lease_owner TEXT, delivered_at REAL, final_state TEXT,
                last_http_status INTEGER, last_error TEXT);
              CREATE INDEX IF NOT EXISTS event_outbox_due ON event_outbox(delivered_at, final_state, next_attempt);
            """)
            # sqlite3.executescript may commit the surrounding transaction; serialize migration again.
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in c.execute("PRAGMA table_info(event_outbox)")}
            if "lease_owner" not in columns:
                c.execute("ALTER TABLE event_outbox ADD COLUMN lease_owner TEXT")

    # ---- evidence log (no secrets, no callback URLs) -------------------------------------------
    def log(self, kind, **fields):
        line = canonical({"at": iso(time.time()), "kind": kind, **fields})
        try:
            with (self.runtime.data / "events.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print("MCP-EVENTS " + line, file=sys.stderr, flush=True)

    # ---- protocol methods ------------------------------------------------------------------------
    def list_events(self, params):
        return {"events": [EVENT_DEFINITION], "nextCursor": None}

    def _arguments(self, arguments):
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict) or set(arguments) - {"project_id"}:
            raise EventsError(-32602, "Invalid params", {"reason": "arguments_do_not_match_inputSchema"})
        if "project_id" in arguments:
            if not isinstance(arguments["project_id"], str) or arguments["project_id"] not in self.runtime.projects:
                raise EventsError(-32602, "Invalid params", {"reason": "unknown_project_id"})
        return arguments

    @staticmethod
    def subscription_id(principal, url, name, arguments):
        return "sub_" + hashlib.sha256(canonical({"principal": principal, "url": url, "name": name,
                                                   "arguments": arguments}).encode()).hexdigest()[:32]

    def _verify_callback(self, url, secret, subscription_id):
        with self.runtime.connection() as c:
            cached = c.execute("SELECT verified_at FROM event_callback_verifications WHERE principal=? AND url=?",
                               (PRINCIPAL, url)).fetchone()
        if cached and time.time() - cached["verified_at"] < VERIFY_CACHE_SECONDS:
            self.log("callback_verification_cached", subscription_id=subscription_id)
            return
        challenge = secrets.token_urlsafe(32)
        body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
        message_id, timestamp = "msg_verification_" + secrets.token_hex(12), str(int(time.time()))
        status, reply = secure_post(url, body, {
            "Content-Type": "application/json", "webhook-id": message_id, "webhook-timestamp": timestamp,
            "webhook-signature": sign([secret], message_id, timestamp, body), "X-MCP-Subscription-Id": subscription_id})
        try:
            echoed = json.loads(reply.decode("utf-8")).get("challenge")
        except (ValueError, UnicodeError, AttributeError):
            echoed = None
        if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
            self.log("callback_verification_failed", subscription_id=subscription_id, http_status=status)
            raise EventsError(-32015, "CallbackEndpointError",
                              {"reason": "challenge_failed" if 200 <= status < 300 else "http_status", "status": status})
        with self.runtime.connection(write=True) as c:
            c.execute("INSERT OR REPLACE INTO event_callback_verifications(principal,url,verified_at) VALUES(?,?,?)",
                      (PRINCIPAL, url, time.time()))
        self.log("callback_verified", subscription_id=subscription_id, http_status=status, webhook_id=message_id)

    def subscribe(self, params):
        if params.get("name") != EVENT_NAME:
            raise EventsError(-32602, "Invalid params", {"reason": "unknown_event"})
        arguments = self._arguments(params.get("arguments"))
        delivery = params.get("delivery")
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise EventsError(-32602, "Invalid params", {"reason": "only_webhook_delivery_supported"})
        url, secret = delivery.get("url"), delivery.get("secret")
        if not isinstance(url, str):
            raise EventsError(-32602, "Invalid params", {"reason": "callback_url_missing"})
        validate_callback_url(url)
        signing_key(secret)
        ttl_ms = params.get("ttlMs", "default")
        ttl = DEFAULT_TTL if ttl_ms == "default" else MAX_TTL if ttl_ms is None else \
            max(MIN_TTL, min(MAX_TTL, int(ttl_ms) // 1000)) if isinstance(ttl_ms, int) and not isinstance(ttl_ms, bool) else None
        if ttl is None:
            raise EventsError(-32602, "Invalid params", {"reason": "ttlMs_must_be_integer_or_null"})
        ident = self.subscription_id(PRINCIPAL, url, EVENT_NAME, arguments)
        self._verify_callback(url, secret, ident)
        now = time.time()
        with self.runtime.connection(write=True) as c:
            existing = c.execute("SELECT secret,previous_secret,previous_secret_until FROM event_subscriptions WHERE id=?",
                                 (ident,)).fetchone()
            if existing:
                rotated = existing["secret"] != secret
                previous_secret = existing["secret"] if rotated else existing["previous_secret"]
                previous_until = now + ROTATION_WINDOW if rotated else existing["previous_secret_until"]
                if previous_until is None or previous_until <= now:
                    previous_secret, previous_until = None, None
                c.execute("UPDATE event_subscriptions SET secret=?,previous_secret=?,previous_secret_until=?,updated=?,"
                          "refresh_before=?,active=1 WHERE id=?",
                          (secret, previous_secret, previous_until,
                           now, now + ttl, ident))
            else:
                c.execute("INSERT INTO event_subscriptions(id,principal,name,arguments,url,secret,created,updated,refresh_before,active)"
                          " VALUES(?,?,?,?,?,?,?,?,?,1)", (ident, PRINCIPAL, EVENT_NAME, canonical(arguments), url, secret, now, now, now + ttl))
        self.log("subscribed", subscription_id=ident, refreshed=bool(existing), arguments=arguments, refresh_before=iso(now + ttl))
        return {"id": ident, "refreshBefore": iso(now + ttl), "cursor": None, "truncated": False}

    def unsubscribe(self, params):
        if params.get("name") != EVENT_NAME:
            raise EventsError(-32602, "Invalid params", {"reason": "unknown_event"})
        arguments = self._arguments(params.get("arguments"))
        delivery = params.get("delivery") or {}
        url = delivery.get("url")
        if not isinstance(url, str):
            raise EventsError(-32602, "Invalid params", {"reason": "callback_url_missing"})
        ident = self.subscription_id(PRINCIPAL, url, EVENT_NAME, arguments)
        with self.runtime.connection(write=True) as c:
            c.execute("UPDATE event_subscriptions SET active=0,updated=? WHERE id=?", (time.time(), ident))
            c.execute("UPDATE event_outbox SET final_state='unsubscribed' WHERE subscription_id=? AND delivered_at IS NULL"
                      " AND final_state IS NULL", (ident,))
        self.log("unsubscribed", subscription_id=ident)
        return {}

    # ---- event production (called inside the task's terminal-state transaction) -------------------
    def enqueue(self, c, task_id, status, result_sha256):
        if status not in ENDED:
            return
        row = c.execute("SELECT project FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return
        now = time.time()
        for sub in c.execute("SELECT id,arguments,created FROM event_subscriptions WHERE active=1 AND name=? AND "
                             "(refresh_before IS NULL OR refresh_before>?)", (EVENT_NAME, now)).fetchall():
            arguments = json.loads(sub["arguments"])
            if arguments.get("project_id") not in (None, row["project"]):
                continue
            event_id = "evt_" + hashlib.sha256(f"{sub['id']}|{task_id}|{status}".encode()).hexdigest()[:32]
            body = json.dumps({"eventId": event_id, "name": EVENT_NAME, "timestamp": iso(now),
                               "data": {"task_id": task_id, "status": status, "result_sha256": result_sha256},
                               "cursor": None}, separators=(",", ":")).encode()
            c.execute("INSERT OR IGNORE INTO event_outbox(event_id,subscription_id,task_id,status,body,occurred_at,created,next_attempt)"
                      " VALUES(?,?,?,?,?,?,?,?)", (event_id, sub["id"], task_id, status, body, now, now, now))

    # ---- delivery --------------------------------------------------------------------------------
    def deliver_due(self, owner, post=None):
        post = post or secure_post
        claimed = 0
        for _ in range(20):
            now = time.time()
            # Claim immediately before each POST, so waiting batch members hold no expiring lease.
            claim_owner = f"{owner}:{secrets.token_hex(16)}"
            with self.runtime.connection(write=True) as c:
                item = c.execute(
                    "SELECT event_id FROM event_outbox WHERE delivered_at IS NULL AND final_state IS NULL AND next_attempt<=? "
                    "AND (lease_until IS NULL OR lease_until<=?) ORDER BY occurred_at,event_id LIMIT 1", (now, now)).fetchone()
                if item is None:
                    break
                event_id = item["event_id"]
                c.execute("UPDATE event_outbox SET lease_until=?,lease_owner=? WHERE event_id=?",
                          (now + 60, claim_owner, event_id))
            claimed += 1
            self._deliver(event_id, claim_owner, post)
        return claimed

    def _deliver(self, event_id, owner, post):
        with self.runtime.connection() as c:
            item = c.execute("SELECT * FROM event_outbox WHERE event_id=?", (event_id,)).fetchone()
            if (item is None or item["delivered_at"] is not None or item["final_state"] is not None
                    or item["lease_owner"] != owner or (item["lease_until"] or 0) <= time.time()):
                return
            sub = c.execute("SELECT * FROM event_subscriptions WHERE id=?", (item["subscription_id"],)).fetchone()
        now = time.time()
        if not sub or not sub["active"] or (sub["refresh_before"] is not None and sub["refresh_before"] <= now):
            self._finish(event_id, owner, final_state="subscription_inactive")
            self.log("delivery_skipped", event_id=event_id, reason="subscription_inactive")
            return
        keys = [sub["secret"]] + ([sub["previous_secret"]] if sub["previous_secret"] and (sub["previous_secret_until"] or 0) > now else [])
        timestamp = str(int(now))
        body = bytes(item["body"])
        headers = {"Content-Type": "application/json", "webhook-id": event_id, "webhook-timestamp": timestamp,
                   "webhook-signature": sign(keys, event_id, timestamp, body), "X-MCP-Subscription-Id": sub["id"]}
        attempt = item["attempts"] + 1
        try:
            status, _ = post(sub["url"], body, headers)
            error = None
        except EventsError as e:
            status, error = None, (e.data or {}).get("reason", "error")
        if status is not None and 200 <= status < 300:
            if self._finish(event_id, owner, delivered=True, attempts=attempt, http_status=status):
                self.log("delivered", event_id=event_id, subscription_id=sub["id"], task_id=item["task_id"],
                         status=item["status"], attempt=attempt, http_status=status)
        elif status in (410, 413):
            if not self._finish(event_id, owner, final_state="gone" if status == 410 else "too_large", attempts=attempt, http_status=status):
                return
            if status == 410:
                with self.runtime.connection(write=True) as c:
                    c.execute("UPDATE event_subscriptions SET active=0,updated=? WHERE id=?", (time.time(), sub["id"]))
            self.log("delivery_final", event_id=event_id, attempt=attempt, http_status=status)
        else:
            final = attempt >= MAX_ATTEMPTS
            with self.runtime.connection(write=True) as c:
                updated = c.execute("UPDATE event_outbox SET attempts=?,next_attempt=?,lease_until=NULL,lease_owner=NULL,"
                                    "last_http_status=?,last_error=?,final_state=? WHERE event_id=? AND lease_owner=? "
                                    "AND delivered_at IS NULL AND final_state IS NULL",
                                    (attempt, time.time() + min(5 * 2 ** (attempt - 1), 3600), status,
                                     error, "gave_up" if final else None, event_id, owner)).rowcount
            if not updated:
                return
            self.log("delivery_retry" if not final else "delivery_gave_up", event_id=event_id, attempt=attempt,
                     http_status=status, error=error)

    def _finish(self, event_id, owner, delivered=False, final_state=None, attempts=None, http_status=None):
        with self.runtime.connection(write=True) as c:
            return c.execute("UPDATE event_outbox SET delivered_at=?,final_state=?,attempts=COALESCE(?,attempts),"
                             "lease_until=NULL,lease_owner=NULL,last_http_status=? WHERE event_id=? AND lease_owner=? "
                             "AND delivered_at IS NULL AND final_state IS NULL",
                             (time.time() if delivered else None, final_state, attempts, http_status, event_id, owner)).rowcount

    def start_dispatcher(self, interval=2.0):
        if self._dispatcher:
            return self._dispatcher
        owner = f"pid-{__import__('os').getpid()}"

        def loop():
            while True:
                try:
                    self.runtime.reconcile_interrupted()
                    self.deliver_due(owner)
                except Exception as e:  # Never crash the facade; the outbox keeps pending work.
                    print(f"MCP-EVENTS dispatcher error: {type(e).__name__}", file=sys.stderr, flush=True)
                time.sleep(interval)
        self._dispatcher = threading.Thread(target=loop, name="mcp-events-dispatcher", daemon=True)
        self._dispatcher.start()
        return self._dispatcher
