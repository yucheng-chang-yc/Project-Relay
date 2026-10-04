"""Operator-configured deterministic binary fixtures. No model, R or containment proof."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile


def make_zip(size):
    info = zipfile.ZipInfo('payload.bin', (2026, 10, 2, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 0
    info.external_attr = 0
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, 'w', allowZip64=False) as archive:
        archive.writestr(info, b'')
    payload_size = size - len(empty.getvalue())
    pattern = bytes(range(256))
    payload = pattern * (payload_size // 256) + pattern[:payload_size % 256]
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', allowZip64=False) as archive:
        archive.writestr(info, payload)
    value = result.getvalue()
    if len(value) != size:
        raise RuntimeError('Fixture size invariant failed')
    return value, hashlib.sha256(payload).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True)
    args = parser.parse_args()
    root = Path(args.project_root).resolve(strict=True)
    if Path.cwd().resolve() != root or not (root / '.git').exists():
        raise SystemExit('Run only in the explicitly configured isolated Git project')
    receipts = []
    for mib in (1, 20):
        path = root / f'host_probe_{mib}m.zip'
        value, payload_sha = make_zip(mib * 1024 * 1024)
        sha = hashlib.sha256(value).hexdigest()
        if path.exists() or path.is_symlink():
            raise SystemExit('Fixture output already exists; preserve it and use a new isolated project')
        with path.open('xb') as output:
            output.write(value)
        receipts.append({'name': path.name, 'size_bytes': len(value), 'sha256': sha,
                         'member': 'payload.bin', 'payload_sha256': payload_sha})
    print(json.dumps({'fixture_only': True, 'tomato_e2e': 'NOT_RUN', 'artifacts': receipts}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
