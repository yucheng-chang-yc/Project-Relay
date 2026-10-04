"""Operator-only deterministic inventory. Does not install, activate, or change profiles."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

def decode(raw):
    if raw.startswith(b'\xff\xfe'):
        return raw.decode('utf-16', 'replace')
    if raw[:512].count(b'\0') > len(raw[:512]) // 5:
        return raw.decode('utf-16-le', 'replace')
    # BOM-less UTF-16-LE with mostly CJK text (e.g. localized wsl.exe) has fewer NULs than the
    # ratio above; ordinary version messages have no embedded NULs, so try UTF-16-LE first.
    if b'\0' in raw and len(raw) % 2 == 0:
        try:
            return raw.decode('utf-16-le')
        except UnicodeDecodeError:
            pass
    return raw.decode('utf-8-sig', 'replace')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    results = {'schema': 'wb.compute_doctor.v1', 'platform': platform.platform(), 'probes': [],
               'native_windows_enforcement': 'UNVERIFIED', 'host_binary_route': 'UNVERIFIED'}
    choices = {'Rscript': ['--version'], 'python': ['--version'], 'claude': ['--version'],
               'docker': ['version', '--format', '{{json .Server}}'], 'podman': ['version', '--format', 'json']}
    if os.name == 'nt':
        choices['wsl'] = ['--list', '--quiet']
    for name, flags in choices.items():
        executable = shutil.which(name)
        record = {'name': name, 'path': executable, 'argv': flags}
        if executable:
            try:
                p = subprocess.run([executable, *flags], stdin=subprocess.DEVNULL, capture_output=True, timeout=8, shell=False)
                for stream in ('stdout', 'stderr'):
                    raw = getattr(p, stream)
                    record[stream] = {'text': decode(raw[:65536]), 'sha256': hashlib.sha256(raw).hexdigest(),
                                      'size_bytes': len(raw), 'raw_base64': base64.b64encode(raw[:65536]).decode('ascii')}
                record['exit_code'] = p.returncode
            except (OSError, subprocess.TimeoutExpired) as e:
                record['error'] = type(e).__name__
        results['probes'].append(record)
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(Path(args.output).resolve()), 'compute_ready': False,
                      'reason': 'Inventory alone does not prove containment or ChatGPT host transfer'}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
