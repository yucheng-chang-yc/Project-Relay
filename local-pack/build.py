"""Rebuild the clean ZIP from reviewed source. Generated installation data is excluded."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

VERSION = '0.2.0-preview.2'
IGNORED_DIRS = {'.git', '__pycache__', '.pytest_cache'}


def write_zip(output, prefix, members):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(prefix + '/' + name, (2026, 10, 3, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument('--output', type=Path)
    targets.add_argument('--release-dir', type=Path, help='Build Local ZIP, Plugin Template, docs and SHA256SUMS together')
    args = parser.parse_args()
    release_dir = args.release_dir.expanduser().resolve() if args.release_dir else None
    output = (release_dir / ('Project-Relay-Local-v' + VERSION + '.zip') if release_dir else
              (args.output or root.parent / ('Project-Relay-Local-v' + VERSION + '.zip')).expanduser().resolve())
    if output.is_relative_to(root):
        raise SystemExit('Write the archive outside the release source directory')
    manifest = root / 'PACKAGE_MANIFEST.json'
    files = []
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p == manifest or IGNORED_DIRS.intersection(p.parts) or p.suffix == '.pyc':
            continue
        if p.is_symlink() or any(x in p.parts for x in ('state', '.git', 'node_modules')) or p.suffix in ('.sqlite3', '.pem', '.key'):
            raise SystemExit('Unexpected local state/link/key in release source')
        if p.name in ('INSTALLATION.json', 'config.json', 'facade.lock'):
            raise SystemExit('Do not package a configured installation')
        raw = p.read_bytes()
        files.append({'path': p.relative_to(root).as_posix(), 'bytes': len(raw),
                      'sha256': hashlib.sha256(raw).hexdigest()})
    manifest.write_text(json.dumps({'schema': 'workbench.release.v1',
        'package_version': VERSION, 'runtime_version': '0.2.0-spike.4',
        'files': files}, indent=2) + '\n', encoding='utf-8')
    sys.path.insert(0, str(root / 'tools'))
    from verify_package import verify
    verify(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    members = {p.relative_to(root).as_posix(): p.read_bytes()
               for p in [root / f['path'] for f in files] + [manifest]}
    write_zip(output, 'Project-Relay-Local-v' + VERSION, members)
    report = {'zip': str(output), 'bytes': output.stat().st_size,
                      'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                      'files': len(files) + 1}
    if release_dir:
        template = release_dir / ('Project-Relay-Plugin-Template-v' + VERSION + '.zip')
        plugin = json.loads((root / 'project-workbench/plugin.json').read_text())
        if plugin['version'] != VERSION or plugin['name'] != 'project-workbench':
            raise SystemExit('Plugin template identity differs from Local package')
        skill = 'skills/project-workbench/SKILL.md'
        template_members = {'plugin.json': (json.dumps(plugin, indent=2) + '\n').encode(),
                            skill: (root / 'project-workbench' / skill).read_bytes(),
                            'LICENSE': (root / 'LICENSE').read_bytes(),
                            'README.md': (root / 'docs/PLUGIN_TEMPLATE.md').read_bytes()}
        write_zip(template, 'Project-Relay-Plugin-Template-v' + VERSION, template_members)
        for source, name in [('README.md', 'README.md'), ('docs/INSTALL.md', 'INSTALL.md'),
                             ('docs/RELEASE_NOTES.md', 'RELEASE_NOTES.md'), ('LICENSE', 'LICENSE')]:
            (release_dir / name).write_bytes((root / source).read_bytes())
        release_names = [output.name, template.name, 'README.md', 'INSTALL.md', 'RELEASE_NOTES.md', 'LICENSE']
        sums = ''.join(hashlib.sha256((release_dir / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                       for name in release_names)
        (release_dir / 'SHA256SUMS.txt').write_text(sums, encoding='utf-8')
        report['release_files'] = release_names + ['SHA256SUMS.txt']
        report['plugin_template_sha256'] = hashlib.sha256(template.read_bytes()).hexdigest()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
