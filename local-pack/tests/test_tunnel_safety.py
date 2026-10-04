import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
from support import mcp_command_text, mcp_stdio_argv
import tunnel_safety as safety

TUNNEL = 'tunnel_' + '1' * 32
OTHER = 'tunnel_' + '2' * 32
KEY = 'EXPLICIT_FILE_KEY_TEST_1234567890'
AMBIENT = 'WRONG_BRIDGE_KEY_TEST_1234567890'


class TunnelSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-safety-')
        self.base = Path(self.temp.name)
        self.file = self.base / 'private-key.txt'
        self.file.write_text(KEY)

    def tearDown(self):
        self.temp.cleanup()

    def test_windows_encoder_handles_spaces_unicode_quotes_and_backslashes(self):
        # Independent shell tokenizer reconstructs exactly the intended argv.
        # The real Windows full-client probe separately covers its own parser.
        for text in (r'C:\Users\user\ProjectRelay', r"C:\Users\analyst's account\分析 root", r'C:\Projects\$tools; & data'):
            root = PureWindowsPath(text)
            python = r'C:\Program Files\Python\python.exe'
            command = mcp_command_text(root, python, windows=True)
            self.assertNotIn('\\', command)
            self.assertEqual(shlex.split(command), mcp_stdio_argv(root, python, windows=True))
            self.assertEqual(shlex.split(command)[0], 'C:/Program Files/Python/python.exe')

    def test_ambient_key_is_ignored_even_when_prompt_is_available(self):
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': AMBIENT}), \
                patch('sys.stdin.isatty', return_value=True), patch('getpass.getpass', return_value=KEY):
            self.assertEqual(safety.explicit_credential({'type': 'prompt'}), KEY)

    def test_ambient_key_does_not_enable_noninteractive_prompt_mode(self):
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': AMBIENT}), \
                patch('sys.stdin.isatty', return_value=False), self.assertRaises(ValueError) as error:
            safety.explicit_credential({'type': 'prompt'})
        self.assertNotIn(AMBIENT, str(error.exception))

    def test_file_source_wins_and_rotates_without_saving_a_digest(self):
        source = {'type': 'file', 'path': str(self.file)}
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': AMBIENT}):
            self.assertEqual(safety.explicit_credential(source), KEY)
            new = self.base / 'new-key'
            new.write_text(KEY + '_ROTATED')
            os.replace(new, self.file)
            self.assertEqual(safety.explicit_credential(source), KEY + '_ROTATED')
        self.assertEqual(source, {'type': 'file', 'path': str(self.file)})

    def test_file_accepts_bom_crlf_and_refuses_empty_multiline_or_excess_bytes(self):
        source = {'type': 'file', 'path': str(self.file)}
        self.file.write_bytes(b'\xef\xbb\xbf' + KEY.encode() + b'\r\n')
        self.assertEqual(safety.explicit_credential(source), KEY)
        for raw in (b'', b'\n', (KEY + '\n' + AMBIENT).encode(), b'a\x00b', b'\xff', b'a' * (safety.MAX_KEY_BYTES + 1)):
            with self.subTest(bytes=len(raw)):
                self.file.write_bytes(raw)
                with self.assertRaises(ValueError) as error:
                    safety.explicit_credential(source)
                self.assertNotIn(KEY, str(error.exception))
                self.assertNotIn(AMBIENT, str(error.exception))

    def test_missing_or_unreadable_file_never_falls_back(self):
        source = {'type': 'file', 'path': str(self.file)}
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': AMBIENT}), \
                patch('tunnel_safety.os.open', side_effect=PermissionError(AMBIENT)), self.assertRaises(ValueError) as error:
            safety.explicit_credential(source)
        self.assertNotIn(AMBIENT, str(error.exception))
        self.file.unlink()
        with self.assertRaises(ValueError):
            safety.explicit_credential(source)

    def test_file_symlink_and_directory_are_refused(self):
        link = self.base / 'key-link'
        try:
            link.symlink_to(self.file)
        except OSError:
            self.skipTest('Host does not permit creating a symlink fixture')
        for path in (link, self.base, Path('relative-key.txt')):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safety.credential_file(path)

    def test_file_change_during_read_is_refused(self):
        actual = self.file.stat()
        changed = SimpleNamespace(st_dev=actual.st_dev, st_ino=actual.st_ino,
                                  st_size=actual.st_size, st_mtime_ns=actual.st_mtime_ns + 1)
        with patch('tunnel_safety.os.fstat', side_effect=[actual, changed]), self.assertRaises(ValueError):
            safety.explicit_credential({'type': 'file', 'path': str(self.file)})

    def test_same_tunnel_guard_excludes_other_roots_and_releases(self):
        code = ("import sys;sys.path.insert(0,sys.argv[1]);from tunnel_safety import tunnel_guard;"
                "\nwith tunnel_guard(sys.argv[2]): print('acquired')")
        with safety.tunnel_guard(TUNNEL):
            run = subprocess.run([sys.executable, '-c', code, str(TOOLS), TUNNEL], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn('acquired', run.stdout)
            self.assertNotEqual(safety.guard_port(TUNNEL), safety.guard_port(OTHER))
            with safety.tunnel_guard(OTHER):
                pass
        with safety.tunnel_guard(TUNNEL):
            pass

    def test_guard_releases_after_holder_process_is_killed(self):
        code = ("import sys;sys.path.insert(0,sys.argv[1]);from tunnel_safety import tunnel_guard;"
                "\nwith tunnel_guard(sys.argv[2]): print('held',flush=True);sys.stdin.read()")
        child = subprocess.Popen([sys.executable, '-c', code, str(TOOLS), TUNNEL],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'held')
            child.kill(); child.wait(timeout=10)
            with safety.tunnel_guard(TUNNEL):
                pass
        finally:
            if child.poll() is None:
                child.kill(); child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

    def test_unknown_legacy_binding_blocks_regardless_of_health_port(self):
        item = safety.classify_client(79368, ['old/tunnel-client.exe', 'run', '--profile', 'legacy',
                    '--health.listen-addr', '127.0.0.1:8091'], TUNNEL)
        self.assertEqual(item, {'pid': 79368, 'reason': 'active_client_binding_unconfirmed'})
        self.assertIsNone(safety.classify_client(4, ['tunnel-client', 'run', '--control-plane.tunnel-id=' + OTHER], TUNNEL))
        self.assertEqual(safety.classify_client(5, ['tunnel-client', 'run', '--control-plane.tunnel-id', TUNNEL], TUNNEL)['reason'], 'same_tunnel_client_active')
        for mode in ('init', 'doctor', '--version'):
            self.assertIsNone(safety.classify_client(6, ['tunnel-client', mode], TUNNEL))

    def test_cim_errors_are_sanitized_and_fail_closed(self):
        bad = [SimpleNamespace(returncode=1, stdout=KEY.encode(), stderr=AMBIENT.encode()),
               SimpleNamespace(returncode=0, stdout=b'not json', stderr=b''),
               SimpleNamespace(returncode=0, stdout=b'{"processes":null}', stderr=b''),
               SimpleNamespace(returncode=0, stdout=b'{"processes":[null]}', stderr=b'')]
        for run in bad:
            with patch('tunnel_safety.subprocess.run', return_value=run), self.assertRaises(ValueError) as error:
                safety.inspect_clients('tunnel-client.exe', TUNNEL, windows=True)
            self.assertNotIn(KEY, str(error.exception)); self.assertNotIn(AMBIENT, str(error.exception))
        with patch('tunnel_safety.subprocess.run', side_effect=subprocess.TimeoutExpired(KEY, 10)), self.assertRaises(ValueError):
            safety.inspect_clients('tunnel-client.exe', TUNNEL, windows=True)

    def test_cim_null_command_blocks_and_raw_secret_is_never_returned(self):
        raw = json.dumps({'processes': [{'pid': 11, 'command': None}, {'pid': 12, 'command': KEY}]}).encode()
        with patch('tunnel_safety.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=raw)), \
                patch('tunnel_safety.windows_argv', side_effect=[ValueError(), ['client', 'run', '--control-plane.api-key', KEY]]):
            rows = safety.inspect_clients('tunnel-client.exe', TUNNEL, windows=True)
        self.assertEqual([r['pid'] for r in rows], [11, 12])
        self.assertNotIn(KEY, json.dumps(rows))


if __name__ == '__main__':
    unittest.main()
