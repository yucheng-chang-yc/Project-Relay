"""Hash-guarded preview.3 D-005 patch. Default is read-only plan; no service control."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

HERE = Path(__file__).resolve().parent
APP = 'app/0.2.0-preview.3/project-workbench'
SOURCE = APP + '/workbench/shared.py'
TEST = APP + '/tests/test_shared_publication.py'
FIX = 'D005-v1'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def plain(path):
    path = path.expanduser().absolute()
    for p in (path, *path.parents):
        if p.exists() or p.is_symlink():
            st = p.lstat()
            if p.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Links/junctions are refused')
    return path.resolve()

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def replace(path, data):
    path = plain(path)
    fd, temp = tempfile.mkstemp(prefix='.d005-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)

def stopped(root):
    for name in ('state/facade.lock', 'connection/tunnel-process.lock'):
        if (root/name).exists():
            raise ValueError('Service lock remains; stop/inspect the owner locally. Never delete a live lock')
    db = root/'state/workbench.sqlite3'
    if db.exists():
        with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
                if c.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running','cancelling','interrupted') LIMIT 1").fetchone():
                    raise ValueError('Active/uncertain executor task remains; do not patch or cancel user work')

def verify_receipt(root, receipt, ignored=()):
    if receipt.get('schema') != 'workbench.clean-install.v1' or receipt.get('status') != 'installed' or receipt.get('runtime_version') != '0.2.0-preview.3' or receipt.get('app_relative') != APP:
        raise ValueError('Only an installed preview.3 clean package is supported')
    for item in receipt['installed_files']:
        rel = item['path']
        if rel in ignored:
            continue
        p = plain(root/rel)
        if not p.is_relative_to(root) or not p.is_file() or p.stat().st_size != item['bytes'] or sha(p) != item['sha256']:
            raise ValueError('Installed file differs from receipt: '+rel)

def rollback(root, backup, manifest):
    stopped(root)
    record = read(backup/'BACKUP.json')
    if record['root'] != str(root) or record['fix'] != FIX:
        raise ValueError('Backup belongs to another installation/fix')
    original_receipt = read(backup/'INSTALLATION.json')
    verify_receipt(root, original_receipt, (SOURCE, TEST))
    for name, digest in record['backup_hashes'].items():
        if sha(plain(backup/name)) != digest:
            raise ValueError('Backup integrity differs')
    if sha(root/SOURCE) not in (manifest['before_sha256'], manifest['after_sha256']):
        raise ValueError('Current shared.py differs; do not overwrite later changes')
    if (root/TEST).exists() and sha(root/TEST) != manifest['test_sha256']:
        raise ValueError('Current regression test differs; do not delete later changes')
    receipt_sha = sha(root/'INSTALLATION.json')
    if receipt_sha not in (record['before_receipt_sha256'], record['after_receipt_sha256']):
        raise ValueError('Current installation receipt changed; return for review')
    replace(root/SOURCE, (backup/'shared.py').read_bytes())
    (root/TEST).unlink(missing_ok=True)
    replace(root/'INSTALLATION.json', (backup/'INSTALLATION.json').read_bytes())
    verify_receipt(root, original_receipt)
    return {'status':'rolled_back', 'runtime_version':'0.2.0-preview.3',
            'state':'preserved; diagnostic table/history retained, no database restore'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true')
    modes.add_argument('--rollback', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    root = plain(args.root)
    manifest = read(HERE/'PATCH_MANIFEST.json')
    for name, key in (('candidate/shared.py','after_sha256'), ('candidate/test_shared_publication.py','test_sha256')):
        if sha(plain(HERE/name)) != manifest[key]:
            raise ValueError('Candidate integrity differs')
    if args.rollback:
        if not args.backup_dir:
            raise ValueError('--rollback requires the exact recorded backup directory')
        return rollback(root, plain(args.backup_dir), manifest)
    receipt_path = root/'INSTALLATION.json'
    receipt = read(receipt_path)
    verify_receipt(root, receipt)
    current = sha(root/SOURCE)
    if current == manifest['after_sha256'] and (root/TEST).is_file() and sha(root/TEST) == manifest['test_sha256'] and any(h.get('id') == FIX for h in receipt.get('hotfixes', [])):
        return {'status':'unchanged', 'runtime_version':'0.2.0-preview.3', 'hotfix':FIX}
    if current != manifest['before_sha256'] or (root/TEST).exists():
        raise ValueError('Baseline differs; do not overwrite another patch or user changes')
    if not args.apply:
        return {'status':'plan', 'runtime_version':'0.2.0-preview.3', 'hotfix':FIX,
                'files':[SOURCE, TEST, 'INSTALLATION.json'], 'restart_required':True,
                'configuration_and_existing_state':'preserved', 'old_uncertain_writes':'not auto-cleared'}
    stopped(root)
    if not args.backup_dir:
        raise ValueError('--apply requires a new private backup directory')
    backup = plain(args.backup_dir)
    if backup == root or backup.is_relative_to(root) or backup == HERE or backup.is_relative_to(HERE):
        raise ValueError('Backup must be outside installation and extracted patch bundle')
    db = root/'state/workbench.sqlite3'
    if db.exists():
        with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro', uri=True)) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='shared_grants'").fetchone():
                for (folder,) in c.execute('SELECT path FROM shared_grants'):
                    if backup == Path(folder).resolve() or backup.is_relative_to(Path(folder).resolve()):
                        raise ValueError('Private backup cannot be placed in a shared folder')
    # Preserve the original package provenance; record the overlay separately.
    new = json.loads(json.dumps(receipt))
    for item in new['installed_files']:
        if item['path'] == SOURCE:
            item.update(bytes=(HERE/'candidate/shared.py').stat().st_size, sha256=manifest['after_sha256'])
    new['installed_files'].append({'path':TEST,'bytes':(HERE/'candidate/test_shared_publication.py').stat().st_size,'sha256':manifest['test_sha256']})
    new.setdefault('hotfixes', []).append({'id':FIX,'source_sha256':manifest['after_sha256'],'patch_manifest_sha256':sha(HERE/'PATCH_MANIFEST.json')})
    raw = (json.dumps(new,indent=2)+'\n').encode()
    backup.mkdir(parents=True, exist_ok=False)
    (backup/'shared.py').write_bytes((root/SOURCE).read_bytes())
    (backup/'INSTALLATION.json').write_bytes(receipt_path.read_bytes())
    (backup/'patched-INSTALLATION.json').write_bytes(raw)
    if db.exists():
        with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as src, contextlib.closing(sqlite3.connect(backup/'workbench.sqlite3')) as dst:
            src.backup(dst)
    record = {'fix':FIX,'root':str(root),'before_receipt_sha256':sha(receipt_path),
              'after_receipt_sha256':hashlib.sha256(raw).hexdigest(),
              'backup_hashes':{name:sha(backup/name) for name in ('shared.py','INSTALLATION.json','patched-INSTALLATION.json')}}
    (backup/'BACKUP.json').write_text(json.dumps(record,indent=2)+'\n')
    try:
        replace(root/SOURCE,(HERE/'candidate/shared.py').read_bytes())
        replace(root/TEST,(HERE/'candidate/test_shared_publication.py').read_bytes())
        replace(receipt_path,raw)
        verify_receipt(root,new)
    except Exception:
        rollback(root,backup,manifest)
        raise
    return {'status':'applied','runtime_version':'0.2.0-preview.3','hotfix':FIX,
            'source_sha256':manifest['after_sha256'],'backup':str(backup),'restart_required':True,
            'old_uncertain_writes':'unchanged; local exact-SHA recovery remains required'}

if __name__ == '__main__':
    try:
        print(json.dumps(main(),indent=2))
    except Exception as error:
        print('D005 patch stopped: '+type(error).__name__+': '+str(error),file=sys.stderr)
        raise SystemExit(1)
