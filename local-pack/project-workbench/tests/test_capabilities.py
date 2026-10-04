"""Behavioral regressions for runtime 0.1.2; every workspace is disposable."""
import hashlib
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch, Mock

import test_runtime as fixtures
from workbench.core import Runtime, WorkbenchError, run_git
from workbench.mcp import MCP
from workbench.worker import execute

REAL_POPEN = subprocess.Popen


class CapabilityTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown
    start = fixtures.RuntimeTests.start
    wait = fixtures.RuntimeTests.wait

    def config_data(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def save_config(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")
        return Runtime(self.config)

    def alias(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)],
                           check=True, capture_output=True)

    def queued_task(self):
        worker = Mock(pid=999999999)
        def launch(argv, *args, **kwargs):
            return REAL_POPEN(argv, *args, **kwargs) if argv[0] == "git" else worker
        # Do not patch threading.Thread: it is the global module, and Windows subprocess.communicate
        # reads pipes with threads. The Mock worker's wait() returns immediately anyway.
        with patch("workbench.core.subprocess.Popen", side_effect=launch):
            return self.runtime.start_task("test", "codex", {
                "goal": "Binding regression", "acceptance": ["No execution after binding changes"]}, "binding")

    @staticmethod
    def allow_only_git(argv, *args, **kwargs):
        if argv[0] != "git":
            raise AssertionError("Query or changed task launched an executor")
        return REAL_POPEN(argv, *args, **kwargs)

    def test_utf8_pages_preserve_every_character_and_boundary(self):
        original = "A中文😀é\n�尾"
        raw = original.encode("utf-8")
        (self.root / "unicode.txt").write_bytes(raw)
        for limit in range(1, 9):
            offset, parts = 0, []
            while True:
                page = self.runtime.read_file("test", "unicode.txt", offset, limit)
                self.assertFalse(page["lossy"])
                self.assertLessEqual(page["next_offset"] - offset, limit + 3)
                parts.append(page["text"])
                self.assertGreater(page["next_offset"], offset)
                offset = page["next_offset"]
                if not page["truncated"]:
                    break
            self.assertEqual(offset, len(raw))
            self.assertEqual("".join(parts), original)
        with self.assertRaisesRegex(WorkbenchError, "inside a UTF-8"):
            self.runtime.read_file("test", "unicode.txt", 2, 1)

    def test_invalid_utf8_is_explicit_and_can_be_paginated(self):
        (self.root / "invalid.dat").write_bytes(b"\xff\x80A\xe4\xb8")
        offset, lossy = 0, False
        while offset < 5:
            page = self.runtime.read_file("test", "invalid.dat", offset, 1)
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
            lossy |= page["lossy"]
        self.assertTrue(lossy)
        eof = self.runtime.read_file("test", "invalid.dat", 8, 1)
        self.assertFalse(eof["truncated"])
        self.assertEqual(eof["text"], "")

    def test_resolved_metadata_is_fenced_but_normal_alias_is_usable(self):
        self.alias(self.root / "metadata-alias", self.root / ".git")
        normal = self.root / "normal"
        normal.mkdir()
        (normal / "note.txt").write_text("readable", encoding="utf-8")
        self.alias(self.root / "normal-alias", normal)
        for path in ("metadata-alias/config", ".GIT/config", ".git./config"):
            with self.assertRaises(WorkbenchError):
                self.runtime.read_file("test", path)
            with self.assertRaises(WorkbenchError):
                self.runtime.write_file("test", path, "blocked", "")
            with self.assertRaises(WorkbenchError):
                self.runtime.stat_file("test", path)
        self.assertEqual(self.runtime.read_file("test", "normal-alias/note.txt")["text"], "readable")
        self.assertTrue(self.runtime.stat_file("test", "normal-alias/note.txt")["is_alias"])

    def test_search_prunes_junctions_into_metadata_and_outside_root(self):
        (self.root / ".git" / "hidden-note").write_text("SENSITIVE_SENTINEL", encoding="utf-8")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "hidden-note").write_text("SENSITIVE_SENTINEL", encoding="utf-8")
        self.alias(self.root / "metadata-alias", self.root / ".git")
        self.alias(self.root / "outside-alias", outside)
        # Junctions are not symlinks on Windows; exercise that branch on POSIX too.
        walk = [(str(self.root), ["metadata-alias", "outside-alias"], ["baseline.txt"])]
        def emulate_junction_walk(*args, **kwargs):
            yield walk[0]
            for name in walk[0][1]:
                yield str(self.root / name), [], ["hidden-note"]
        with patch("workbench.core.os.walk", side_effect=emulate_junction_walk), patch.object(Path, "is_symlink", return_value=False):
            self.assertEqual(self.runtime.search_files("test", "SENSITIVE_SENTINEL")["matches"], [])
        self.assertEqual(walk[0][1], [])

    def test_duplicate_canonical_roots_are_rejected(self):
        data = self.config_data()
        data["projects"].append({**data["projects"][0], "id": "duplicate"})
        with self.assertRaisesRegex(WorkbenchError, "canonical project root"):
            self.save_config(data)
        data["projects"].pop()
        self.alias(self.base / "project-alias", self.root)
        data["projects"].append({**data["projects"][0], "id": "alias", "root": str(self.base / "project-alias")})
        with self.assertRaisesRegex(WorkbenchError, "canonical project root"):
            self.save_config(data)

    def test_renamed_project_id_cannot_bypass_active_root_lock(self):
        original = self.config_data()
        task = self.start("timeout", timeout=20)
        # Wait until its worker has claimed before changing config.
        import time
        end = time.monotonic() + 5
        while not self.runtime.get_task(task["id"])["child_pid"] and time.monotonic() < end:
            time.sleep(.05)
        self.assertTrue(self.runtime.get_task(task["id"])["child_pid"])
        renamed = self.config_data()
        renamed["projects"][0]["id"] = "renamed"
        try:
            runtime = self.save_config(renamed)
            self.assertEqual(runtime.preflight_task("renamed", "codex")["status"], "blocked")
            with self.assertRaisesRegex(WorkbenchError, "active/uncertain"):
                runtime.write_file("renamed", "new.txt", "blocked", "")
        finally:
            self.save_config(original)
            self.runtime.cancel_task(task["id"])
            self.wait(task["id"])

    def test_linked_worktree_shares_repository_writer_lock(self):
        linked = self.base / "linked"
        run_git(self.root, ["worktree", "add", "--detach", str(linked), "HEAD"])
        data = self.config_data()
        data["projects"].append({**data["projects"][0], "id": "linked", "root": str(linked)})
        self.runtime = self.save_config(data)
        task = self.start("slow")
        try:
            with self.assertRaisesRegex(WorkbenchError, "active/uncertain"):
                self.runtime.write_file("linked", "new.txt", "blocked", "")
        finally:
            self.wait(task["id"])

    def test_capability_queries_are_sanitized_and_do_not_launch(self):
        data = self.config_data()
        data["projects"][0]["claude_allowed_tools"] = ["Read", "Write(PRIVATE_SENTINEL)"]
        data["projects"][0]["codex_command"].append("PRIVATE_SENTINEL")
        self.runtime = self.save_config(data)
        with patch("workbench.core.subprocess.Popen", side_effect=self.allow_only_git):
            cap = MCP(self.runtime).call("get_project_capabilities", {"project_id": "test"})
            result = self.runtime.preflight_task("test", "claude", ["shell_execution"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(cap))
        self.assertEqual(cap["executors"]["claude"]["bash_policy"], "disabled")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["authentication"], "unknown")
        self.assertFalse(result["task_created"])
        self.assertEqual(self.runtime.list_tasks(), [])

    def test_preflight_reports_missing_policy_readonly_and_unsupported_features(self):
        with patch("workbench.core.subprocess.Popen", side_effect=self.allow_only_git):
            self.assertEqual(self.runtime.preflight_task("test", "codex", ["text_read"])["status"], "eligible")
            self.assertEqual(self.runtime.preflight_task("test", "claude", ["shell_execution"])["status"], "blocked")
            self.assertEqual(self.runtime.preflight_task("test", "codex", ["binary_host_transfer"])["status"], "blocked")
            self.assertEqual(self.runtime.preflight_task("test", "command", profile="missing")["status"], "blocked")
            data = self.config_data()
            data["projects"][0]["writable"] = False
            data["projects"][0].pop("codex_command")
            runtime = self.save_config(data)
            preflight = runtime.preflight_task("test", "codex")
            self.assertEqual(preflight["status"], "blocked")
            self.assertEqual(runtime.list_tasks(), [])

    def test_preflight_requires_committed_exact_repository_root(self):
        data = self.config_data()
        nested = self.root / "nested"
        nested.mkdir()
        data["projects"][0]["root"] = str(nested)
        runtime = self.save_config(data)
        self.assertEqual(runtime.preflight_task("test", "codex")["status"], "blocked")
        empty = self.base / "empty"
        empty.mkdir()
        data["projects"][0]["root"] = str(empty)
        runtime = self.save_config(data)
        self.assertEqual(runtime.preflight_task("test", "codex")["status"], "blocked")

    def test_invalid_claude_policy_blocks_start_before_task_creation(self):
        data = self.config_data()
        data["projects"][0]["claude_max_turns"] = True
        runtime = self.save_config(data)
        self.assertFalse(runtime.get_project_capabilities("test")["executors"]["claude"]["configured"])
        self.assertEqual(runtime.preflight_task("test", "claude")["status"], "blocked")
        with self.assertRaises(WorkbenchError):
            runtime.start_task("test", "claude", {"goal": "blocked", "acceptance": ["invalid"]}, "invalid")
        self.assertEqual(runtime.list_tasks(), [])

    def test_directory_pages_filter_aliases_hash_files_and_reject_stale_cursor(self):
        self.alias(self.root / "git-alias", self.root / ".git")
        self.alias(self.root / "outside-alias", self.base)
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text(name, encoding="utf-8")
        tool = MCP(self.runtime)
        page = tool.call("list_directory", {"project_id": "test", "limit": 2})
        self.assertEqual([e["name"] for e in page["entries"]], ["a.txt", "b.txt"])
        self.assertEqual(page["filtered_entries"], 3)
        next_page = self.runtime.list_directory("test", cursor=page["next_cursor"], limit=2)
        self.assertEqual([e["name"] for e in next_page["entries"]], ["baseline.txt", "c.txt"])
        self.assertIsNone(next_page["next_cursor"])
        info = tool.call("stat_file", {"project_id": "test", "path": "a.txt", "include_sha256": True})
        self.assertEqual(info["sha256"], hashlib.sha256(b"a.txt").hexdigest())
        self.assertEqual(info["size_bytes"], 5)
        with self.assertRaises(WorkbenchError):
            tool.call("stat_file", {"project_id": "test", "path": "a.txt", "include_sha256": "true"})
        (self.root / "d.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(WorkbenchError, "stale"):
            self.runtime.list_directory("test", cursor=page["next_cursor"])

    def test_config_change_blocks_stale_facade_and_queued_worker(self):
        task = self.queued_task()
        data = self.config_data()
        data["projects"][0]["claude_allowed_tools"] = ["Read", "Bash"]
        newer = self.save_config(data)
        with self.assertRaisesRegex(WorkbenchError, "configuration changed"):
            self.runtime.write_file("test", "new.txt", "blocked", "")
        with patch("workbench.worker.subprocess.Popen", side_effect=self.allow_only_git):
            with self.assertRaisesRegex(WorkbenchError, "binding changed"):
                execute(newer, task["id"])
        self.assertFalse((self.runtime.task_dir(task["id"]) / "worktree").exists())
        self.assertEqual(newer.get_task(task["id"])["status"], "queued")

    def test_same_head_clone_rebinding_cannot_apply_old_task(self):
        task = self.runtime.start_task("test", "claude", {
            "goal": "binding integration", "acceptance": ["fixture"]}, "clone-binding")
        self.assertEqual(self.wait(task["id"])["status"], "completed")
        result = self.runtime.get_task_result(task["id"])
        self.runtime.mark_task_reviewed(task["id"], result["result_sha256"], "accept_changes", "Fixture reviewed")
        clone = self.base / "clone"
        run_git(self.base, ["clone", str(self.root), str(clone)])
        self.assertEqual(run_git(clone, ["rev-parse", "HEAD"]), run_git(self.root, ["rev-parse", "HEAD"]))
        data = self.config_data()
        data["projects"][0]["root"] = str(clone)
        runtime = self.save_config(data)
        with self.assertRaisesRegex(WorkbenchError, "binding changed"):
            runtime.apply_task_changes(task["id"], result["result"]["diff_sha256"])
        self.assertFalse((clone / "claude-analysis.txt").exists())
        self.assertFalse((self.root / "claude-analysis.txt").exists())

    def test_legacy_database_migrates_without_fabricating_task_binding(self):
        task = self.runtime.start_task("test", "claude", {
            "goal": "legacy integration", "acceptance": ["fixture"]}, "legacy")
        self.assertEqual(self.wait(task["id"])["status"], "completed")
        result = self.runtime.get_task_result(task["id"])
        self.runtime.mark_task_reviewed(task["id"], result["result_sha256"], "accept_changes", "Legacy fixture reviewed")
        with contextlib.closing(sqlite3.connect(self.runtime.db)) as c:
            for column in ("project_root", "repo_identity", "project_binding_sha256"):
                c.execute(f"ALTER TABLE tasks DROP COLUMN {column}")
            c.commit()
        migrated = Runtime(self.config)
        legacy = migrated.get_task_result(task["id"])
        self.assertEqual(legacy["result_sha256"], result["result_sha256"])
        self.assertIsNone(legacy["task"]["project_binding_sha256"])
        with self.assertRaisesRegex(WorkbenchError, "Legacy task"):
            migrated.apply_task_changes(task["id"], result["result"]["diff_sha256"])
        self.assertFalse((self.root / "claude-analysis.txt").exists())

    def test_legacy_uncertain_task_remains_locked_after_project_rename(self):
        task = self.queued_task()
        with self.runtime.connection(write=True) as c:
            c.execute("UPDATE tasks SET project_root=NULL,repo_identity=NULL,project_binding_sha256=NULL WHERE id=?", (task["id"],))
        data = self.config_data()
        data["projects"][0]["id"] = "renamed"
        runtime = self.save_config(data)
        with self.assertRaisesRegex(WorkbenchError, "active/uncertain"):
            runtime.write_file("renamed", "new.txt", "blocked", "")


if __name__ == "__main__":
    unittest.main()
