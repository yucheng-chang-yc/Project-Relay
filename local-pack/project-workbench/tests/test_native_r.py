"""Native trusted Rscript backend with a real R installation (skips when Rscript is absent).

Set WB_TEST_RSCRIPT to an absolute Rscript path, or rely on PATH / the default Windows location.
No model or Claude CLI is used: the task-bound CommandRuntime is driven directly.
"""
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_runtime as fixtures
from workbench.core import Runtime, WorkbenchError, digest, process_alive, run_git
from workbench.restricted import NATIVE_ACK, CommandRuntime, capture_runs, stop_owned, validate_policy


def find_rscript():
    for candidate in (os.environ.get("WB_TEST_RSCRIPT"), shutil.which("Rscript")):
        if candidate and Path(candidate).is_absolute() and Path(candidate).is_file():
            return candidate
    return None


RSCRIPT = find_rscript()
REAL_POPEN = fixtures.subprocess.Popen


@unittest.skipUnless(RSCRIPT, "No Rscript installation available for native trusted R tests")
class NativeRTests(unittest.TestCase):
    setUp_base = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown

    def setUp(self):
        self.setUp_base()
        data = json.loads(self.config.read_text())
        data["projects"][0]["compute_runtime"] = {"backend": "native_trusted", "rscript": RSCRIPT,
                                                  "trust_acknowledgement": NATIVE_ACK}
        self.config.write_text(json.dumps(data))
        self.runtime = Runtime(self.config)

    def running_task(self, key="native", inputs=None):
        worker = Mock(pid=999999999)
        def launch(argv, *args, **kwargs):
            return REAL_POPEN(argv, *args, **kwargs) if argv[0] == "git" else worker
        with patch("workbench.core.subprocess.Popen", side_effect=launch):
            task = self.runtime.start_task("test", "claude", {"goal": "native R", "acceptance": ["evidence"],
                                           "compute_runtime": True, "inputs": inputs or []}, key)
        directory = self.runtime.task_dir(task["id"])
        worktree = directory / "worktree"
        run_git(self.root, ["worktree", "add", "--detach", str(worktree), task["base_head"]])
        with self.runtime.connection(write=True) as c:
            c.execute("UPDATE tasks SET status='running',worktree=? WHERE id=?", (str(worktree), task["id"]))
        self.runtime.loop.materialize(task["id"], worktree)
        return task["id"], worktree, CommandRuntime(self.runtime, task["id"])

    @staticmethod
    def settle(commands, run_id, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = commands.get_run(run_id)
            if data.get("status") not in ("running_or_uncertain", "starting") and "exit_code" in data:
                return data
            time.sleep(.1)
        raise AssertionError("Run did not settle")

    def test_intentional_error_then_repaired_rerun_with_evidence(self):
        task_id, worktree, commands = self.running_task()
        (worktree / "analysis.R").write_text('stop("WB_INTENTIONAL_ERROR: repair me")\n', encoding="utf-8")
        failed = self.settle(commands, commands.run("Rscript", "analysis.R", "run-1", 30)["run_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotEqual(failed["exit_code"], 0)
        self.assertIn("WB_INTENTIONAL_ERROR", failed["stderr_preview"])
        self.assertTrue(failed["termination_confirmed"])
        self.assertFalse(failed["os_containment"])
        with self.assertRaises(WorkbenchError):  # Same key, different script bytes.
            (worktree / "analysis.R").write_text('write.csv(data.frame(a=1:3), "out.csv", row.names=FALSE)\n', encoding="utf-8")
            commands.run("Rscript", "analysis.R", "run-1", 30)
        fixed = self.settle(commands, commands.run("Rscript", "analysis.R", "run-2", 30)["run_id"])
        self.assertEqual((fixed["status"], fixed["exit_code"]), ("completed", 0))
        self.assertTrue((worktree / "out.csv").is_file())
        self.assertEqual(commands.run("Rscript", "analysis.R", "run-2", 30)["run_id"], fixed["run_id"])
        runs = capture_runs(self.runtime, task_id)
        self.assertEqual([r["receipt"]["status"] for r in runs if r["run_id"] in (failed["run_id"], fixed["run_id"])].count("completed"), 1)

    def test_timeout_and_cancel_leave_no_r_processes(self):
        _, worktree, commands = self.running_task()
        (worktree / "slow.R").write_text("Sys.sleep(60)\n", encoding="utf-8")
        timed = self.settle(commands, commands.run("Rscript", "slow.R", "slow-1", 2)["run_id"])
        self.assertEqual(timed["status"], "timed_out")
        self.assertTrue(timed["termination_confirmed"])
        started = commands.run("Rscript", "slow.R", "slow-2", 60)["run_id"]
        pid_file = commands.runtime.task_dir(commands.task_id) / "command-runs" / started / "native.json"
        end = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < end:
            time.sleep(.05)
        time.sleep(1)
        commands.terminate_run(started)
        cancelled = self.settle(commands, started)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["termination_confirmed"])
        for run in (timed["run_id"], started):
            pid = json.loads((commands.runtime.task_dir(commands.task_id) / "command-runs" / run / "native.json").read_text())["pid"]
            self.assertFalse(process_alive(pid))

    def test_background_child_started_by_r_is_stopped_after_exit(self):
        _, worktree, commands = self.running_task()
        rscript = RSCRIPT.replace("\\", "/")
        (worktree / "spawn.R").write_text(
            f'system2("{rscript}", c("-e", shQuote("writeLines(as.character(Sys.getpid()), \'child.pid\'); Sys.sleep(60)")), wait=FALSE)\n'
            'for (i in 1:100) { if (file.exists("child.pid")) break; Sys.sleep(.1) }\n', encoding="utf-8")
        result = self.settle(commands, commands.run("Rscript", "spawn.R", "spawn-1", 30)["run_id"])
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["termination_confirmed"])
        # On Windows R's wait=FALSE child may already be gone when the root exits, so the count
        # at stop can be 0; orphan survival is covered deterministically in test_host_diagnostics.
        self.assertIsInstance(result["processes_alive_at_stop"], int)
        child = int((worktree / "child.pid").read_text().strip())
        end = time.monotonic() + 5
        while process_alive(child) and time.monotonic() < end:
            time.sleep(.05)
        self.assertFalse(process_alive(child))

    def test_environment_excludes_facade_secrets_and_detects_input_modification(self):
        data = b"plant,yield\nT01,1.1\n"
        path = self.base / "input.csv"
        path.write_bytes(data)
        item = self.runtime.loop.stage_local("test", path, digest(data), "stage-native")
        _, worktree, commands = self.running_task(inputs=[{"input_id": item["input_id"], "destination": "wb_inputs/input.csv"}])
        (worktree / "env.R").write_text('cat("KEY=[", Sys.getenv("CONTROL_PLANE_API_KEY"), "]\\n", sep="")\n'
                                        'writeLines("tampered", "wb_inputs/input.csv")\n', encoding="utf-8")
        with patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": "sk-SHOULD-NOT-REACH-R"}):
            result = self.settle(commands, commands.run("Rscript", "env.R", "env-1", 30)["run_id"])
        self.assertIn("KEY=[]", result["stdout_preview"])
        self.assertNotIn("SHOULD-NOT-REACH-R", result["stdout_preview"])
        self.assertEqual(result["status"], "input_modified")
        self.assertEqual(result["modified_inputs"], ["wb_inputs/input.csv"])

    def test_policy_requires_ack_absolute_rscript_and_rscript_profile_only(self):
        good = {"backend": "native_trusted", "rscript": RSCRIPT, "trust_acknowledgement": NATIVE_ACK}
        self.assertEqual(validate_policy(good), good)
        for bad in ({**good, "trust_acknowledgement": "yes"}, {k: v for k, v in good.items() if k != "trust_acknowledgement"},
                    {**good, "rscript": "Rscript.exe"}, {**good, "rscript": sys.executable},
                    {**good, "enforcement_verified": True}):
            with self.assertRaises(WorkbenchError):
                validate_policy(bad)
        _, worktree, commands = self.running_task()
        (worktree / "x.py").write_text("print(1)\n", encoding="utf-8")
        with self.assertRaises(WorkbenchError):
            commands.run("Python", "x.py", "py-1", 10)
        data = json.loads(self.config.read_text())
        data["projects"][0]["claude_allowed_tools"] = ["Read", "Bash(Rscript *)"]
        self.config.write_text(json.dumps(data))
        with self.assertRaises(WorkbenchError):
            Runtime(self.config).start_task("test", "claude", {"goal": "x", "acceptance": ["y"], "compute_runtime": True}, "bash")

    def test_capabilities_report_native_trust_without_containment(self):
        caps = self.runtime.get_project_capabilities("test")
        self.assertEqual(caps["computational_runtime"]["backend"], "native_trusted")
        self.assertFalse(caps["computational_runtime"]["os_containment"])
        self.assertTrue(caps["features"]["native_trusted_r"])
        self.assertFalse(caps["features"]["computational_runtime_verified"])
        self.assertNotIn(RSCRIPT, json.dumps(caps))


if __name__ == "__main__":
    unittest.main()
