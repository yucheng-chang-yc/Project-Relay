from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from workbench.core import Runtime, WorkbenchError, process_alive, run_git
from workbench.mcp import MCP, UI_URI

SOURCE = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        run_git(self.root, ["init", "-b", "main"])
        run_git(self.root, ["config", "user.name", "Workbench Test"])
        run_git(self.root, ["config", "user.email", "test@example.invalid"])
        (self.root / "baseline.txt").write_text("original\n")
        run_git(self.root, ["add", "baseline.txt"])
        run_git(self.root, ["commit", "-m", "baseline"])
        self.config = self.base / "config.json"
        profiles = {
            "smoke": [sys.executable, "-c", "import time;time.sleep(.2);print('ok')"],
            "slow": [sys.executable, "-c", "import time;time.sleep(2);print('ok')"],
            "timeout": [sys.executable, "-c", "import time;time.sleep(20)"],
            "fail": [sys.executable, "-c", "raise SystemExit(7)"],
            "artifact": [sys.executable, "-c", "from pathlib import Path;Path('binary.dat').write_bytes(bytes(range(256))*16384)"],
            "childtree": [sys.executable, "-c", "import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']);Path('grandchild.pid').write_text(str(p.pid));time.sleep(20)"],
        }
        self.config.write_text(json.dumps({"data_dir": str(self.base / "state"), "auth_token": "t" * 48,
            "projects": [{"id": "test", "name": "Test Project", "root": str(self.root), "writable": True,
                "profiles": profiles, "codex_command": [sys.executable, str(SOURCE / "tests" / "fake_codex.py")],
                "claude_command": [sys.executable, str(SOURCE / "tests" / "fake_claude.py")]}]}))
        self.runtime = Runtime(self.config)
        self.running = []

    def tearDown(self):
        # All tests await/cancel their workers before removing their isolated roots.
        for proc in self.running:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        # A worker writes its terminal state just before exiting; on Windows its open worker.log
        # blocks removal until the process is gone, so wait for every recorded worker first.
        try:
            with self.runtime.connection() as c:
                pids = [r[0] for r in c.execute("SELECT worker_pid FROM tasks WHERE worker_pid IS NOT NULL")]
        except Exception:
            pids = []
        end = time.monotonic() + 10
        while any(process_alive(pid) for pid in pids) and time.monotonic() < end:
            time.sleep(.05)
        self.temp.cleanup()

    def start(self, profile="smoke", timeout=5, key="request-1", artifacts=None):
        return self.runtime.start_task("test", "command", {"goal": "bounded test", "acceptance": ["CLI exits successfully"],
            "profile": profile, "timeout_seconds": timeout, "artifacts": artifacts or []}, key)

    def wait(self, ident, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            task = self.runtime.get_task(ident)
            if task["status"] not in ("queued", "running", "cancelling"):
                return task
            time.sleep(.05)
        raise AssertionError("Task did not settle: " + str(self.runtime.get_task(ident)))

    def test_atomic_file_preconditions_and_root_fence(self):
        original = self.runtime.read_file("test", "baseline.txt")
        with self.assertRaises(WorkbenchError):
            self.runtime.write_file("test", "baseline.txt", "wrong", "")
        self.runtime.write_file("test", "baseline.txt", "更新\n", original["sha256"])
        with self.assertRaises(WorkbenchError):
            self.runtime.write_file("test", "baseline.txt", "stale", original["sha256"])
        for path in ("../outside", ".git/config", "C:/private", "a/../../private"):
            with self.assertRaises(WorkbenchError):
                self.runtime.read_file("test", path)
        try:
            (self.root / "escape").symlink_to(self.base)
        except OSError:
            if os.name != "nt":
                raise
            # Unprivileged Windows accounts cannot create symlinks; a junction is the equivalent escape.
            subprocess.run(["cmd", "/c", "mklink", "/J", str(self.root / "escape"), str(self.base)],
                           check=True, capture_output=True)
        with self.assertRaises(WorkbenchError):
            self.runtime.read_file("test", "escape/config.json")
        self.assertEqual(self.runtime.search_files("test", "auth_token")["matches"], [])

    def test_duplicate_concurrent_launch_runs_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.start("slow"), range(2)))
        self.assertEqual(results[0]["id"], results[1]["id"])
        ident = results[0]["id"]
        with self.assertRaises(WorkbenchError):
            self.start("fail", key="request-1")
        self.assertEqual(self.wait(ident)["status"], "completed")
        with self.runtime.connection() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM task_events WHERE event='task.running'").fetchone()[0], 1)

    def test_active_project_blocks_other_writers(self):
        t = self.start("slow")
        with self.assertRaises(WorkbenchError):
            self.start(key="request-2")
        with self.assertRaises(WorkbenchError):
            self.runtime.write_file("test", "other.txt", "x", "")
        self.wait(t["id"])

    def test_exit_failure_is_not_completion(self):
        t = self.start("fail")
        done = self.wait(t["id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["exit_code"], 7)
        self.assertEqual(done["review_status"], "pending")

    def test_timeout_and_cancellation(self):
        first = self.start("timeout", timeout=1)
        self.assertEqual(self.wait(first["id"])["status"], "timed_out")
        second = self.start("timeout", key="cancel")
        self.runtime.cancel_task(second["id"])
        self.assertEqual(self.wait(second["id"])["status"], "cancelled")

    def test_binary_snapshot_and_tamper_detection(self):
        t = self.start("artifact", artifacts=["binary.dat"])
        self.assertEqual(self.wait(t["id"])["status"], "completed")
        result = self.runtime.get_task_result(t["id"])
        p, a = self.runtime.artifact_path(t["id"], 0)
        self.assertEqual(p.stat().st_size, 4 * 1024 * 1024)
        self.assertEqual(a["sha256"], hashlib.sha256((self.root / "binary.dat").read_bytes()).hexdigest())
        p.write_bytes(b"tampered")
        with self.assertRaises(WorkbenchError):
            self.runtime.read_task_artifact(t["id"], 0)

    @unittest.skipIf(os.name == "nt", "POSIX process-group evidence; native taskkill still needs Windows validation")
    def test_cancellation_stops_foreground_executor_descendants(self):
        task = self.start("childtree", timeout=10)
        pidfile = self.root / "grandchild.pid"
        end = time.monotonic() + 4
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(.03)
        self.assertTrue(pidfile.is_file())
        child = int(pidfile.read_text())
        self.assertTrue(process_alive(child))
        self.runtime.cancel_task(task["id"])
        self.assertEqual(self.wait(task["id"])["status"], "cancelled")
        end = time.monotonic() + 3
        while process_alive(child) and time.monotonic() < end:
            time.sleep(.03)
        self.assertFalse(process_alive(child))

    def test_missing_required_artifact_fails(self):
        t = self.start(artifacts=["missing.txt"])
        self.assertEqual(self.wait(t["id"])["status"], "failed")

    def test_codex_fixture_isolated_diff_and_review_identity(self):
        (self.root / "baseline.txt").write_text("existing user modification\n")
        original = (self.root / "baseline.txt").read_bytes()
        task = self.runtime.start_task("test", "codex", {"goal": "中文\nquoted ' task", "acceptance": ["return bounded report"],
                "artifacts": ["analysis.txt"], "timeout_seconds": 5}, "codex-one")
        done = self.wait(task["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual((self.root / "baseline.txt").read_bytes(), original)
        self.assertFalse((self.root / "analysis.txt").exists())
        with self.assertRaises(WorkbenchError):
            self.runtime.mark_task_reviewed(task["id"], done["result_sha256"], "accept_changes", "before retrieval")
        data = self.runtime.get_task_result(task["id"])
        self.assertEqual(set(data["result"]["changed_files"]), {"analysis.txt", "new file.txt"})
        with self.assertRaises(WorkbenchError):
            self.runtime.mark_task_reviewed(task["id"], "bad", "accept_changes", "wrong object")
        self.runtime.mark_task_reviewed(task["id"], data["result_sha256"], "accept_changes", "Fixture diff inspected")
        with self.assertRaises(WorkbenchError):
            self.runtime.apply_task_changes(task["id"], data["result"]["diff_sha256"])
        run_git(self.root, ["restore", "baseline.txt"])
        applied = self.runtime.apply_task_changes(task["id"], data["result"]["diff_sha256"])
        self.assertTrue(applied["applied"])
        self.assertEqual((self.root / "analysis.txt").read_text(encoding="utf-8"), "分析完成\n中文\nquoted ' task")
        self.assertTrue(self.runtime.apply_task_changes(task["id"], data["result"]["diff_sha256"])["already_applied"])

    def test_scoped_commit_preserves_unrelated_changes_and_pushes_exact_head(self):
        (self.root / "baseline.txt").write_text("unrelated dirty work\n")
        (self.root / "owned.txt").write_text("owned\n")
        old = self.runtime.git_status("test")["head"]
        commit = self.runtime.git_commit("test", ["owned.txt"], "scoped change", old)
        self.assertEqual(commit["committed_files"], ["owned.txt"])
        self.assertEqual(run_git(self.root, ["show", "HEAD:baseline.txt"]), "original")
        self.assertIn("baseline.txt", commit["status"]["porcelain"])
        remote = self.base / "remote.git"
        run_git(self.base, ["init", "--bare", str(remote)])
        run_git(self.root, ["remote", "add", "origin", str(remote)])
        pushed = self.runtime.git_push("test", "origin", "main", commit["head"])
        self.assertTrue(pushed["verified"])
        self.assertEqual(run_git(remote, ["rev-parse", "refs/heads/main"]), commit["head"])

    def test_claude_fixture_schema_isolation_and_reviewed_integration(self):
        tool = MCP(self.runtime)
        task = tool.call("start_claude_task", {"project_id": "test", "goal": "中文 Claude task",
                    "acceptance": ["return schema"], "artifacts": ["claude-analysis.txt"],
                    "timeout_seconds": 5, "idempotency_key": "claude-one"})
        done = self.wait(task["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertFalse((self.root / "claude-analysis.txt").exists())
        data = self.runtime.get_task_result(task["id"])
        self.assertEqual(data["result"]["executor_session_id"], "fixture-session")
        self.assertEqual(data["result"]["changed_files"], ["claude-analysis.txt"])
        self.assertIn("Fixture diagnostic", self.runtime.get_task_log(task["id"])["text"])
        self.runtime.mark_task_reviewed(task["id"], data["result_sha256"], "accept_changes", "Fixture diff inspected")
        self.runtime.apply_task_changes(task["id"], data["result"]["diff_sha256"])
        self.assertTrue((self.root / "claude-analysis.txt").is_file())

    def test_claude_permission_and_format_failures_never_claim_completion(self):
        for goal in ("permission-failure", "malformed-output", "invalid-schema", "auth-failure"):
            with self.subTest(goal=goal):
                task = self.runtime.start_task("test", "claude", {"goal": goal, "acceptance": ["valid result"],
                    "timeout_seconds": 5}, goal)
                done = self.wait(task["id"])
                self.assertEqual(done["status"], "failed", done)
                self.assertEqual(done["review_status"], "pending")
                self.assertFalse(self.runtime.get_task_result(task["id"])["result"]["complete"])
                self.assertEqual(self.runtime.get_task_result(task["id"])["result"]["integration"], "not_performed")
                self.assertTrue(done["error"])

    @unittest.skipIf(os.name == "nt", "Abrupt POSIX worker loss; Windows requires native live evidence")
    def test_worker_loss_retains_lock_until_local_recovery(self):
        task = self.start("timeout", timeout=20)
        end = time.monotonic() + 4
        while time.monotonic() < end:
            row = self.runtime.get_row(task["id"])
            if row["child_pid"]:
                break
            time.sleep(.03)
        else:
            self.fail("Child was not started")
        os.kill(row["worker_pid"], signal.SIGKILL)
        try:
            # Signal delivery is asynchronous; reconcile only after observed worker loss.
            end = time.monotonic() + 3
            while process_alive(row["worker_pid"]) and time.monotonic() < end:
                time.sleep(.03)
            self.assertFalse(process_alive(row["worker_pid"]))
            with self.runtime.connection(write=True) as c:
                c.execute("UPDATE tasks SET heartbeat=? WHERE id=?", (time.time() - 60, task["id"]))
            self.assertEqual(self.runtime.get_task(task["id"])["status"], "interrupted")
            with self.assertRaises(WorkbenchError):
                self.start(key="must-not-replay")
            with self.assertRaises(WorkbenchError):
                self.runtime.resolve_interrupted_locally(task["id"], "child still running")
        finally:
            os.killpg(row["child_pid"], signal.SIGKILL)
        end = time.monotonic() + 3
        while process_alive(row["child_pid"]) and time.monotonic() < end:
            time.sleep(.03)
        resolved = self.runtime.resolve_interrupted_locally(task["id"], "Fixture process group killed; workspace inspected")
        self.assertEqual(resolved["status"], "failed")
        self.assertEqual(self.wait(self.start(key="after-local-recovery")["id"])["status"], "completed")

    def test_staged_changes_block_scoped_commit(self):
        (self.root / "baseline.txt").write_text("already staged\n")
        (self.root / "owned.txt").write_text("new\n")
        run_git(self.root, ["add", "baseline.txt"])
        with self.assertRaises(WorkbenchError):
            self.runtime.git_commit("test", ["owned.txt"], "no", self.runtime.git_status("test")["head"])
        self.assertEqual(run_git(self.root, ["diff", "--cached", "--name-only"]), "baseline.txt")

    def test_mcp_stdio_wire_and_resource(self):
        proc = subprocess.Popen([sys.executable, "-m", "workbench.server", "--config", str(self.config), "--stdio"],
                                cwd=SOURCE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "ascii"})
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": UI_URI}}]
        out, err = proc.communicate("\n".join(json.dumps(r) for r in requests) + "\n", timeout=5)
        messages = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in messages], [1, 2, 3, 4])
        self.assertEqual(messages[2]["result"]["structuredContent"]["data"][0]["id"], "test")
        self.assertIn("Project Relay", messages[3]["result"]["contents"][0]["text"])

    def start_server(self, port):
        proc = subprocess.Popen([sys.executable, "-m", "workbench.server", "--config", str(self.config), "--port", str(port)],
                                cwd=SOURCE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.running.append(proc)
        end = time.monotonic() + 4
        while time.monotonic() < end:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.3).close()
                return proc
            except OSError:
                time.sleep(.05)
        raise AssertionError("Server not ready")

    def test_http_auth_origin_and_service_restart_keeps_worker(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = self.start_server(port)
        url = f"http://127.0.0.1:{port}/api/call"
        def call(name, args=None, token="t" * 48, origin=None):
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
            if origin:
                headers["Origin"] = origin
            req = urllib.request.Request(url, json.dumps({"name": name, "arguments": args or {}}).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=3) as response:
                return json.load(response)["data"]
        with self.assertRaises(urllib.error.HTTPError) as denied:
            call("list_projects", token="bad")
        denied.exception.close()
        self.assertEqual(denied.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            call("list_projects", origin="https://evil.example")
        denied.exception.close()
        self.assertEqual(denied.exception.code, 403)
        task = call("start_command_task", {"project_id": "test", "goal": "survive service restart", "acceptance": ["finish"],
                    "profile": "slow", "timeout_seconds": 5, "idempotency_key": "restart"})
        proc.terminate()
        proc.wait(timeout=3)
        self.assertEqual(self.wait(task["id"])["status"], "completed")
        self.start_server(port)
        recovered = call("get_task_result", {"task_id": task["id"]})
        self.assertEqual(recovered["task"]["status"], "completed")
        self.assertEqual(recovered["task"]["review_status"], "pending")


if __name__ == "__main__":
    unittest.main()
