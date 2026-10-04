"""Check every declared release file and detect undeclared release members."""
import argparse
import json
from pathlib import Path
import sys
from support import check_entries, read_json


def verify(root):
    root = Path(root).resolve()
    manifest = read_json(root / 'PACKAGE_MANIFEST.json')
    check_entries(root, manifest['files'])
    expected = {f['path'] for f in manifest['files']}
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()
              and not {'.git', '__pycache__', '.pytest_cache'}.intersection(p.parts) and p.suffix != '.pyc'
              and p.relative_to(root).as_posix() != 'PACKAGE_MANIFEST.json'}
    if actual != expected:
        raise ValueError('Release has undeclared or missing members')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        manifest = verify(args.package)
        print(json.dumps({'status': 'PASS', 'files': len(manifest['files']),
                          'package_version': manifest['package_version'],
                          'runtime_version': manifest['runtime_version']}, indent=2))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print('Package verification failed: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
