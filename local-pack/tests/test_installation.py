import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

PACKAGE = Path(__file__).resolve().parents[1]


class CleanInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workbench-test-')
        self.base = Path(self.temp.name)
        self.root = self.base / '安裝 root with spaces'

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, script, *args):
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              capture_output=True, text=True, encoding='utf-8', timeout=45)

    def install(self, *args):
        result = self.run_tool(PACKAGE / 'install.py', '--root', self.root, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def check(self):
        return self.run_tool(self.root / 'maintenance' / 'check_install.py', '--root', self.root, '--self-test')

    def git_project(self):
        p = self.base / 'external project'
        p.mkdir()
        subprocess.run(['git', '-C', str(p), 'init', '-b', 'main'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(p), 'config', 'user.name', 'Install Test'], check=True)
        subprocess.run(['git', '-C', str(p), 'config', 'user.email', 'test@example.invalid'], check=True)
        (p / 'input.txt').write_text('preserve me\n')
        subprocess.run(['git', '-C', str(p), 'add', 'input.txt'], check=True)
        subprocess.run(['git', '-C', str(p), 'commit', '-m', 'fixture'], check=True, capture_output=True)
        return p

    def test_fresh_file_only_install_at_unicode_space_path_and_real_stdio(self):
        result = self.install()
        self.assertEqual((self.root / 'LICENSE').read_bytes(), (PACKAGE / 'LICENSE').read_bytes())
        receipt = json.loads((self.root / 'INSTALLATION.json').read_text())
        self.assertEqual((self.root / receipt['app_relative'] / 'LICENSE').read_bytes(), (PACKAGE / 'LICENSE').read_bytes())
        cfg = json.loads((self.root / 'config' / 'config.json').read_text())
        self.assertEqual(cfg['projects'], [])
        self.assertEqual(Path(cfg['data_dir']), self.root / 'state')
        self.assertNotIn(cfg['auth_token'], result.stdout + result.stderr)
        self.assertFalse((self.root / 'state' / 'workbench.sqlite3').exists())
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        data = json.loads(checked.stdout)
        self.assertEqual(data['status'], 'PASS')
        self.assertTrue(data['checks']['mcp_stdio'])
        self.assertGreaterEqual(data['tool_count'], 39)
        self.assertFalse((self.root / 'state' / 'facade.lock').exists())
        self.assertNotIn(cfg['auth_token'], checked.stdout + checked.stderr)

    def test_repeated_install_preserves_existing_config_and_state(self):
        self.install()
        marker = self.root / 'state' / 'user-data.txt'
        marker.write_text('existing data')
        before = (self.root / 'config' / 'config.json').read_bytes()
        result = self.run_tool(PACKAGE / 'install.py', '--root', self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, (self.root / 'config' / 'config.json').read_bytes())
        self.assertEqual(marker.read_text(), 'existing data')

    def test_plan_is_read_only_and_distinct_installations_have_distinct_tokens(self):
        planned = self.run_tool(PACKAGE / 'install.py', '--root', self.root, '--plan')
        self.assertEqual(planned.returncode, 0, planned.stderr)
        self.assertFalse(self.root.exists())
        self.install()
        other = self.base / 'second user install'
        run = self.run_tool(PACKAGE / 'install.py', '--root', other)
        self.assertEqual(run.returncode, 0, run.stderr)
        tokens = [json.loads((p / 'config' / 'config.json').read_text())['auth_token'] for p in (self.root, other)]
        self.assertNotEqual(*tokens)

    def test_demo_git_project_and_stdio(self):
        self.install('--demo')
        cfg = json.loads((self.root / 'config' / 'config.json').read_text())
        self.assertTrue(cfg['projects'][0]['writable'])
        self.assertEqual(Path(cfg['projects'][0]['root']), self.root / 'projects' / 'demo')
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_register_exact_external_project_preserves_input_and_auth(self):
        project = self.git_project()
        before = (project / 'input.txt').read_bytes()
        self.install()
        config = self.root / 'config' / 'config.json'
        token = json.loads(config.read_text())['auth_token']
        script = self.root / 'maintenance' / 'register_project.py'
        registered = self.run_tool(script, '--root', self.root, '--project', project, '--project-id', 'analysis')
        self.assertEqual(registered.returncode, 0, registered.stderr)
        cfg = json.loads(config.read_text())
        self.assertFalse(cfg['projects'][0]['writable'])
        self.assertEqual(cfg['auth_token'], token)
        duplicate = self.run_tool(script, '--root', self.root, '--project', project, '--project-id', 'other')
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(len(json.loads(config.read_text())['projects']), 1)
        self.assertEqual((project / 'input.txt').read_bytes(), before)
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_native_r_needs_explicit_acknowledgement_and_exact_executable(self):
        fake = self.base / 'Rscript.exe'
        fake.write_bytes(b'fixture; never executed')
        result = self.run_tool(PACKAGE / 'install.py', '--root', self.root, '--demo', '--rscript', fake)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.root.exists())
        self.install('--demo', '--rscript', fake, '--ack-native-r')
        cfg = json.loads((self.root / 'config' / 'config.json').read_text())
        self.assertEqual(cfg['projects'][0]['compute_runtime']['backend'], 'native_trusted')

    def test_corrupted_package_is_rejected_before_installation(self):
        copy = self.base / 'corrupt source'
        shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        with (copy / 'project-workbench' / 'workbench' / 'core.py').open('ab') as f:
            f.write(b'\n# corruption\n')
        result = self.run_tool(copy / 'install.py', '--root', self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.root.exists())

    def test_undeclared_package_file_is_rejected_and_not_imported(self):
        copy = self.base / 'extra source'
        shutil.copytree(PACKAGE, copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (copy / 'project-workbench' / 'accidental-secret.txt').write_text('PRIVATE_TEST_SENTINEL')
        result = self.run_tool(copy / 'install.py', '--root', self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('PRIVATE_TEST_SENTINEL', result.stdout + result.stderr)
        self.assertFalse(self.root.exists())

    def test_corrupted_installed_resource_returns_failure(self):
        self.install()
        receipt = json.loads((self.root / 'INSTALLATION.json').read_text())
        resource = self.root / receipt['app_relative'] / 'ui' / 'file-snapshot.html'
        resource.write_bytes(b'bad resource')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['status'], 'FAIL')

    def test_active_subscription_blocks_self_test_without_dispatching(self):
        self.install()
        self.assertEqual(self.check().returncode, 0)
        db = self.root / 'state' / 'workbench.sqlite3'
        with sqlite3.connect(db) as c:
            c.execute('DROP TABLE event_subscriptions')
            c.execute('CREATE TABLE event_subscriptions (active INTEGER)')
            c.execute('INSERT INTO event_subscriptions VALUES (1)')
        result = self.check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('active event subscriptions', result.stdout)
        self.assertFalse((self.root / 'state' / 'facade.lock').exists())

    def test_second_facade_is_rejected_and_normal_close_releases_lock(self):
        self.install()
        argv = [sys.executable, str(self.root / 'maintenance' / 'launch.py'), '--root', str(self.root), '--mode', 'stdio']
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8')
        try:
            p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}) + '\n')
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())['id'], 1)
            second = subprocess.run(argv, input='', capture_output=True, text=True, timeout=10)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('facade lock', second.stderr)
            p.stdin.close()
            p.wait(timeout=10)
            self.assertEqual(p.returncode, 0)
            self.assertFalse((self.root / 'state' / 'facade.lock').exists())
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
            for stream in (p.stdout, p.stderr):
                stream.close()

    def test_portable_plugin_entry_uses_selected_clean_installation(self):
        self.install()
        receipt = json.loads((self.root / 'INSTALLATION.json').read_text())
        entry = self.root / receipt['app_relative'] / 'scripts' / 'plugin_entry.py'
        env = dict(os.environ, PROJECT_WORKBENCH_ROOT=str(self.root))
        run = subprocess.run([sys.executable, str(entry)],
              input=json.dumps({'jsonrpc': '2.0', 'id': 7, 'method': 'ping'}) + '\n',
              capture_output=True, text=True, encoding='utf-8', timeout=20, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['id'], 7)
        self.assertFalse((self.root / 'state' / 'facade.lock').exists())

    def test_unicode_install_receipt_survives_legacy_pipe_encoding(self):
        env = dict(os.environ, PYTHONIOENCODING='ascii')
        run = subprocess.run([sys.executable, str(PACKAGE / 'install.py'), '--root', str(self.root)],
              capture_output=True, text=True, encoding='utf-8', timeout=20, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['root'], str(self.root))

    def test_installed_smoke_and_real_worker_do_not_inherit_tunnel_secrets(self):
        self.install('--demo')
        smoke = self.run_tool(self.root / 'maintenance' / 'smoke_test.py',
                              '--root', self.root, '--project-id', 'sandbox')
        self.assertEqual(smoke.returncode, 0, smoke.stdout + smoke.stderr)
        self.assertEqual(json.loads(smoke.stdout)['status'], 'PASS')
        config = self.root / 'config' / 'config.json'
        cfg = json.loads(config.read_text())
        probe = ("import json,os,zipfile; "
                 "v={'tunnel_present':any(k.upper().startswith('CONTROL_PLANE_') for k in os.environ),"
                 "'admin_present':any(k.upper()=='OPENAI_ADMIN_KEY' for k in os.environ),"
                 "'provider_preserved':os.environ.get('OPENAI_API_KEY')=='PROVIDER_FIXTURE'}; "
                 "print(json.dumps(v)); "
                 "z=zipfile.ZipFile('probe.zip','w'); z.writestr('probe.json',json.dumps(v)); z.close()")
        cfg['projects'][0]['profiles']['env_probe'] = [sys.executable, '-c', probe]
        config.write_text(json.dumps(cfg), encoding='utf-8')
        env = {**os.environ, 'CONTROL_PLANE_API_KEY': 'TUNNEL_FIXTURE_PRIVATE',
               'control_plane_extra': 'TUNNEL_FIXTURE_PRIVATE',
               'OPENAI_ADMIN_KEY': 'ADMIN_FIXTURE_PRIVATE', 'OPENAI_API_KEY': 'PROVIDER_FIXTURE'}

        def call(name, arguments):
            message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': name, 'arguments': arguments}}
            run = subprocess.run([sys.executable, str(self.root / 'maintenance' / 'launch.py'),
                                  '--root', str(self.root), '--mode', 'stdio'],
                                 input=json.dumps(message) + '\n', env=env,
                                 capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn('TUNNEL_FIXTURE_PRIVATE', run.stdout + run.stderr)
            self.assertNotIn('ADMIN_FIXTURE_PRIVATE', run.stdout + run.stderr)
            response = json.loads(run.stdout)['result']
            self.assertFalse(response.get('isError'), response)
            return response['structuredContent']['data']

        task = call('start_command_task', {'project_id': 'sandbox', 'goal': 'Check installed worker environment',
                    'acceptance': ['No tunnel/admin credential in executor; provider credential retained'],
                    'profile': 'env_probe', 'artifacts': ['probe.zip'],
                    'timeout_seconds': 10, 'idempotency_key': 'credential-boundary-probe'})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = call('get_task', {'task_id': task['id']})
            if current['status'] not in ('queued', 'running', 'cancelling'):
                break
            time.sleep(.1)
        self.assertEqual(current['status'], 'completed', current)
        log = call('get_task_log', {'task_id': task['id']})['text']
        self.assertEqual(json.loads(log.strip()), {'tunnel_present': False, 'admin_present': False,
                                                 'provider_preserved': True})
        result = call('get_task_result', {'task_id': task['id']})
        self.assertEqual(result['task']['review_status'], 'pending')
        artifact = result['result']['artifacts'][0]
        chunk = call('read_artifact_bytes', {'task_id': task['id'], 'artifact_id': 0,
                     'expected_sha256': artifact['sha256'], 'offset': 0, 'limit': 65536})
        raw = base64.b64decode(chunk['base64'], validate=True)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), artifact['sha256'])
        for path in (self.root / 'state').rglob('*'):
            if path.is_file() and path.suffix != '.sqlite3':
                data = path.read_bytes()
                self.assertNotIn(b'TUNNEL_FIXTURE_PRIVATE', data)
                self.assertNotIn(b'ADMIN_FIXTURE_PRIVATE', data)


if __name__ == '__main__':
    unittest.main()
