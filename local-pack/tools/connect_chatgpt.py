"""Configure a private OpenAI tunnel and package a user's registered ChatGPT app.

This helper does not create an OpenAI tunnel, register an app, import a plugin,
or claim that ChatGPT has connected. Credentials are supplied at process start.
"""
import argparse
from contextlib import contextmanager, nullcontext
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import zipfile

from support import installation, mcp_command_text, mcp_stdio_argv, plain_path, prepare_shutdown_signals, read_json, require_idle, sha
from tunnel_safety import credential_file, explicit_credential, inspect_clients, refuse_active_clients, tunnel_guard


SCHEMA = 'project-relay.chatgpt-connection.v2'
APP_PATTERN = re.compile(r'(?:plugin_)?(asdk_app_[0-9a-f]{32})\Z')
TUNNEL_PATTERN = re.compile(r'tunnel_[0-9a-f]{32}\Z')
PROFILE_PATTERN = re.compile(r'project-relay-[0-9a-f]{32}\Z')
SKILL_PATH = 'skills/project-workbench/SKILL.md'
START_SCRIPT = (
    "$ErrorActionPreference = 'Stop'\n"
    "$RelayReceipt = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'INSTALLATION.json') -Raw -Encoding UTF8 | ConvertFrom-Json\n"
    "& $RelayReceipt.python (Join-Path $PSScriptRoot 'maintenance/connect_chatgpt.py') --root $PSScriptRoot run\n"
    "exit $LASTEXITCODE\n"
).encode('utf-8-sig')


def encoded_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


def atomic_write(path, data):
    """Only called after collision checks while holding this helper's lock."""
    path = plain_path(path)
    fd, name = tempfile.mkstemp(prefix='.relay-write-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def exclusive(path):
    path = plain_path(path)
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump({'pid': os.getpid()}, stream)
    except FileExistsError:
        raise ValueError('A connection operation is already active. After a crash, inspect its recorded PID before removing the connection lock') from None
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def app_id(value):
    """Accept an explicit app ID or an unambiguous ChatGPT app detail URL."""
    value = value.strip()
    match = APP_PATTERN.fullmatch(value)
    if match:
        return match.group(1)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname != 'chatgpt.com'
                or parsed.username or parsed.password or parsed.port
                or parsed.query or parsed.fragment or '%' in parsed.path
                or not parsed.path.startswith(('/apps/', '/plugins/'))):
            raise ValueError
        matches = [APP_PATTERN.fullmatch(part) for part in parsed.path.split('/')]
        ids = [m.group(1) for m in matches if m]
        if len(ids) == 1:
            return ids[0]
    except ValueError:
        pass
    raise ValueError('Supply the registered asdk_app_ ID, plugin_asdk_app_ ID, or its https://chatgpt.com/apps/ detail URL; plugin aliases are not app IDs')


def executable(value):
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        candidate = plain_path(candidate)
    elif len(candidate.parts) == 1:
        found = shutil.which(value)
        if not found:
            raise ValueError('Install the full tunnel-client binary and supply its absolute path or put it on PATH')
        candidate = plain_path(found)
    else:
        raise ValueError('Supply an absolute tunnel-client executable path or a command on PATH')
    if not candidate.is_file() or candidate.suffix.lower() in ('.cmd', '.bat', '.ps1'):
        raise ValueError('Supply the full tunnel-client executable, not a shell wrapper')
    if os.name == 'nt' and candidate.suffix.lower() != '.exe':
        raise ValueError('Windows requires the full tunnel-client.exe binary')
    if os.name != 'nt' and not os.access(candidate, os.X_OK):
        raise ValueError('The tunnel-client executable is not executable')
    return str(candidate)


def mcp_command(root, receipt):
    argv = mcp_stdio_argv(root, receipt['python'])
    expected = mcp_command_text(root, receipt['python'])
    supplied = plain_path(root / 'connection' / 'mcp-command.txt').read_text(encoding='utf-8').strip()
    if supplied != expected:
        raise ValueError('The generated MCP command differs from this installation; restore it before connecting')
    stdio = read_json(plain_path(root / 'connection' / 'stdio.json'))
    if stdio != {'mcpServers': {'project-workbench': {'type': 'stdio', 'command': argv[0],
                'args': argv[1:], 'cwd': str(root)}}}:
        raise ValueError('The stdio argv mapping differs from this installation')
    return expected


def child_environment(profile_dir, credential=None):
    # Ignore inherited tunnel/MCP overrides so another installation cannot
    # redirect this profile. Proxy/CA variables retain ordinary OS behavior.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(('CONTROL_PLANE_', 'MCP_', 'TUNNEL_CLIENT_'))
           and key.upper() not in ('OPENAI_API_KEY', 'OPENAI_ADMIN_KEY')}
    env['TUNNEL_CLIENT_PROFILE_DIR'] = str(profile_dir)
    if credential:
        env['CONTROL_PLANE_API_KEY'] = credential
    return env


def runtime_credential(source=None):
    return explicit_credential(source or {'type': 'prompt'})


def profile_file(root, binding):
    profile = binding.get('profile', '')
    if not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError('Invalid saved tunnel profile identity')
    return plain_path(root / 'connection' / 'tunnel-profiles' / (profile + '.yaml'))


def validate_profile(path):
    text = path.read_text(encoding='utf-8-sig')
    # The official init sample should retain an environment reference. Refuse
    # literal credentials rather than trying to sanitize and continue.
    keys = re.findall(r'^\s*api_key\s*:\s*(.*?)\s*$', text, re.MULTILINE)
    if len(keys) != 1 or keys[0].strip('"\'') != 'env:CONTROL_PLANE_API_KEY':
        raise ValueError('Tunnel profile must reference env:CONTROL_PLANE_API_KEY; no successful binding was recorded')


def load_binding(root, receipt):
    binding = read_json(plain_path(root / 'connection' / 'chatgpt.json'))
    if (binding.get('schema') != SCHEMA or binding.get('status') != 'configured'
            or binding.get('package_version') != receipt['package_version']
            or binding.get('runtime_version') != receipt['runtime_version']
            or not TUNNEL_PATTERN.fullmatch(binding.get('tunnel_id', ''))):
        raise ValueError('Missing or incompatible ChatGPT connection; run configure for this clean installation')
    command = mcp_command(root, receipt)
    if hashlib.sha256(command.encode('utf-8')).hexdigest() != binding.get('mcp_command_sha256'):
        raise ValueError('Saved connection command differs from this installation')
    profile = profile_file(root, binding)
    validate_profile(profile)
    if sha(profile) != binding.get('profile_sha256'):
        raise ValueError('Tunnel profile changed after configuration; review it locally before reuse')
    executable(binding['tunnel_client'])
    if type(binding.get('health_port')) is not int or not 1 <= binding['health_port'] <= 65535:
        raise ValueError('Invalid saved health port')
    source = binding.get('credential_source')
    if not isinstance(source, dict) or source.get('type') not in ('prompt', 'file'):
        raise ValueError('Invalid saved credential source')
    if source['type'] == 'file' and (not isinstance(source.get('path'), str) or not Path(source['path']).is_absolute()):
        raise ValueError('Invalid credential file reference')
    return binding


def print_result(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def configure(root, receipt, args, cfg):
    if not TUNNEL_PATTERN.fullmatch(args.tunnel_id):
        raise ValueError('Supply tunnel_ followed by 32 lowercase hexadecimal characters from OpenAI Platform')
    client = executable(args.tunnel_client)
    source = {'type': 'prompt'}
    if args.credential_file:
        path = credential_file(args.credential_file)
        if any(path.is_relative_to(Path(p['root']).resolve()) for p in cfg['projects']):
            raise ValueError('Keep the tunnel credential file outside registered project roots')
        source = {'type': 'file', 'path': str(path)}
    health_port = args.health_port if args.health_port is not None else 8080
    if not 1 <= health_port <= 65535:
        raise ValueError('Health port must be between 1 and 65535')
    command = mcp_command(root, receipt)
    connection = plain_path(root / 'connection')
    require_idle(root)
    with exclusive(connection / 'setup.lock'):
        saved = plain_path(connection / 'chatgpt.json')
        if saved.exists():
            binding = load_binding(root, receipt)
            if binding['tunnel_id'] != args.tunnel_id or binding['tunnel_client'] != client:
                raise ValueError('This installation is already bound to a different tunnel or client path; configure a separate clean installation rather than overwrite it')
            if ((args.credential_file and binding['credential_source'] != source)
                    or (args.health_port is not None and binding['health_port'] != health_port)):
                raise ValueError('This installation is already bound to another credential source or health port; use a fresh installation')
            if plain_path(root / 'Start-Relay.ps1').read_bytes() != START_SCRIPT:
                raise ValueError('Start-Relay.ps1 differs from the generated launcher')
            print_result({'status': 'unchanged', 'profile': binding['profile'],
                          'tunnel_id': binding['tunnel_id'], 'host_validation': 'pending'})
            return 0
        profile_dir = plain_path(connection / 'tunnel-profiles')
        if profile_dir.exists() and any(profile_dir.iterdir()):
            raise ValueError('The tunnel profile directory is not empty; existing or partial setup is preserved for local review')
        start = plain_path(root / 'Start-Relay.ps1')
        if start.exists() and start.read_bytes() != START_SCRIPT:
            raise ValueError('Start-Relay.ps1 already exists with different content; it was preserved')
        profile_dir.mkdir(exist_ok=True)
        if os.name != 'nt':
            profile_dir.chmod(0o700)
        profile = 'project-relay-' + secrets.token_hex(16)
        argv = [client, 'init', '--sample', 'sample_mcp_stdio_local', '--profile', profile,
                '--tunnel-id', args.tunnel_id, '--mcp-command', command,
                '--health-listen-addr', '127.0.0.1:' + str(health_port)]
        # No runtime key is passed to init. External output is not replayed into
        # receipts or logs, even on failure.
        result = subprocess.run(argv, cwd=root, env=child_environment(profile_dir),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise ValueError('tunnel-client init failed (exit %d); any partial profile is preserved, and no successful binding was recorded' % result.returncode)
        binding = {'schema': SCHEMA, 'status': 'configured',
                   'package_version': receipt['package_version'],
                   'runtime_version': receipt['runtime_version'],
                   'transport': 'openai-secure-tunnel-stdio',
                   'tunnel_id': args.tunnel_id, 'tunnel_client': client, 'profile': profile,
                   'credential_source': source, 'health_port': health_port,
                   'mcp_command_sha256': hashlib.sha256(command.encode('utf-8')).hexdigest(),
                   'host_validation': 'pending'}
        path = profile_file(root, binding)
        validate_profile(path)
        binding['profile_sha256'] = sha(path)
        atomic_write(start, START_SCRIPT)
        atomic_write(saved, encoded_json(binding))
        print_result({'status': 'configured', 'tunnel_id': args.tunnel_id, 'profile': profile,
                      'launcher': str(start), 'host_validation': 'pending',
                      'credential_source': source['type'], 'health_url': 'http://127.0.0.1:' + str(health_port),
                      'next_step': 'Run doctor, then Start-Relay.ps1. While it is running, register a ChatGPT developer-mode app with this tunnel.'})
        return 0


def stop_child(child):
    if child.poll() is not None:
        return
    try:
        if os.name == 'nt':
            child.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def run_client(root, receipt, mode):
    binding = load_binding(root, receipt)
    require_idle(root)
    profile_dir = root / 'connection' / 'tunnel-profiles'
    argv = [binding['tunnel_client'], mode, '--profile', binding['profile']]
    if mode == 'doctor':
        argv.append('--explain')
    else:
        argv += ['--health.listen-addr', '127.0.0.1:' + str(binding['health_port'])]
    # Shared lock serializes doctor/run because a future doctor implementation
    # may inspect the stdio target. The runtime facade has its own second lock.
    guard = tunnel_guard(binding['tunnel_id']) if mode == 'run' else nullcontext()
    with guard, exclusive(root / 'connection' / 'tunnel-process.lock') as lock:
        require_idle(root)
        observation = {'status': 'clear', 'pids': []}
        if mode == 'run':
            refuse_active_clients(binding['tunnel_client'], binding['tunnel_id'])
        else:
            try:
                blockers = inspect_clients(binding['tunnel_client'], binding['tunnel_id'])
                if blockers:
                    observation = {'status': 'existing_or_unconfirmed_clients', 'pids': [x['pid'] for x in blockers]}
            except ValueError:
                observation = {'status': 'inspection_inconclusive', 'pids': []}
        credential = runtime_credential(binding['credential_source'])
        kwargs = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
        child = subprocess.Popen(argv, cwd=root, env=child_environment(profile_dir, credential),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **kwargs)
        interrupted = False
        try:
            atomic_write(lock, encoded_json({'pid': os.getpid(), 'child_pid': child.pid, 'mode': mode}))
            if mode == 'run':
                print('Project Relay tunnel process started. Keep this terminal open. Readiness and ChatGPT access still require verification; press Ctrl+C to stop.', file=sys.stderr, flush=True)
            code = child.wait(timeout=60 if mode == 'doctor' else None)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            interrupted = True
            stop_child(child)
            code = 130 if mode == 'run' else 124
        finally:
            stop_child(child)
        print_result({'status': ('stopped' if mode == 'run' else 'doctor_passed') if code == 0 else ('interrupted' if interrupted else 'failed'),
                      'operation': mode, 'exit_code': code, 'host_validation': 'pending',
                      'local_client_check': observation})
        if code:
            print('The external client did not complete successfully. Its raw output is suppressed to avoid exposing credentials. Use the official local client diagnostics when investigating.', file=sys.stderr)
        return code if 0 <= code <= 255 else 1


def plugin_bytes(app, receipt, ident):
    source = read_json(plain_path(app / 'plugin.json'))
    version = receipt['package_version']
    if (source.get('name') != 'project-workbench' or source.get('version') != version
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[a-z0-9.]+', version)):
        raise ValueError('Plugin and Local Runtime package versions must match')
    # Explicit allowlist: runtime paths, mcp.json and unexpected extension
    # settings can never be swept into a personalized plugin.
    manifest = {key: source[key] for key in ('$schema', 'name', 'version', 'description', 'author') if key in source}
    interface = source['extensions']['com.openai']['interface']
    if interface.get('displayName') != 'Project Relay':
        raise ValueError('Unexpected installed plugin display identity')
    manifest['extensions'] = {'com.openai': {'interface': interface, 'apps': './.app.json'}}
    members = {'plugin.json': encoded_json(manifest),
               '.app.json': encoded_json({'apps': {'workbench-mcp': {'id': ident, 'required': True}}}),
               'LICENSE': plain_path(app / 'LICENSE').read_bytes(),
               SKILL_PATH: plain_path(app / SKILL_PATH).read_bytes()}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, data, compresslevel=9)
    return stream.getvalue()


def package(root, receipt, app, args):
    binding = load_binding(root, receipt)
    ident = app_id(args.app_id)
    data = plugin_bytes(app, receipt, ident)
    digest = hashlib.sha256(data).hexdigest()
    connection = root / 'connection'
    with exclusive(connection / 'setup.lock'):
        generated = plain_path(connection / 'generated')
        generated.mkdir(exist_ok=True)
        output = plain_path(generated / ('Project-Relay-Plugin-v' + receipt['package_version'] + '.zip'))
        record_path = plain_path(connection / 'plugin-binding.json')
        if record_path.exists():
            old = read_json(record_path)
            if old.get('schema') != 'project-relay.plugin-binding.v1':
                raise ValueError('An unrecognized plugin binding receipt exists; it was preserved')
            if old.get('app_id') != ident and not args.replace:
                raise ValueError('A different app is already recorded; use --replace only after reviewing the new registered app')
        same = output.exists() and output.read_bytes() == data
        if output.exists() and not same and not args.replace:
            raise ValueError('A different generated ZIP already exists; use --replace to replace that local package explicitly')
        record = {'schema': 'project-relay.plugin-binding.v1', 'app_id': ident,
                  'tunnel_id': binding['tunnel_id'], 'package_version': receipt['package_version'],
                  'filename': output.name, 'sha256': digest,
                  'identity_validation': 'format_only', 'host_validation': 'pending',
                  'chatgpt_import': 'not_performed_by_helper'}
        if not same:
            atomic_write(output, data)
        atomic_write(record_path, encoded_json(record))
        print_result({'status': 'unchanged' if same else 'generated', 'package': str(output),
                      'sha256': digest, 'app_id': ident, 'identity_validation': 'format_only',
                      'host_validation': 'pending',
                      'next_step': 'Import this private package with Plugin Creator, then verify the registered app points to this tunnel and run the README smoke test.'})
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('configure', help='Initialize a private stdio profile for an existing OpenAI tunnel')
    prepare.add_argument('--tunnel-id', required=True)
    prepare.add_argument('--tunnel-client', default='tunnel-client')
    prepare.add_argument('--credential-file', type=Path, help='Explicit existing local key file; store only its path. Otherwise use hidden prompts and ignore inherited API keys')
    prepare.add_argument('--health-port', type=int, help='Loopback health port, default 8080; independent of tunnel ownership checks')
    commands.add_parser('doctor', help='Run official tunnel-client configuration diagnostics; requires a runtime API key')
    commands.add_parser('run', help='Run the tunnel and its stdio runtime in the foreground; requires a runtime API key')
    commands.add_parser('preflight', help='Check local single-client ownership without keys or starting a client; expected to block while the old client runs')
    make = commands.add_parser('package', help='Generate a private plugin ZIP for an already registered ChatGPT app')
    make.add_argument('--app-id', required=True)
    make.add_argument('--replace', action='store_true', help='Explicitly replace an existing local generated ZIP/app binding')
    args = parser.parse_args(argv)
    try:
        root, receipt, app, _config, _cfg = installation(args.root)
        if args.command == 'configure':
            return configure(root, receipt, args, _cfg)
        if args.command == 'package':
            return package(root, receipt, app, args)
        if args.command == 'preflight':
            binding = load_binding(root, receipt)
            require_idle(root)
            with tunnel_guard(binding['tunnel_id']):
                refuse_active_clients(binding['tunnel_client'], binding['tunnel_id'])
            print_result({'status': 'preflight_passed', 'client_started': False,
                          'credential_read': False, 'host_validation': 'pending'})
            return 0
        return run_client(root, receipt, args.command)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        # Never format subprocess exceptions, argv, environment or raw output.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
        print('Connection helper failed: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    stopping = False
    def interrupt(*_):
        global stopping
        if not stopping:
            stopping = True
            raise KeyboardInterrupt
    try:
        prepare_shutdown_signals(interrupt)
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
