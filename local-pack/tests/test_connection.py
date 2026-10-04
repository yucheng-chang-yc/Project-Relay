"""Offline bootstrap/package acceptance with a fake external tunnel executable.

The executable fixture runs on POSIX; it exercises real subprocess argv and
file effects, not a network connection, Windows process control or ChatGPT.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile


PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'tools'))
import connect_chatgpt as connection
from tunnel_safety import inspect_clients
from support import mcp_command_text, mcp_stdio_argv

APP_A = 'asdk_app_' + 'a' * 32
APP_B = 'asdk_app_' + 'b' * 32
TUNNEL_A = 'tunnel_' + '1' * 32
TUNNEL_B = 'tunnel_' + '2' * 32
RUNTIME_SECRET = 'RUNTIME_KEY_TEST_DO_NOT_PERSIST_1234567890'
LOCAL_SECRET = 'LOCAL_HTTP_TOKEN_TEST_DO_NOT_PERSIST_1234567890'


FAKE_CLIENT = r'''
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import time

args = sys.argv[1:]
with open(os.environ['FAKE_CAPTURE'], 'a', encoding='utf-8') as capture:
    capture.write(json.dumps({'argv': args,
                             'profile_dir': os.environ.get('TUNNEL_CLIENT_PROFILE_DIR'),
                             'key_present': bool(os.environ.get('CONTROL_PLANE_API_KEY')),
                             'key_sha256': hashlib.sha256(os.environ.get('CONTROL_PLANE_API_KEY', '').encode()).hexdigest(),
                             'admin_key_present': 'OPENAI_ADMIN_KEY' in os.environ,
                             'inherited_mcp_present': 'MCP_COMMAND' in os.environ,
                             'inherited_tunnel_id_present': 'CONTROL_PLANE_TUNNEL_ID' in os.environ}) + '\n')
print(os.environ.get('CONTROL_PLANE_API_KEY', 'no credential'))
print(os.environ.get('CONTROL_PLANE_API_KEY', 'no credential'), file=sys.stderr)
mode = args[0]
if mode == 'run' and os.environ.get('FAKE_STAY_RUNNING'):
    time.sleep(60)
if mode == 'init':
    tokens = shlex.split(args[args.index('--mcp-command') + 1])
    if not Path(tokens[0]).is_file() or not Path(tokens[1]).is_file():
        raise SystemExit(6)
    profile = args[args.index('--profile') + 1]
    path = Path(os.environ['TUNNEL_CLIENT_PROFILE_DIR']) / (profile + '.yaml')
    if not os.environ.get('FAKE_MISSING_PROFILE'):
        with path.open('x', encoding='utf-8') as out:
            out.write('config_version: 1\ncontrol_plane:\n  api_key: env:CONTROL_PLANE_API_KEY\n')
            out.write('  tunnel_id: ' + args[args.index('--tunnel-id') + 1] + '\n')
            out.write('health:\n  listen_addr: ' + json.dumps(args[args.index('--health-listen-addr') + 1]) + '\n')
            out.write('mcp:\n  command: ' + json.dumps(args[args.index('--mcp-command') + 1]) + '\n')
    raise SystemExit(int(os.environ.get('FAKE_INIT_EXIT', '0')))
raise SystemExit(int(os.environ.get('FAKE_EXIT', '0')))
'''


class ConnectionPureTests(unittest.TestCase):
    def test_app_ids_and_safe_urls_are_normalized(self):
        for supplied in (APP_A, 'plugin_' + APP_A,
                         'https://chatgpt.com/apps/' + APP_A,
                         'https://chatgpt.com/plugins/plugin_' + APP_A + '/',
                         'https://chatgpt.com/apps/project-relay/plugin_' + APP_A):
            with self.subTest(supplied=supplied):
                self.assertEqual(connection.app_id(supplied), APP_A)

    def test_unrelated_urls_aliases_and_ambiguous_ids_are_rejected(self):
        bad = ['plugins_' + 'a' * 32, 'asdk_app_' + 'a' * 31, APP_A + '/evil',
               'http://chatgpt.com/apps/' + APP_A,
               'https://chatgpt.com.evil.invalid/apps/' + APP_A,
               'https://someone@chatgpt.com/apps/' + APP_A,
               'https://chatgpt.com:443/apps/' + APP_A,
               'https://chatgpt.com/apps/' + APP_A + '?token=secret',
               'https://chatgpt.com/apps/' + APP_A + '#' + APP_B,
               'https://chatgpt.com/apps/' + APP_A + '/' + APP_B,
               'https://chatgpt.com/apps/%61sdk_app_' + 'a' * 32,
               'https://chatgpt.com/c/' + APP_A]
        for supplied in bad:
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                connection.app_id(supplied)

    def test_environment_isolation_preserves_proxy_but_removes_other_bindings(self):
        values = {'CONTROL_PLANE_API_KEY': RUNTIME_SECRET, 'OPENAI_API_KEY': 'another-key',
                  'OPENAI_ADMIN_KEY': 'admin-key', 'MCP_COMMAND': 'wrong-server',
                  'CONTROL_PLANE_TUNNEL_ID': TUNNEL_B, 'TUNNEL_CLIENT_PROFILE_DIR': 'wrong-profile',
                  'HTTPS_PROXY': 'http://proxy.invalid:8888'}
        with patch.dict(os.environ, values, clear=True):
            init = connection.child_environment(Path('/my-profiles'))
            live = connection.child_environment(Path('/my-profiles'), RUNTIME_SECRET)
        self.assertNotIn('CONTROL_PLANE_API_KEY', init)
        self.assertEqual(live['CONTROL_PLANE_API_KEY'], RUNTIME_SECRET)
        for env in (init, live):
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('OPENAI_ADMIN_KEY', env)
            self.assertNotIn('MCP_COMMAND', env)
            self.assertNotIn('CONTROL_PLANE_TUNNEL_ID', env)
            self.assertEqual(env['HTTPS_PROXY'], 'http://proxy.invalid:8888')
            self.assertEqual(env['TUNNEL_CLIENT_PROFILE_DIR'], str(Path('/my-profiles')))

    def test_interactive_key_is_not_written_to_parent_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.stdin.isatty', return_value=True), \
                patch('getpass.getpass', return_value=RUNTIME_SECRET):
            self.assertEqual(connection.runtime_credential(), RUNTIME_SECRET)
            self.assertNotIn('CONTROL_PLANE_API_KEY', os.environ)

    def test_noninteractive_missing_key_fails_without_prompt(self):
        with patch.dict(os.environ, {'CONTROL_PLANE_API_KEY': 'WRONG_AMBIENT_KEY'}, clear=True), patch('sys.stdin.isatty', return_value=False), \
                patch('getpass.getpass') as hidden, self.assertRaises(ValueError):
            connection.runtime_credential()
        hidden.assert_not_called()


@unittest.skipIf(os.name == 'nt', 'Executable fixture uses a POSIX shebang; real Windows/tunnel-client acceptance remains separate')
class ConnectionExecutableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-connection-')
        self.base = Path(self.temp.name)
        self.root = self.base / '使用者 install with spaces'
        self.root.mkdir()
        self.capture = self.base / 'fake-argv.jsonl'
        self.client = self.base / 'client with spaces' / 'tunnel-client'
        self.client.parent.mkdir()
        self.client.write_text('#!' + sys.executable + '\n' + FAKE_CLIENT, encoding='utf-8')
        self.client.chmod(0o700)
        self.env = {**os.environ, 'FAKE_CAPTURE': str(self.capture)}
        for name in ('CONTROL_PLANE_API_KEY', 'FAKE_INIT_EXIT', 'FAKE_EXIT', 'FAKE_MISSING_PROFILE'):
            self.env.pop(name, None)
        self.app = self.root / 'app' / '0.2.0-spike.4' / 'project-workbench'
        self.app.mkdir(parents=True)
        (self.app / 'skills' / 'project-workbench').mkdir(parents=True)
        (self.app / connection.SKILL_PATH).write_bytes((PACKAGE / 'project-workbench' / connection.SKILL_PATH).read_bytes())
        (self.app / 'LICENSE').write_bytes((PACKAGE / 'LICENSE').read_bytes())
        manifest = json.loads((PACKAGE / 'project-workbench' / 'plugin.json').read_text())
        manifest['version'] = '0.2.0-preview.2'
        (self.app / 'plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
        for name in ('connection', 'maintenance', 'config', 'state'):
            (self.root / name).mkdir()
        for name in ('connect_chatgpt.py', 'support.py', 'launch.py', 'tunnel_safety.py', 'test_tunnel_init.py'):
            shutil.copy2(PACKAGE / 'tools' / name, self.root / 'maintenance' / name)
        self.script = self.root / 'maintenance' / 'connect_chatgpt.py'
        argv = mcp_stdio_argv(self.root, sys.executable)
        (self.root / 'connection' / 'mcp-command.txt').write_text(mcp_command_text(self.root, sys.executable) + '\n')
        (self.root / 'connection' / 'stdio.json').write_text(json.dumps({'mcpServers': {'project-workbench':
            {'type': 'stdio', 'command': argv[0], 'args': argv[1:], 'cwd': str(self.root)}}}))
        self.keyfile = self.base / 'private-runtime-key.txt'
        self.keyfile.write_text(RUNTIME_SECRET)
        cfg = {'data_dir': str(self.root / 'state'), 'auth_token': LOCAL_SECRET,
               'port': 8765, 'projects': []}
        (self.root / 'config' / 'config.json').write_text(json.dumps(cfg))
        # This is an independent fixture receipt, not an installer success
        # claim. It avoids touching the candidate's mutable build manifest.
        self.receipt = {'schema': 'workbench.clean-install.v1', 'status': 'installed',
                        'package_version': '0.2.0-preview.2', 'runtime_version': '0.2.0-spike.4',
                        'app_relative': 'app/0.2.0-spike.4/project-workbench',
                        'python': sys.executable, 'installed_files': []}
        self.rehash_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def rehash_fixture(self):
        self.receipt['installed_files'] = [
            {'path': p.relative_to(self.root).as_posix(), 'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for directory in (self.root / 'maintenance', self.app)
            for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts
        ]
        (self.root / 'INSTALLATION.json').write_text(json.dumps(self.receipt), encoding='utf-8')

    def call(self, *args, env=None):
        return subprocess.run([sys.executable, str(self.script), '--root', str(self.root), *map(str, args)],
                              cwd=self.base, env=env or self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding='utf-8', timeout=15)

    def configure(self, **changes):
        args = ['configure', '--tunnel-id', changes.get('tunnel_id', TUNNEL_A),
                '--tunnel-client', changes.get('client', self.client)]
        if changes.get('credential', True):
            args += ['--credential-file', self.keyfile]
        result = self.call(*args, env=changes.get('env'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def records(self):
        if not self.capture.exists():
            return []
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def binding(self):
        return json.loads((self.root / 'connection' / 'chatgpt.json').read_text())

    def generated(self):
        return self.root / 'connection' / 'generated' / 'Project-Relay-Plugin-v0.2.0-preview.2.zip'

    def test_configure_passes_exact_argv_with_unicode_and_spaces_no_shell(self):
        self.configure()
        calls = self.records()
        self.assertEqual(len(calls), 1)
        args = calls[0]['argv']
        self.assertEqual(args[:3], ['init', '--sample', 'sample_mcp_stdio_local'])
        self.assertEqual(args[args.index('--tunnel-id') + 1], TUNNEL_A)
        self.assertEqual(args[args.index('--mcp-command') + 1],
                         (self.root / 'connection' / 'mcp-command.txt').read_text().strip())
        self.assertEqual(calls[0]['profile_dir'], str(self.root / 'connection' / 'tunnel-profiles'))
        self.assertEqual((self.root / 'Start-Relay.ps1').read_bytes(), connection.START_SCRIPT)
        self.assertEqual(self.binding()['host_validation'], 'pending')

    def test_configure_rerun_is_stable_and_does_not_reinitialize(self):
        self.configure()
        before = (self.root / 'connection' / 'chatgpt.json').read_bytes()
        repeated = self.configure()
        self.assertEqual(json.loads(repeated.stdout)['status'], 'unchanged')
        self.assertEqual(len(self.records()), 1)
        self.assertEqual((self.root / 'connection' / 'chatgpt.json').read_bytes(), before)

    def test_different_tunnel_or_executable_cannot_rebind(self):
        self.configure()
        different = self.call('configure', '--tunnel-id', TUNNEL_B, '--tunnel-client', self.client)
        self.assertNotEqual(different.returncode, 0)
        other = self.base / 'other-client'
        shutil.copy2(self.client, other)
        different = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', other)
        self.assertNotEqual(different.returncode, 0)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.binding()['tunnel_id'], TUNNEL_A)

    def test_existing_profile_directory_is_not_overwritten(self):
        profiles = self.root / 'connection' / 'tunnel-profiles'
        profiles.mkdir()
        marker = profiles / 'other-person.yaml'
        marker.write_text('preserve this profile')
        result = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', self.client)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), 'preserve this profile')
        self.assertEqual(self.records(), [])

    def test_init_failure_retains_partial_files_and_never_claims_success(self):
        result = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', self.client,
                           env={**self.env, 'FAKE_INIT_EXIT': '7'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'connection' / 'chatgpt.json').exists())
        self.assertFalse((self.root / 'Start-Relay.ps1').exists())
        self.assertIn('exit 7', result.stderr)
        self.assertEqual(len(list((self.root / 'connection' / 'tunnel-profiles').glob('*.yaml'))), 1)
        self.assertFalse((self.root / 'connection' / 'setup.lock').exists())

    def test_zero_exit_without_profile_does_not_claim_success(self):
        result = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', self.client,
                           env={**self.env, 'FAKE_MISSING_PROFILE': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'connection' / 'chatgpt.json').exists())

    def test_modified_profile_is_rejected_before_start(self):
        self.configure()
        binding = self.binding()
        profile = self.root / 'connection' / 'tunnel-profiles' / (binding['profile'] + '.yaml')
        with profile.open('a') as stream:
            stream.write('# operator edit\n')
        result = self.call('run', env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.records()), 1)

    def test_tampered_command_cannot_start_another_program(self):
        (self.root / 'connection' / 'mcp-command.txt').write_text('a-different-program')
        result = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', self.client)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records(), [])

    def test_credentials_never_reach_init_or_helper_output_or_connection_files(self):
        env = {**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET, 'OPENAI_ADMIN_KEY': 'ADMIN_SECRET_SENTINEL',
               'MCP_COMMAND': 'wrong-target', 'CONTROL_PLANE_TUNNEL_ID': TUNNEL_B}
        configured = self.configure(env=env)
        for mode in ('doctor', 'run'):
            result = self.call(mode, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(RUNTIME_SECRET, result.stdout + result.stderr)
            self.assertNotIn(LOCAL_SECRET, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)['host_validation'], 'pending')
        self.assertNotIn(RUNTIME_SECRET, configured.stdout + configured.stderr)
        calls = self.records()
        self.assertFalse(calls[0]['key_present'])
        for item in calls[1:]:
            self.assertTrue(item['key_present'])
        for item in calls:
            self.assertFalse(item['admin_key_present'])
            self.assertFalse(item['inherited_mcp_present'])
            self.assertFalse(item['inherited_tunnel_id_present'])
        for path in (self.root / 'connection').rglob('*'):
            if path.is_file():
                self.assertNotIn(RUNTIME_SECRET.encode(), path.read_bytes())
                self.assertNotIn(LOCAL_SECRET.encode(), path.read_bytes())

    def test_nonzero_doctor_and_run_propagate_failure_and_release_lock(self):
        self.configure()
        for mode in ('doctor', 'run'):
            with self.subTest(mode=mode):
                result = self.call(mode, env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET, 'FAKE_EXIT': '9'})
                self.assertEqual(result.returncode, 9)
                self.assertEqual(json.loads(result.stdout)['status'], 'failed')
                self.assertNotIn(RUNTIME_SECRET, result.stdout + result.stderr)
                self.assertFalse((self.root / 'connection' / 'tunnel-process.lock').exists())

    def test_missing_key_never_launches_external_client(self):
        self.configure(credential=False)
        result = self.call('doctor', env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Inherited CONTROL_PLANE_API_KEY is ignored', result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_existing_facade_or_process_lock_prevents_second_client(self):
        self.configure()
        for lock in (self.root / 'state' / 'facade.lock', self.root / 'connection' / 'tunnel-process.lock'):
            with self.subTest(lock=lock.name):
                lock.write_text('{"pid":123}')
                result = self.call('run', env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET})
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(lock.exists())
                lock.unlink()
        self.assertEqual(len(self.records()), 1)

    def test_package_is_minimal_personalized_deterministic_and_preserves_skill(self):
        self.configure()
        # An unlisted local secret near the application is never swept up.
        (self.app / 'config.private.json').write_text(LOCAL_SECRET)
        (self.app / 'mcp.json').write_text('{"private":"runtime paths"}')
        first = self.call('package', '--app-id', APP_A)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.generated().read_bytes()
        second = self.call('package', '--app-id', 'https://chatgpt.com/apps/plugin_' + APP_A)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['status'], 'unchanged')
        self.assertEqual(before, self.generated().read_bytes())
        self.assertEqual(json.loads(first.stdout)['sha256'], hashlib.sha256(before).hexdigest())
        with zipfile.ZipFile(io.BytesIO(before)) as archive:
            self.assertEqual(set(archive.namelist()), {'plugin.json', '.app.json', connection.SKILL_PATH, 'LICENSE'})
            self.assertEqual(archive.read('LICENSE'), (PACKAGE / 'LICENSE').read_bytes())
            self.assertEqual(archive.read(connection.SKILL_PATH), (self.app / connection.SKILL_PATH).read_bytes())
            manifest = json.loads(archive.read('plugin.json'))
            self.assertEqual(manifest['name'], 'project-workbench')
            self.assertEqual(manifest['version'], '0.2.0-preview.2')
            self.assertEqual(manifest['extensions']['com.openai']['apps'], './.app.json')
            apps = json.loads(archive.read('.app.json'))
            self.assertEqual(apps, {'apps': {'workbench-mcp': {'id': APP_A, 'required': True}}})
            for name in archive.namelist():
                self.assertNotIn(LOCAL_SECRET.encode(), archive.read(name))
                self.assertNotIn(RUNTIME_SECRET.encode(), archive.read(name))
                self.assertNotIn(str(self.root).encode(), archive.read(name))
                self.assertNotIn(TUNNEL_A.encode(), archive.read(name))
        receipt = json.loads((self.root / 'connection' / 'plugin-binding.json').read_text())
        self.assertEqual(receipt['identity_validation'], 'format_only')
        self.assertEqual(receipt['host_validation'], 'pending')
        self.assertEqual(receipt['chatgpt_import'], 'not_performed_by_helper')

    def test_package_rebind_and_archive_collision_require_explicit_replace(self):
        self.configure()
        self.assertEqual(self.call('package', '--app-id', APP_A).returncode, 0)
        before = self.generated().read_bytes()
        result = self.call('package', '--app-id', APP_B)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.generated().read_bytes(), before)
        result = self.call('package', '--app-id', APP_B, '--replace')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.generated().read_bytes(), before)
        self.generated().write_bytes(b'other local data')
        result = self.call('package', '--app-id', APP_B)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.generated().read_bytes(), b'other local data')

    def test_package_can_be_generated_while_runtime_is_running(self):
        self.configure()
        (self.root / 'state' / 'facade.lock').write_text('{"pid":123}')
        result = self.call('package', '--app-id', APP_A)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_symlink_destination_is_rejected(self):
        self.configure()
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'connection' / 'generated').symlink_to(outside, target_is_directory=True)
        result = self.call('package', '--app-id', APP_A)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_literal_key_in_profile_is_rejected(self):
        self.configure()
        profile = self.root / 'connection' / 'tunnel-profiles' / (self.binding()['profile'] + '.yaml')
        profile.write_text('control_plane:\n  api_key: ' + RUNTIME_SECRET + '\n')
        result = self.call('doctor', env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(RUNTIME_SECRET, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)

    def test_invalid_tunnel_id_and_shell_wrapper_are_rejected(self):
        for ident in (TUNNEL_A + ';echo', 'tunnel_wrong', TUNNEL_A.upper()):
            with self.subTest(ident=ident):
                result = self.call('configure', '--tunnel-id', ident, '--tunnel-client', self.client)
                self.assertNotEqual(result.returncode, 0)
        wrapper = self.base / 'wrapper.cmd'
        wrapper.write_text('echo bad')
        result = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', wrapper)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records(), [])

    def test_file_key_overrides_wrong_ambient_key_and_rotates(self):
        self.configure()
        before = (self.root / 'connection' / 'chatgpt.json').read_bytes()
        self.assertEqual(self.call('package', '--app-id', APP_A).returncode, 0)
        plugin = self.generated().read_bytes()
        wrong = 'WRONG_AMBIENT_BRIDGE_TEST_1234567890'
        result = self.call('doctor', env={**self.env, 'CONTROL_PLANE_API_KEY': wrong})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]['key_sha256'], hashlib.sha256(RUNTIME_SECRET.encode()).hexdigest())
        rotated = RUNTIME_SECRET + '_ROTATED'
        self.keyfile.write_text(rotated)
        result = self.call('doctor', env={**self.env, 'CONTROL_PLANE_API_KEY': wrong})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[-1]['key_sha256'], hashlib.sha256(rotated.encode()).hexdigest())
        self.assertEqual(before, (self.root / 'connection' / 'chatgpt.json').read_bytes())
        self.assertEqual(plugin, self.generated().read_bytes())
        self.assertNotIn(wrong, result.stdout + result.stderr)
        self.assertNotIn(rotated, result.stdout + result.stderr)

    def test_missing_explicit_file_does_not_launch_or_fall_back_to_ambient(self):
        self.configure()
        self.keyfile.unlink()
        result = self.call('doctor', env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.records()), 1)
        # Package generation itself needs no runtime key/file content.
        self.assertEqual(self.call('package', '--app-id', APP_A).returncode, 0)

    def test_legacy_client_on_another_health_port_blocks_run_but_allows_doctor(self):
        self.configure()
        legacy = self.base / 'old binary directory' / 'tunnel-client'
        legacy.parent.mkdir()
        shutil.copy2(self.client, legacy)
        child = subprocess.Popen([str(legacy), 'run', '--profile', 'old-profile',
                                  '--health.listen-addr', '127.0.0.1:8091'],
                                 env={**self.env, 'FAKE_STAY_RUNNING': '1'},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            while len(self.records()) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(self.records()), 2)
            # /proc can expose host PIDs while Popen reports namespace PIDs.
            # Compare the process inspection results used by the launcher.
            observed_pids = {row['pid'] for row in inspect_clients(str(self.client), TUNNEL_A)}
            self.assertTrue(observed_pids)
            blocked = self.call('run')
            self.assertNotEqual(blocked.returncode, 0)
            for pid in observed_pids:
                self.assertIn(str(pid), blocked.stderr)
            self.assertEqual(len(self.records()), 2)
            self.assertIsNone(child.poll())
            checked = self.call('doctor')
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(observed_pids, set(json.loads(checked.stdout)['local_client_check']['pids']))
        finally:
            child.kill(); child.wait(timeout=10)
        self.assertEqual(self.call('run').returncode, 0)

    def test_tampered_stdio_mapping_is_rejected_before_init(self):
        (self.root / 'connection' / 'stdio.json').write_text('{"mcpServers":{}}')
        result = self.call('configure', '--tunnel-id', TUNNEL_A, '--tunnel-client', self.client)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records(), [])

    def test_credential_free_probe_exercises_three_path_cases_without_touching_binding(self):
        # This fixture verifies orchestration only; Windows runs the same tool
        # with the actual full tunnel-client binary for the acceptance receipt.
        result = subprocess.run([sys.executable, str(self.root / 'maintenance/test_tunnel_init.py'),
                                '--root', str(self.root), '--tunnel-client', str(self.client)],
                                env={**self.env, 'CONTROL_PLANE_API_KEY': RUNTIME_SECRET},
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(len(report['cases']), 3)
        self.assertFalse((self.root / 'connection/chatgpt.json').exists())
        self.assertTrue(all(not row['key_present'] for row in self.records()))
        self.assertNotIn(RUNTIME_SECRET, result.stdout + result.stderr)

    def test_preflight_reads_no_key_and_starts_no_client(self):
        self.configure()
        self.keyfile.unlink()
        result = self.call('preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['credential_read'], False)
        self.assertEqual(json.loads(result.stdout)['client_started'], False)
        self.assertEqual(len(self.records()), 1)


if __name__ == '__main__':
    unittest.main()
