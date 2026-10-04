"""Local tunnel ownership and explicit credential sources; never log secrets."""
import base64
from contextlib import contextmanager
import ctypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess

from support import plain_path

MAX_KEY_BYTES = 16384


def credential_file(value):
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise ValueError('Credential file must be an existing absolute local path')
    if os.name == 'nt':
        text = str(p)
        if text.startswith(('\\\\', '//')) or ':' in text[2:] or not re.match(r'^[A-Za-z]:[\\/]', text):
            raise ValueError('Credential file must not use UNC, devices or alternate streams')
    p = plain_path(p)
    if not p.is_file():
        raise ValueError('Credential file must be an existing regular file')
    return p


def explicit_credential(source):
    """Ignore ambient CONTROL_PLANE_API_KEY; do not fall back on file failure."""
    if source.get('type') == 'prompt':
        if not __import__('sys').stdin.isatty():
            raise ValueError('Interactive runtime-key input required; configure --credential-file for noninteractive startup. Inherited CONTROL_PLANE_API_KEY is ignored')
        value = getpass.getpass('Runtime API key for this tunnel (not saved): ').strip()
    elif source.get('type') == 'file':
        try:
            path = credential_file(source['path'])
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(path, flags), 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('Credential file is not regular')
                raw = stream.read(MAX_KEY_BYTES + 1)
                after = os.fstat(stream.fileno())
            current = credential_file(path).stat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise ValueError('Credential file changed during reading')
            if len(raw) > MAX_KEY_BYTES:
                raise ValueError('Credential file exceeds the bounded read limit')
            value = raw.decode('utf-8-sig').strip()
        except (OSError, UnicodeError, KeyError) as exc:
            raise ValueError('Explicit credential file cannot be read; no ambient fallback') from None
    else:
        raise ValueError('Invalid credential source')
    if not value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError('Runtime key must be a nonempty single value without whitespace/control characters')
    return value


def guard_port(tunnel_id):
    digest = hashlib.sha256(('ProjectRelay tunnel ownership v1:' + tunnel_id).encode()).digest()
    return 42000 + int.from_bytes(digest[:4], 'big') % 19000


@contextmanager
def tunnel_guard(tunnel_id):
    """Held across roots until child exit. A busy port is not proof of owner."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', guard_port(tunnel_id)))
        except OSError:
            raise ValueError('Tunnel ownership guard port occupied; another Relay launcher or local program may own it. Stop the previous client before retrying') from None
        yield
    finally:
        sock.close()


def windows_argv(command):
    if not isinstance(command, str) or not command:
        raise ValueError('Client command line is unavailable')
    from ctypes import wintypes
    count = ctypes.c_int()
    split = ctypes.windll.shell32.CommandLineToArgvW
    split.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    split.restype = ctypes.POINTER(wintypes.LPWSTR)
    parts = split(command, ctypes.byref(count))
    if not parts:
        raise ValueError('Client command line cannot be parsed')
    try:
        return [parts[i] for i in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = (wintypes.HLOCAL,)
        free.restype = wintypes.HLOCAL
        free(ctypes.cast(parts, wintypes.HLOCAL))


def classify_client(pid, argv, tunnel_id):
    """Only explicit CLI tunnel overrides prove a different binding."""
    if not argv or len(argv) < 2:
        return {'pid': pid, 'reason': 'client_command_unavailable'}
    args = argv[1:]
    if args[0] in ('init', 'doctor', 'help', 'profiles', 'runtimes', 'admin', '--help', '--version'):
        return None
    if args[0] != 'run':
        return {'pid': pid, 'reason': 'client_mode_unconfirmed'}
    ids = []
    for i, arg in enumerate(args):
        if arg.startswith('--control-plane.tunnel-id='):
            ids.append(arg.split('=', 1)[1])
        elif arg == '--control-plane.tunnel-id' and i + 1 < len(args):
            ids.append(args[i + 1])
    if len(ids) == 1 and re.fullmatch(r'tunnel_[0-9a-f]{32}', ids[0]):
        if ids[0] != tunnel_id:
            return None
        return {'pid': pid, 'reason': 'same_tunnel_client_active'}
    # Other process environments/profile locations cannot be established from
    # CIM. Reading our own default profile would be an unsafe inference.
    return {'pid': pid, 'reason': 'active_client_binding_unconfirmed'}


CIM_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$names = @('tunnel-client.exe', $env:RELAY_CLIENT_BASENAME)
$rows = @(Get-CimInstance Win32_Process | Where-Object { $names -contains $_.Name } |
    ForEach-Object { @{ pid = [int]$_.ProcessId; command = $_.CommandLine } })
@{ processes = $rows } | ConvertTo-Json -Compress -Depth 4
'''


def inspect_clients(client, tunnel_id, windows=None):
    blockers = []
    if windows is None:
        windows = os.name == 'nt'
    if windows:
        env = dict(os.environ, RELAY_CLIENT_BASENAME=Path(client).name)
        encoded = base64.b64encode(CIM_SCRIPT.encode('utf-16le')).decode('ascii')
        try:
            run = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                 env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            if run.returncode:
                raise ValueError
            rows = json.loads(run.stdout.decode('utf-8-sig'))['processes']
            if not isinstance(rows, list):
                raise ValueError
            for row in rows:
                if not isinstance(row, dict) or type(row.get('pid')) is not int:
                    raise ValueError
                try:
                    args = windows_argv(row.get('command'))
                except (ValueError, TypeError):
                    args = None
                item = classify_client(row['pid'], args, tunnel_id)
                if item:
                    blockers.append(item)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
            raise ValueError('Local tunnel-client inspection failed or is inconclusive; raw process data omitted') from None
    elif Path('/proc').is_dir():
        names = {'tunnel-client', Path(client).name}
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                raw = (entry / 'cmdline').read_bytes()
                argv = [v.decode('utf-8', 'replace') for v in raw.split(b'\0') if v]
                if not argv:
                    continue
                if Path(argv[0]).name not in names:
                    if len(argv) < 2 or Path(argv[1]).name not in names:
                        continue
                    argv = argv[1:]  # Executable script fixture/interpreter.
                item = classify_client(int(entry.name), argv, tunnel_id)
                if item:
                    blockers.append(item)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError:
                # Process may disappear between directory enumeration and read.
                continue
    else:
        raise ValueError('Legacy tunnel-client inspection is unavailable on this host')
    return blockers


def refuse_active_clients(client, tunnel_id):
    blockers = inspect_clients(client, tunnel_id)
    if blockers:
        pids = ','.join(str(item['pid']) for item in blockers)
        raise ValueError('Existing or unconfirmed tunnel-client process blocks startup (PID ' + pids + '). Stop the old client locally before starting Relay')
