import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import upgrade_runtime as upgrade


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "new"
        self.target = self.base / "installed"
        self.state = self.base / "state"
        for root in (self.source, self.target):
            (root / "workbench").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "demo-project").mkdir()
            (root / "skills").mkdir()
        for root, version in ((self.source, "0.1.2"), (self.target, "0.1.1")):
            (root / "workbench" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
            (root / "workbench" / "core.py").write_text(version, encoding="utf-8")
            (root / "README.md").write_text(version, encoding="utf-8")
            (root / "demo-project" / "user.txt").write_text("new template" if root == self.source else "USER_PROJECT", encoding="utf-8")
            (root / "mcp.json").write_text("new template" if root == self.source else "USER_MCP", encoding="utf-8")
            (root / "skills" / "SKILL.md").write_text("new template" if root == self.source else "USER_SKILL", encoding="utf-8")
        self.state.mkdir()
        with contextlib.closing(sqlite3.connect(self.state / "workbench.sqlite3")) as c:
            c.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
            c.execute("INSERT INTO tasks VALUES ('historic', 'completed')")
            c.commit()
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"data_dir": str(self.state), "auth_token": "PRIVATE_SENTINEL",
            "projects": [{"id": "test", "root": str(self.target / "demo-project")}]}), encoding="utf-8")
        self.config_bytes = self.config.read_bytes()
        entries = [{"path": p.relative_to(self.source).as_posix(), "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                   for p in sorted(self.source.rglob("*")) if p.is_file()]
        (self.source / "docs" / "PACKAGE_MANIFEST.json").write_text(json.dumps({"version": "0.1.2", "files": entries}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return upgrade.make_plan(self.source, self.target, self.config)

    def test_upgrade_preserves_projects_config_state_mcp_and_skill(self):
        plan = self.plan()
        result = upgrade.apply_plan(plan)
        self.assertTrue(result["updated"])
        self.assertEqual(upgrade.version(self.target), "0.1.2")
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertEqual((self.target / "demo-project" / "user.txt").read_text(), "USER_PROJECT")
        self.assertEqual((self.target / "mcp.json").read_text(), "USER_MCP")
        self.assertEqual((self.target / "skills" / "SKILL.md").read_text(), "USER_SKILL")
        backup = Path(result["backup"])
        self.assertEqual((backup / "files" / "workbench" / "core.py").read_text(), "0.1.1")
        for db in (self.state / "workbench.sqlite3", backup / "workbench.sqlite3"):
            with contextlib.closing(sqlite3.connect(db)) as c:
                self.assertEqual(c.execute("SELECT * FROM tasks").fetchall(), [("historic", "completed")])

    def test_active_tasks_and_source_tampering_stop_upgrade(self):
        with contextlib.closing(sqlite3.connect(self.state / "workbench.sqlite3")) as c:
            c.execute("UPDATE tasks SET status='interrupted'")
            c.commit()
        with self.assertRaisesRegex(ValueError, "Active or uncertain"):
            upgrade.apply_plan(self.plan())
        self.assertEqual(upgrade.version(self.target), "0.1.1")
        (self.source / "workbench" / "core.py").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.plan()

    def test_partial_copy_failure_restores_previous_code(self):
        original_copy = upgrade.atomic_copy
        failed = False
        def fail_once(source, destination):
            nonlocal failed
            if source.parent == self.source / "workbench" and source.name == "core.py" and not failed:
                failed = True
                raise OSError("injected copy failure")
            return original_copy(source, destination)
        with patch.object(upgrade, "atomic_copy", side_effect=fail_once):
            with self.assertRaises(OSError):
                upgrade.apply_plan(self.plan())
        self.assertEqual(upgrade.version(self.target), "0.1.1")
        self.assertEqual((self.target / "README.md").read_text(), "0.1.1")
        self.assertEqual(self.config.read_bytes(), self.config_bytes)

    def test_protected_overlap_and_changed_configuration_are_rejected(self):
        plan = self.plan()
        self.config.write_text(self.config.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Configuration changed"):
            upgrade.apply_plan(plan)
        data = json.loads(self.config.read_text())
        data["projects"][0]["root"] = str(self.target)
        self.config.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
