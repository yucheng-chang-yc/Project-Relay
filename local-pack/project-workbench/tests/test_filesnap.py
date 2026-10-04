"""Single-file snapshots without a project: server-verified one-time approval, fixed bytes, bounded reads.

Local fixtures only; no ChatGPT host, widget rendering or model is exercised here.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from workbench import filesnap
from workbench.core import Runtime
from workbench.mcp import MCP, TOOLS



class FileSnapshotTests(unittest.TestCase):
    def setUp(self, **snapshot_config):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        self.file = self.outside / "bundle.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as z:
            z.writestr("result.csv", "x,y\n1,2\n")
            z.writestr("noise.bin", os.urandom(300 * 1024))  # forces several 256 KiB windows
        self.file.write_bytes(buffer.getvalue())
        self.original = buffer.getvalue()
        config = {"version": 1, "data_dir": str(self.base / "data"), "projects": []}
        if snapshot_config:
            config["file_snapshots"] = snapshot_config
        (self.base / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.runtime = Runtime(self.base / "config.json")
        self.mcp = MCP(self.runtime)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, arguments):
        return self.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})["result"]

    def request(self, path=None, key="k1"):
        return self.call("request_file_snapshot", {"path": str(path or self.file), "idempotency_key": key})

    def prepare(self, requested):
        """What the card does on load (app-only tool): fetch a fresh single-use token."""
        return self.call("prepare_file_snapshot_approval", {"request_id": requested["structuredContent"]["data"]["request_id"]})

    def token(self, requested):
        return self.prepare(requested)["structuredContent"]["data"].get("approval_token")

    def approve(self, requested, decision="approve", token=None):
        return self.call("approve_file_snapshot", {"request_id": requested["structuredContent"]["data"]["request_id"],
                                                   "approval_token": token or self.token(requested) or "none", "decision": decision})

    def read_all(self, ready, window=100000):
        parts, offset = [], 0
        while True:
            chunk = self.call("read_file_snapshot_bytes", {"snapshot_id": ready["snapshot_id"], "expected_sha256": ready["sha256"],
                                                          "offset": offset, "limit": window})
            self.assertFalse(chunk.get("isError"), chunk)
            data = chunk["structuredContent"]["data"]
            raw = base64.b64decode(data["base64"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), data["chunk_sha256"])
            parts.append(raw)
            offset = data["next_offset"]
            if data["eof"]:
                return b"".join(parts)

    def error(self, result):
        self.assertTrue(result.get("isError"), result)
        return result["structuredContent"]["data"]["error"]

    def test_full_flow_needs_no_project_and_returns_exact_bytes(self):
        self.assertEqual(self.runtime.list_projects(), [])
        requested = self.request()
        data = requested["structuredContent"]["data"]
        self.assertEqual(data["status"], "awaiting_user_approval")
        self.assertEqual(data["path"], str(self.file))
        self.assertNotIn("_meta", requested)  # nothing secret rides on the model-visible result
        self.assertNotIn("approval_token", json.dumps(requested))
        self.assertFalse((self.runtime.data / "file_snapshots").exists() and any((self.runtime.data / "file_snapshots").iterdir()),
                         "nothing is read before approval")
        ready = self.approve(requested)["structuredContent"]["data"]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(ready["size_bytes"], len(self.original))
        self.assertEqual(ready["consistency"], "writers_excluded_during_read" if os.name == "nt" else "metadata_unchanged_during_read")
        received = self.read_all(ready)
        self.assertEqual(received, self.original)
        with zipfile.ZipFile(io.BytesIO(received)) as z:
            self.assertEqual(z.read("result.csv"), b"x,y\n1,2\n")

    def test_model_cannot_approve_and_tool_is_app_only(self):
        for name in ("approve_file_snapshot", "prepare_file_snapshot_approval"):
            tool = next(t for t in TOOLS if t["name"] == name)
            self.assertEqual(tool["_meta"]["openai/visibility"], "private")
            self.assertEqual(tool["_meta"]["ui"]["visibility"], ["app"])
        request_tool = next(t for t in TOOLS if t["name"] == "request_file_snapshot")
        self.assertIn("openai/outputTemplate", request_tool["_meta"])
        requested = self.request()
        request_id = requested["structuredContent"]["data"]["request_id"]
        for token in ("", "guess", "authorized:true"):
            self.assertIn("no longer valid", self.error(self.call("approve_file_snapshot",
                          {"request_id": request_id, "approval_token": token, "decision": "approve"})))
        self.assertEqual(self.call("get_file_snapshot", {"request_id": request_id})["structuredContent"]["data"]["status"],
                         "awaiting_user_approval")

    def test_snapshot_bytes_stay_fixed_after_source_changes(self):
        ready = self.approve(self.request())["structuredContent"]["data"]
        self.file.write_bytes(b"changed afterwards")
        self.assertEqual(self.read_all(ready, window=262144), self.original)
        again = self.call("get_file_snapshot", {"snapshot_id": ready["snapshot_id"]})["structuredContent"]["data"]
        self.assertEqual((again["status"], again["sha256"]), ("ready", ready["sha256"]))

    def test_retry_returns_same_snapshot_and_new_key_or_path_needs_new_approval(self):
        ready = self.approve(self.request())["structuredContent"]["data"]
        retried = self.request()
        self.assertNotIn("_meta", retried)
        self.assertEqual(retried["structuredContent"]["data"]["snapshot_id"], ready["snapshot_id"])
        self.assertNotIn("approval_token", self.prepare(retried)["structuredContent"]["data"])
        other = self.outside / "other.txt"
        other.write_text("other", encoding="utf-8")
        self.assertIn("different file", self.error(self.request(other)))
        fresh = self.request(other, key="k2")
        self.assertEqual(fresh["structuredContent"]["data"]["status"], "awaiting_user_approval")
        resample = self.request(key="k3")
        self.assertEqual(resample["structuredContent"]["data"]["status"], "awaiting_user_approval")
        self.assertTrue(self.token(resample))

    def test_pending_retry_keeps_request_and_new_card_rotates_token(self):
        first = self.request()
        old_token = self.token(first)
        second = self.request()
        self.assertEqual(first["structuredContent"]["data"]["request_id"], second["structuredContent"]["data"]["request_id"])
        self.assertEqual(second["structuredContent"]["data"]["status"], "awaiting_user_approval")
        new_token = self.token(second)  # the re-rendered card fetches its own token
        self.assertIn("no longer valid", self.error(self.approve(first, token=old_token)))
        self.assertEqual(self.approve(second, token=new_token)["structuredContent"]["data"]["status"], "ready")

    def test_repeated_click_returns_same_snapshot(self):
        requested = self.request()
        token = self.token(requested)
        one = self.approve(requested, token=token)["structuredContent"]["data"]
        two = self.approve(requested, token=token)["structuredContent"]["data"]
        self.assertEqual(one["snapshot_id"], two["snapshot_id"])
        self.assertEqual(len(list((self.runtime.data / "file_snapshots").glob("*.bin"))), 1)

    def test_deny_and_approval_expiry(self):
        denied = self.approve(self.request(), decision="deny")["structuredContent"]["data"]
        self.assertEqual(denied["status"], "denied")
        self.assertEqual(self.request()["structuredContent"]["data"]["status"], "denied")
        late = self.request(key="late")
        token = self.token(late)
        now = time.time()
        with patch.object(filesnap.time, "time", return_value=now + 16 * 60):
            self.assertNotIn("approval_token", self.prepare(late)["structuredContent"]["data"])
            self.assertIn("no longer valid", self.error(self.approve(late, token=token)))
            status = self.call("get_file_snapshot", {"request_id": late["structuredContent"]["data"]["request_id"]})
            self.assertEqual(status["structuredContent"]["data"]["status"], "expired")

    def test_expired_snapshot_is_removed_and_unreadable(self):
        ready = self.approve(self.request())["structuredContent"]["data"]
        stored = self.runtime.data / "file_snapshots" / (ready["snapshot_id"] + ".bin")
        self.assertTrue(stored.exists())
        with patch.object(filesnap.time, "time", return_value=time.time() + 24 * 3600 + 1):
            self.assertIn("expired", self.error(self.call("read_file_snapshot_bytes",
                          {"snapshot_id": ready["snapshot_id"], "expected_sha256": ready["sha256"]})))
            status = self.call("get_file_snapshot", {"snapshot_id": ready["snapshot_id"]})["structuredContent"]["data"]
            self.assertEqual(status["status"], "expired")
            # The same request cannot be reused for a new capture.
            self.assertEqual(self.request()["structuredContent"]["data"]["status"], "expired")
        self.assertFalse(stored.exists())

    def test_window_and_identity_errors(self):
        ready = self.approve(self.request())["structuredContent"]["data"]
        base = {"snapshot_id": ready["snapshot_id"], "expected_sha256": ready["sha256"]}
        self.assertIn("outside the snapshot", self.error(self.call("read_file_snapshot_bytes", {**base, "offset": ready["size_bytes"] + 1})))
        self.assertTrue(self.call("read_file_snapshot_bytes", {**base, "limit": 262145}).get("isError"))
        self.assertIn("differs", self.error(self.call("read_file_snapshot_bytes", {**base, "expected_sha256": "0" * 64})))
        self.assertIn("Unknown", self.error(self.call("read_file_snapshot_bytes", {**base, "snapshot_id": "fss_" + "0" * 32})))
        end = self.call("read_file_snapshot_bytes", {**base, "offset": ready["size_bytes"]})["structuredContent"]["data"]
        self.assertEqual((end["eof"], end["base64"]), (True, ""))

    def test_capture_failures_are_reported_and_final(self):
        missing = self.request(self.outside / "missing.zip", key="m")
        self.assertIn("not found", self.error(self.approve(missing)))
        status = self.call("get_file_snapshot", {"request_id": missing["structuredContent"]["data"]["request_id"]})["structuredContent"]["data"]
        self.assertEqual(status["status"], "failed")
        folder = self.request(self.outside, key="d")
        self.assertIn("regular file", self.error(self.approve(folder)))

    def test_size_and_store_limits(self):
        self.tearDown()
        self.setUp(max_bytes=1024)
        self.assertIn("snapshot limit", self.error(self.approve(self.request())))
        self.tearDown()
        self.setUp(max_store_bytes=len(self.original) + 10)  # setUp regenerates the file with the same size
        self.approve(self.request())
        self.assertIn("store is full", self.error(self.approve(self.request(key="second"))))

    def test_local_operator_can_decide_without_token(self):
        request_id = self.request()["structuredContent"]["data"]["request_id"]
        ready = self.runtime.files.approve(request_id, via="local_operator")
        self.assertEqual((ready["status"], ready["approved_via"]), ("ready", "local_operator"))

    def test_diagnostics_never_record_the_token(self):
        requested = self.request()
        token = self.token(requested)
        self.approve(requested, token=token)
        self.mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
                         "params": {"uri": "ui://project-workbench/file-snapshot-v1.html", "_meta": {"openai/widgetSessionId": "s"}}})
        log = (self.runtime.data / "mcp-discovery.log").read_text(encoding="utf-8")
        self.assertNotIn(token, log)
        records = [json.loads(line) for line in log.splitlines()]
        call = next(r for r in records if r.get("tool") == "request_file_snapshot")
        self.assertEqual((call["isError"], call["result_meta_keys"]), (False, []))
        read = next(r for r in records if r["method"] == "resources/read")
        html = (Path(__file__).resolve().parents[1] / "ui" / "file-snapshot.html").read_bytes()
        self.assertEqual(read["uri"], "ui://project-workbench/file-snapshot-v1.html")
        self.assertEqual(read["request_meta_keys"], ["openai/widgetSessionId"])
        self.assertEqual((read["contents"][0]["mimeType"], read["contents"][0]["text_sha256"]),
                         ("text/html;profile=mcp-app", hashlib.sha256(html).hexdigest()))
        self.assertNotIn("<script>", log)
        pending = requested["structuredContent"]["data"]
        self.assertIn(pending["request_id"], pending["if_card_does_not_open"])

    @unittest.skipUnless(os.name == "nt", "Windows path grammar")
    def test_windows_path_refusals(self):
        refused = ["relative\\file.zip", "\\\\server\\share\\f.zip", "\\\\?\\C:\\f.zip", "\\\\.\\PhysicalDrive0",
                   "C:\\dir\\*.zip", "C:\\dir\\f.zip:secret", "C:\\dir\\..\\f.zip", "C:\\dir\\f.zip.", "C:\\dir\\NUL",
                   "C:\\dir\\com1.txt", "C:\\dir\\", "C:\\dir\\\\f.zip", "C:f.zip"]
        for path in refused:
            with self.subTest(path=path):
                self.assertTrue(self.request(path, key="r" + str(abs(hash(path))))["isError"])
        self.assertEqual(filesnap.normalize_path('"c:/Users/x/f.zip"'), "C:\\Users\\x\\f.zip")

    @unittest.skipUnless(os.name == "nt", "NTFS junction")
    def test_junction_parent_is_refused(self):
        real = self.outside / "real"
        real.mkdir()
        (real / "f.txt").write_text("secret", encoding="utf-8")
        link = self.outside / "link"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True)
        if made.returncode:
            self.skipTest("junction could not be created")
        self.assertIn("link or junction", self.error(self.approve(self.request(link / "f.txt", key="j"))))

    @unittest.skipUnless(os.name == "nt", "Windows share modes")
    def test_active_writer_falls_back_and_changes_are_detected(self):
        with open(self.file, "ab") as writer:  # Python shares read/write, so an exclusive read open fails
            ready = self.approve(self.request())["structuredContent"]["data"]
            self.assertEqual(ready["consistency"], "metadata_unchanged_during_read")
            self.assertEqual(self.read_all(ready), self.original)

            def grow(path):
                writer.write(b"more")
                writer.flush()
            with patch.object(filesnap, "_on_block", grow):
                self.assertIn("kept changing", self.error(self.approve(self.request(key="moving"))))
