"""Inbound host-file diagnostics and owned process-tree lifecycle (no model, no R)."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runtime as fixtures
from workbench.core import WorkbenchError, process_alive
from workbench.loop import pinned_download
from workbench.proctree import ProcessTree


class HostDiagnosticsTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    tearDown = fixtures.RuntimeTests.tearDown

    def test_unauthorized_host_reports_only_hostname_and_reason(self):
        url = "https://files.example.invalid/backend/upload/abc123?sig=SECRET_SIGNATURE&exp=1"
        with patch("socket.create_connection") as connect, self.assertRaises(WorkbenchError) as failure:
            pinned_download(url, ["other.example"], self.base / "never", 1, "0" * 64)
        connect.assert_not_called()
        message = str(failure.exception)
        self.assertIn("download_host=files.example.invalid", message)
        self.assertIn("reason=host_not_in_allowlist", message)
        for leaked in ("SECRET_SIGNATURE", "sig=", "/backend/upload", "abc123"):
            self.assertNotIn(leaked, message)

    def test_rejection_reasons_never_echo_credentials_or_paths(self):
        cases = {"http://public.example/p?token=SECRET": "not_https",
                 "https://user:SECRET@public.example/p": "embedded_credentials",
                 "https://public.example:444/p": "non_default_port"}
        for url, reason in cases.items():
            with self.subTest(reason=reason), self.assertRaises(WorkbenchError) as failure:
                pinned_download(url, ["public.example"], self.base / reason, 1, "0" * 64)
            self.assertIn("reason=" + reason, str(failure.exception))
            self.assertNotIn("SECRET", str(failure.exception))
            self.assertNotIn("/p", str(failure.exception))

    def test_invalid_file_payload_reports_field_names_not_values(self):
        payload = {"file_id": "file_123", "url": "https://files.example.invalid/x?sig=SECRET_SIGNATURE"}
        with self.assertRaises(WorkbenchError) as failure:
            self.runtime.loop.stage_binary_input("test", payload, 1, "0" * 64, "diag-1")
        message = str(failure.exception)
        self.assertIn('"url":"str"', message)
        self.assertIn('"file_id":"str"', message)
        self.assertNotIn("SECRET_SIGNATURE", message)
        self.assertNotIn("file_123", message)


class ProcessTreeTests(unittest.TestCase):
    def test_stop_terminates_descendants_and_confirms_empty_tree(self):
        script = ("import subprocess,sys,time;"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                  "print(p.pid,flush=True);time.sleep(60)")
        tree = ProcessTree()
        try:
            process = tree.start([sys.executable, "-c", script], stdout=fixtures.subprocess.PIPE)
            child = int(process.stdout.readline())
            self.assertTrue(process_alive(child))
            self.assertGreaterEqual(tree.active_processes(), 1)
            self.assertTrue(tree.stop())
            self.assertEqual(tree.active_processes(), 0)
            end = time.monotonic() + 5
            while process_alive(child) and time.monotonic() < end:
                time.sleep(.05)
            self.assertFalse(process_alive(child))
            process.stdout.close()
        finally:
            tree.close()

    def test_orphan_left_after_root_exit_is_counted_and_stopped(self):
        script = ("import subprocess,sys;"
                  "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                  "print(p.pid,flush=True)")
        tree = ProcessTree()
        try:
            process = tree.start([sys.executable, "-c", script], stdout=fixtures.subprocess.PIPE)
            child = int(process.stdout.readline())
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertTrue(process_alive(child))
            self.assertGreaterEqual(tree.active_processes(), 1)
            self.assertTrue(tree.stop())
            end = time.monotonic() + 5
            while process_alive(child) and time.monotonic() < end:
                time.sleep(.05)
            self.assertFalse(process_alive(child))
            process.stdout.close()
        finally:
            tree.close()

    def test_normal_exit_reports_no_remaining_processes(self):
        tree = ProcessTree()
        try:
            process = tree.start([sys.executable, "-c", "print('ok')"], stdout=fixtures.subprocess.DEVNULL)
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertTrue(tree.stop())
            self.assertEqual(tree.active_processes(), 0)
        finally:
            tree.close()


if __name__ == "__main__":
    unittest.main()
