"""MCP Events (MCP 2.0 webhook) behaviour with a local signed-receiver fake; no network or model."""
import base64
import hashlib
import hmac
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_runtime as fixtures
from workbench.core import Runtime
from workbench.events import EVENT_NAME, EventsError
from workbench.mcp import MCP

SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()
URL = "https://receiver.example.com/mcp-events/callback_123"
V2 = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
      "io.modelcontextprotocol/clientCapabilities": {}}


class Receiver:
    """Plays the ChatGPT callback: verifies Standard Webhooks signatures and answers challenges."""

    def __init__(self, secret=SECRET, statuses=None, wrong_challenge=False):
        self.secret, self.statuses, self.wrong_challenge = secret, list(statuses or []), wrong_challenge
        self.calls, self.lock = [], threading.Lock()

    def verify(self, headers, body, secret=None):
        key = base64.b64decode((secret or self.secret)[6:])
        expected = "v1," + base64.b64encode(hmac.new(key, f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body,
                                                     hashlib.sha256).digest()).decode()
        return expected in headers["webhook-signature"].split(" ")

    def __call__(self, url, body, headers, timeout=10):
        with self.lock:
            self.calls.append({"url": url, "body": body, "headers": dict(headers)})
            assert self.verify(headers, body), "bad signature"
            payload = json.loads(body)
            if payload.get("type") == "verification":
                return 200, json.dumps({"challenge": "nope" if self.wrong_challenge else payload["challenge"]}).encode()
            return (self.statuses.pop(0) if self.statuses else 200), b""


class EventsTests(unittest.TestCase):
    setUp_base = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    start = fixtures.RuntimeTests.start
    wait = fixtures.RuntimeTests.wait

    def setUp(self):
        self.setUp_base()
        self.mcp = MCP(self.runtime)

    def rpc(self, method, params=None, meta=True):
        params = dict(params or {})
        if meta:
            params["_meta"] = V2
        return self.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    def subscribe(self, receiver, arguments=None, secret=SECRET):
        with patch("workbench.events.secure_post", receiver):
            return self.rpc("events/subscribe", {"name": EVENT_NAME, "arguments": arguments if arguments is not None else {"project_id": "test"},
                                                 "delivery": {"mode": "webhook", "url": URL, "secret": secret}, "cursor": None})

    def deliver(self, receiver, runtime=None):
        with patch("workbench.events.secure_post", receiver):
            return (runtime or self.runtime).events.deliver_due("test-owner")

    def outbox(self):
        with self.runtime.connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM event_outbox ORDER BY created").fetchall()]

    def test_discover_and_version_handling(self):
        r = self.rpc("server/discover")["result"]
        self.assertEqual((r["resultType"], r["supportedVersions"]), ("complete", ["2026-07-28"]))
        self.assertIn("events", r["capabilities"])
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "project-workbench")
        listed = self.rpc("tools/list")["result"]
        self.assertEqual(listed["resultType"], "complete")
        self.assertTrue(any(t["name"] == "get_task_result" for t in listed["tools"]))
        bad = self.mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                               "params": {"_meta": {**V2, "io.modelcontextprotocol/protocolVersion": "2099-01-01"}}})
        self.assertEqual(bad["error"]["code"], -32022)
        self.assertEqual(bad["error"]["data"], {"supported": ["2026-07-28"], "requested": "2099-01-01"})
        legacy = self.mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})["result"]
        self.assertNotIn("resultType", legacy)
        self.assertEqual(legacy["protocolVersion"], "2025-06-18")

    def test_cacheable_results_carry_caching_hints(self):
        # MCP 2026-07-28 caching: MUST include ttlMs (>= 0) and cacheScope on these complete results.
        for method in ("server/discover", "tools/list", "resources/list"):
            r = self.rpc(method)["result"]
            self.assertEqual((r["ttlMs"], r["cacheScope"]), (300000, "public"), method)
        ui = self.rpc("resources/read", {"uri": "ui://project-workbench/file-snapshot-v1.html"})["result"]
        self.assertEqual((ui["ttlMs"], ui["cacheScope"]), (300000, "public"))
        self.assertNotIn("ttlMs", self.rpc("events/list")["result"])
        legacy = self.mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                                  "params": {"uri": "ui://project-workbench/file-snapshot-v1.html"}})["result"]
        self.assertNotIn("ttlMs", legacy)

    def test_event_definition_and_subscription_validation(self):
        events = self.rpc("events/list")["result"]["events"]
        self.assertEqual([e["name"] for e in events], [EVENT_NAME])
        self.assertEqual(events[0]["payloadSchema"]["required"], ["task_id", "status", "result_sha256"])
        receiver = Receiver()
        cases = {"secret_must_use_whsec_prefix": {"secret": "abc"},
                 "secret_length_must_be_24_to_64_bytes": {"secret": "whsec_" + base64.b64encode(b"x" * 8).decode()},
                 "callback_url_must_be_https_without_credentials": {"url": "http://receiver.example.com/x"},
                 "unknown_project_id": {"arguments": {"project_id": "nope"}}}
        for reason, change in cases.items():
            with self.subTest(reason=reason), patch("workbench.events.secure_post", receiver):
                params = {"name": EVENT_NAME, "arguments": change.get("arguments", {}),
                          "delivery": {"mode": "webhook", "url": change.get("url", URL), "secret": change.get("secret", SECRET)}}
                err = self.rpc("events/subscribe", params)["error"]
                self.assertEqual(err["data"]["reason"], reason)
        self.assertEqual(receiver.calls, [])
        failed = self.subscribe(Receiver(wrong_challenge=True))["error"]
        self.assertEqual((failed["code"], failed["data"]["reason"]), (-32015, "challenge_failed"))
        with self.runtime.connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM event_subscriptions").fetchone()[0], 0)

    def test_subscribe_is_idempotent_verified_once_and_rotates_secret(self):
        receiver = Receiver()
        first = self.subscribe(receiver)["result"]
        self.assertTrue(first["id"].startswith("sub_") and first["cursor"] is None and first["truncated"] is False)
        self.assertEqual(first["resultType"], "complete")
        verification = receiver.calls[0]
        self.assertTrue(verification["headers"]["webhook-id"].startswith("msg_verification_"))
        self.assertEqual(verification["headers"]["X-MCP-Subscription-Id"], first["id"])
        again = self.subscribe(receiver)["result"]
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(receiver.calls), 1, "verification cached for the same principal and URL")
        new_secret = "whsec_" + base64.b64encode(b"n" * 32).decode()
        self.subscribe(Receiver(secret=new_secret), secret=new_secret)
        task = self.wait(self.start(key="rotation")["id"])
        both = Receiver(secret=new_secret)
        self.deliver(both)
        signatures = both.calls[0]["headers"]["webhook-signature"].split(" ")
        self.assertEqual(len(signatures), 2, "rotation window signs with old and new secrets")
        self.assertTrue(both.verify(both.calls[0]["headers"], both.calls[0]["body"], SECRET))
        with self.runtime.connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM event_subscriptions").fetchone()[0], 1)
        self.assertEqual(task["status"], "completed")

    def test_terminal_task_delivers_one_signed_identity_only_event(self):
        receiver = Receiver()
        sub = self.subscribe(receiver)["result"]["id"]
        task = self.wait(self.start(key="deliver")["id"])
        rows = self.outbox()
        self.assertEqual(len(rows), 1)
        self.runtime.finish(task["id"], task["status"], task["exit_code"], self.runtime.get_task_result(task["id"])["result"])
        self.assertEqual(len(self.outbox()), 1, "idempotent finish does not enqueue twice")
        self.assertEqual(self.deliver(receiver), 1)
        call = receiver.calls[-1]
        body = json.loads(call["body"])
        self.assertEqual(call["headers"]["webhook-id"], body["eventId"])
        self.assertEqual(call["headers"]["X-MCP-Subscription-Id"], sub)
        self.assertEqual(body["name"], EVENT_NAME)
        self.assertEqual(body["data"], {"task_id": task["id"], "status": "completed", "result_sha256": task["result_sha256"]})
        self.assertIsNone(body["cursor"])
        self.assertTrue(body["timestamp"].endswith("Z"))
        self.assertEqual(self.deliver(receiver), 0, "delivered events are not resent")
        self.assertEqual(len([c for c in receiver.calls if json.loads(c["body"]).get("eventId")]), 1)

    def test_retry_keeps_event_id_and_410_stops(self):
        receiver = Receiver(statuses=[500])
        self.subscribe(receiver)
        self.wait(self.start(key="retry")["id"])
        self.deliver(receiver)
        with self.runtime.connection(write=True) as c:
            c.execute("UPDATE event_outbox SET next_attempt=0")
        time.sleep(1.1)
        self.deliver(receiver)
        events = [c for c in receiver.calls if "eventId" in json.loads(c["body"])]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["headers"]["webhook-id"], events[1]["headers"]["webhook-id"])
        self.assertNotEqual(events[0]["headers"]["webhook-timestamp"], events[1]["headers"]["webhook-timestamp"])
        self.assertIsNotNone(self.outbox()[0]["delivered_at"])
        gone = Receiver(statuses=[410])
        self.wait(self.start(key="gone")["id"])
        self.deliver(gone)
        self.assertEqual(self.outbox()[-1]["final_state"], "gone")
        with self.runtime.connection() as c:
            self.assertEqual(c.execute("SELECT active FROM event_subscriptions").fetchone()[0], 0)
        self.wait(self.start(key="after-gone")["id"])
        self.assertEqual(len(self.outbox()), 2, "inactive subscription receives no new events")

    def test_restart_persistence_lease_and_unsubscribe(self):
        receiver = Receiver()
        self.subscribe(receiver)
        self.wait(self.start(key="persist")["id"])
        restarted = Runtime(self.config)  # New process view of the same SQLite store.
        results = []
        slow = Receiver()
        original = slow.__call__

        def delayed(url, body, headers, timeout=10):
            time.sleep(0.3)
            return original(url, body, headers, timeout)
        threads = [threading.Thread(target=lambda r=r: results.append(self.deliver(delayed, r))) for r in (self.runtime, restarted)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [0, 1], "the lease lets only one facade send the event")
        self.assertEqual(len(slow.calls), 1)
        with patch("workbench.events.secure_post", receiver):
            self.assertEqual(self.rpc("events/unsubscribe", {"name": EVENT_NAME, "arguments": {"project_id": "test"},
                                                              "delivery": {"mode": "webhook", "url": URL}})["result"]["resultType"], "complete")
        last = self.wait(self.start(key="after-unsubscribe")["id"])
        self.assertEqual(len(self.outbox()), 1)
        end = time.monotonic() + 10  # Windows keeps worker.log locked until the worker process exits.
        while fixtures.process_alive(self.runtime.get_row(last["id"])["worker_pid"]) and time.monotonic() < end:
            time.sleep(.05)

    def test_discovery_diagnostics_are_redacted_and_capabilities_truthful(self):
        self.rpc("server/discover")
        self.rpc("events/list")
        self.subscribe(Receiver())
        self.mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                         "params": {"name": "list_projects", "arguments": {}}})
        raw = (self.runtime.data / "mcp-discovery.log").read_text(encoding="utf-8")
        lines = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([l["method"] for l in lines], ["server/discover", "events/list", "events/subscribe", "tools/call"])
        self.assertIn("events", lines[0]["response"]["result"]["capabilities"])
        self.assertEqual(lines[1]["response"]["result"]["events"][0]["name"], EVENT_NAME)
        delivery = lines[2]["request"]["delivery"]
        self.assertEqual(delivery["secret"], "[redacted]")
        self.assertEqual(delivery["url"], "https://receiver.example.com/[path-and-query-redacted]")
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("callback_123", raw)
        self.assertEqual((lines[3]["tool"], "request" in lines[3]), ("list_projects", False))
        self.assertEqual(lines[0]["protocolVersion"], "2026-07-28")
        caps = self.runtime.get_project_capabilities("test")
        self.assertTrue(caps["features"]["mcp_events"])
        # One real ChatGPT run (2026-10-03) replied in a separate notification conversation only.
        self.assertEqual(caps["mcp_events_detail"]["chatgpt_notification"], "verified_once_in_separate_notification_conversation")
        self.assertEqual(caps["mcp_events_detail"]["original_conversation_continuation"], "unverified")
        self.assertTrue(caps["features"]["single_file_snapshot"])
        self.assertFalse(caps["single_file_snapshot_detail"]["project_required"])
        self.assertEqual(caps["single_file_snapshot_detail"]["chatgpt_acceptance"], "verified_2026-10-03")
        self.assertEqual(caps["single_file_snapshot_detail"]["chatgpt_verified_max_bytes"], 97520)

    def test_interrupted_task_is_announced(self):
        receiver = Receiver()
        self.subscribe(receiver)
        task = self.start("timeout", timeout=20, key="interrupt")
        end = time.monotonic() + 5
        while not self.runtime.get_row(task["id"])["child_pid"] and time.monotonic() < end:
            time.sleep(.05)
        row = self.runtime.get_row(task["id"])
        import subprocess
        subprocess.run(["taskkill", "/PID", str(row["worker_pid"]), "/T", "/F"] if fixtures.os.name == "nt" else
                       ["kill", "-9", str(row["worker_pid"])], capture_output=True)
        if fixtures.os.name != "nt":
            subprocess.run(["kill", "-9", str(row["child_pid"])], capture_output=True)
        end = time.monotonic() + 5
        while fixtures.process_alive(row["worker_pid"]) and time.monotonic() < end:
            time.sleep(.05)
        with self.runtime.connection(write=True) as c:
            c.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time() - 60, task["id"]))
        self.assertEqual(self.runtime.get_task(task["id"])["status"], "interrupted")
        self.assertEqual([(r["task_id"], r["status"]) for r in self.outbox()], [(task["id"], "interrupted")])
        self.deliver(receiver)
        body = json.loads(receiver.calls[-1]["body"])
        self.assertEqual(body["data"], {"task_id": task["id"], "status": "interrupted", "result_sha256": None})


if __name__ == "__main__":
    unittest.main()
