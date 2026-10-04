"""Real facade stop/restart checks; Windows Break needs an attached console."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'tools'))
import support


class ShutdownSignalsTests(unittest.TestCase):
    def test_windows_handlers_restore_inherited_ctrl_c(self):
        reset = mock.Mock(return_value=True)
        kernel = mock.Mock(SetConsoleCtrlHandler=reset)
        handler = mock.Mock()
        with mock.patch.object(support.os, 'name', 'nt'), \
             mock.patch.object(signal, 'SIGBREAK', 21, create=True), \
             mock.patch.object(signal, 'signal') as register, \
             mock.patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            support.prepare_shutdown_signals(handler)
        self.assertEqual(register.call_args_list,
                         [mock.call(signal.SIGINT, handler), mock.call(signal.SIGTERM, handler),
                          mock.call(21, handler)])
        reset.assert_called_once_with(None, False)

    def test_headless_windows_console_is_supported(self):
        kernel = mock.Mock()
        kernel.SetConsoleCtrlHandler.return_value = False
        with mock.patch.object(support.os, 'name', 'nt'), \
             mock.patch.object(signal, 'SIGBREAK', 21, create=True), \
             mock.patch.object(signal, 'signal'), \
             mock.patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
             mock.patch.object(ctypes, 'get_last_error', return_value=6, create=True):
            support.prepare_shutdown_signals(lambda *_: None)

    def test_other_windows_console_errors_are_not_silently_ignored(self):
        kernel = mock.Mock()
        kernel.SetConsoleCtrlHandler.return_value = False
        with mock.patch.object(support.os, 'name', 'nt'), \
             mock.patch.object(signal, 'SIGBREAK', 21, create=True), \
             mock.patch.object(signal, 'signal'), \
             mock.patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
             mock.patch.object(ctypes, 'get_last_error', return_value=5, create=True), \
             mock.patch.object(ctypes, 'WinError', return_value=OSError('console reset failed'), create=True):
            with self.assertRaises(OSError):
                support.prepare_shutdown_signals(lambda *_: None)


class FacadeShutdownTests(unittest.TestCase):
    def stopped_then_restart(self, sent_signal, inherited_ignore=False):
        with tempfile.TemporaryDirectory(prefix='relay-shutdown-') as temp:
            root = Path(temp) / 'clean root'
            install = subprocess.run([sys.executable, str(PACKAGE / 'install.py'), '--root', str(root)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(install.returncode, 0, install.stderr)
            argv = [sys.executable, str(root / 'maintenance/launch.py'), '--root', str(root), '--mode', 'stdio']
            kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {}
            if inherited_ignore:
                kwargs['preexec_fn'] = lambda: signal.signal(signal.SIGINT, signal.SIG_IGN)
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding='utf-8', **kwargs)
            lock = root / 'state/facade.lock'
            try:
                process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}) + '\n')
                process.stdin.flush()
                self.assertEqual(json.loads(process.stdout.readline())['id'], 1)
                child_pid = json.loads(lock.read_text())['child_pid']
                for event in sent_signal if isinstance(sent_signal, tuple) else (sent_signal,):
                    try:
                        process.send_signal(event)
                    except ProcessLookupError:
                        break
                process.wait(timeout=15)
                self.assertEqual(process.returncode, 130, process.stderr.read())
                self.assertFalse(lock.exists(), 'Clean shutdown left facade.lock')
                if os.name != 'nt':
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                restarted = subprocess.run(argv, input=json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}) + '\n',
                                           capture_output=True, text=True, timeout=15)
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                self.assertEqual(json.loads(restarted.stdout)['id'], 2)
                self.assertFalse(lock.exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    @unittest.skipIf(os.name == 'nt', 'POSIX SIGTERM transport')
    def test_sigterm_cleans_child_and_lock_then_restarts(self):
        self.stopped_then_restart(signal.SIGTERM)

    @unittest.skipIf(os.name == 'nt', 'POSIX inherited SIGINT transport')
    def test_inherited_ignored_sigint_is_restored_and_restart_works(self):
        self.stopped_then_restart(signal.SIGINT, inherited_ignore=True)

    @unittest.skipIf(os.name == 'nt', 'POSIX repeated signal transport')
    def test_repeated_shutdown_events_do_not_interrupt_cleanup(self):
        self.stopped_then_restart((signal.SIGINT, signal.SIGTERM))

    @unittest.skipUnless(os.name == 'nt', 'Real Windows console Ctrl+Break transport')
    def test_windows_ctrl_break_cleans_lock_then_restarts(self):
        if not ctypes.windll.kernel32.GetConsoleCP():
            self.skipTest('Run this test in a Windows console to send Ctrl+Break')
        self.stopped_then_restart(signal.CTRL_BREAK_EVENT)


if __name__ == '__main__':
    unittest.main()
