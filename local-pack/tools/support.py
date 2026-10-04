"""Shared installation checks. Never print configuration values or credentials."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import subprocess
import sys

# MCP and installation receipts have a stable UTF-8 pipe encoding on Windows too.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='strict')


def prepare_shutdown_signals(handler):
    """Route console shutdown through Python cleanup, including Windows Break.

    A parent or CREATE_NEW_PROCESS_GROUP can disable Ctrl+C independently of
    Python's SIGINT handler. Restore that inherited attribute in this process.
    Headless stdio processes have no console to restore (Win32 error 6).
    """
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        signal.signal(signal.SIGBREAK, handler)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        reset = kernel.SetConsoleCtrlHandler
        reset.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        reset.restype = wintypes.BOOL
        if not reset(None, False):
            error = ctypes.get_last_error()
            if error != 6:
                raise ctypes.WinError(error)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value, private=False):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    if private and os.name != 'nt':
        path.chmod(0o600)


def plain_path(path):
    """Reject links/junctions in existing ancestors before installation writes."""
    path = Path(path).expanduser().absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            st = item.lstat()
            if item.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Installation/configuration paths must not pass through a link or junction')
    return path.resolve()


def check_entries(root, entries):
    root = Path(root).resolve()
    seen = set()
    for item in entries:
        rel = item['path']
        if not isinstance(rel, str) or not rel or '\\' in rel or ':' in rel:
            raise ValueError('Invalid manifest path')
        parts = Path(rel).parts
        if Path(rel).is_absolute() or '..' in parts or rel in seen:
            raise ValueError('Unsafe or duplicate manifest path')
        seen.add(rel)
        p = root.joinpath(*parts)
        if not p.resolve().is_relative_to(root) or p.is_symlink() or not p.is_file():
            raise ValueError('Missing or escaping package file: ' + rel)
        plain_path(p)
        if p.stat().st_size != item['bytes'] or sha(p) != item['sha256']:
            raise ValueError('File integrity mismatch: ' + rel)


def installation(root):
    root = plain_path(root)
    receipt = read_json(root / 'INSTALLATION.json')
    if receipt.get('status') != 'installed' or receipt.get('schema') != 'workbench.clean-install.v1':
        raise ValueError('Installation is incomplete or has an unsupported receipt')
    check_entries(root, receipt['installed_files'])
    if receipt['app_relative'] != f"app/{receipt['runtime_version']}/project-workbench" or '/' in receipt['runtime_version'] or '\\' in receipt['runtime_version']:
        raise ValueError('Invalid installed application path')
    app = root / receipt['app_relative']
    config_path = root / 'config' / 'config.json'
    cfg = read_json(plain_path(config_path))
    state = plain_path(cfg['data_dir'])
    if state != root / 'state':
        raise ValueError('Clean installation state must remain in its own state directory')
    if not isinstance(cfg.get('auth_token'), str) or len(cfg['auth_token']) < 32:
        raise ValueError('A strong local token is required')
    if type(cfg.get('port')) is not int or not 1 <= cfg['port'] <= 65535:
        raise ValueError('Invalid HTTP port')
    seen = set()
    roots = set()
    if not isinstance(cfg.get('projects'), list):
        raise ValueError('Projects configuration must be a list')
    for p in cfg.get('projects', []):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', p['id']) or p['id'] in seen:
            raise ValueError('Invalid or duplicate project ID')
        project = plain_path(p['root'])
        key = os.path.normcase(str(project))
        if not project.is_dir() or key in roots:
            raise ValueError('Missing or duplicate project root')
        if state == project or state.is_relative_to(project) or project.is_relative_to(state):
            raise ValueError('Project and state directories must be separate')
        if project == root or app.is_relative_to(project) or project.is_relative_to(app):
            raise ValueError('Do not register the installed program as a work project')
        roots.add(key)
        seen.add(p['id'])
    return root, receipt, app, config_path, cfg


def project_record(path, ident, writable=False):
    path = plain_path(path)
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', ident):
        raise ValueError('Project ID must be a lowercase portable name')
    if not path.is_dir():
        raise ValueError('Project directory does not exist')
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                            capture_output=True, check=True, timeout=15)
    if Path(result.stdout.decode('utf-8').strip()).resolve() != path:
        raise ValueError('Supply the exact Git project root')
    subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True,
                   check=True, timeout=15)
    return {'id': ident, 'name': path.name, 'root': str(path), 'writable': writable,
            'profiles': {'smoke': [sys.executable, '-c',
                         "print('Local CLI started',flush=True);print('Local CLI completed',flush=True)"]}}


def default_root():
    return Path.home() / 'ProjectRelay'


def mcp_stdio_argv(root, python, windows=None):
    """Use portable Windows path tokens before the tunnel's shell tokenizer."""
    if windows is None:
        windows = os.name == 'nt'
    argv = [str(python), str(root).rstrip('/\\') + '/maintenance/launch.py',
            '--root', str(root), '--mode', 'stdio']
    if any(any(ord(c) < 32 for c in arg) for arg in argv):
        raise ValueError('Installation command paths contain control characters')
    if windows:
        argv = [arg.replace('\\', '/') for arg in argv]
    return argv


def mcp_command_text(root, python, windows=None):
    # tunnel-client 0.0.11 uses shell tokenization on every OS. A Windows
    # CreateProcess command string is not an interchangeable representation.
    return shlex.join(mcp_stdio_argv(root, python, windows))


def require_idle(root, for_self_test=False):
    root = Path(root)
    if (root / 'state' / 'facade.lock').exists():
        raise ValueError('Stop this installation facade first')
    db = root / 'state' / 'workbench.sqlite3'
    if not db.exists():
        return
    connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tasks' in tables and connection.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running','cancelling','interrupted')").fetchone()[0]:
            raise ValueError('Active or uncertain tasks must be resolved locally first')
        if for_self_test and 'event_subscriptions' in tables and connection.execute('SELECT COUNT(*) FROM event_subscriptions WHERE active=1').fetchone()[0]:
            raise ValueError('Self-test requires a store without active event subscriptions')
    except sqlite3.Error as exc:
        raise ValueError('Cannot verify idle state from the local database') from exc
    finally:
        connection.close()
