"""Register an exact Git root locally while the installation is idle."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from support import installation, project_record, require_idle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--writable', action='store_true')
    parser.add_argument('--rscript', type=Path)
    parser.add_argument('--ack-native-r', action='store_true')
    args = parser.parse_args()
    tmp = None
    lock = None
    acquired = False
    try:
        root, receipt, app, config, cfg = installation(args.root)
        # The same lock coordinates registrations, launchers and self-tests.
        lock = root / 'state' / 'facade.lock'
        require_idle(root)
        with lock.open('x', encoding='utf-8') as stream:
            json.dump({'pid': os.getpid(), 'mode': 'configure'}, stream)
        acquired = True
        item = project_record(args.project, args.project_id, args.writable)
        project = Path(item['root'])
        if project == root or root.is_relative_to(project) or project.is_relative_to(root):
            raise ValueError('Register an external project; installation and project must be separate')
        if any(p['id'] == item['id'] or os.path.normcase(p['root']) == os.path.normcase(item['root']) for p in cfg['projects']):
            raise ValueError('Project ID or root is already registered')
        sys.path.insert(0, str(app / 'scripts'))
        from configure import discover_codex, discover_claude
        item.update({'codex_command': discover_codex(), 'claude_command': discover_claude(),
                     'claude_allowed_tools': ['Read', 'Edit', 'Write', 'Glob', 'Grep'], 'claude_max_turns': 40})
        if bool(args.rscript) != args.ack_native_r:
            raise ValueError('--rscript and --ack-native-r must be supplied together')
        if args.rscript:
            from support import plain_path
            p = plain_path(args.rscript)
            if not p.is_file() or p.name.lower() not in ('rscript', 'rscript.exe'):
                raise ValueError('Supply the exact installed Rscript executable')
            item['compute_runtime'] = {'backend': 'native_trusted', 'rscript': str(p),
                 'trust_acknowledgement': 'local-account-r-execution-without-os-containment'}
        cfg['projects'].append(item)
        fd, temp = tempfile.mkstemp(prefix='.config-', dir=config.parent)
        tmp = Path(temp)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        if os.name != 'nt':
            tmp.chmod(0o600)
        os.replace(tmp, config)
        tmp = None
        print(json.dumps({'registered': item['id'], 'writable': item['writable'],
                          'codex_configured': bool(item['codex_command']),
                          'claude_configured': bool(item['claude_command']),
                          'native_r_enabled': bool(args.rscript)}, indent=2))
        return 0
    except (ValueError, KeyError, OSError, __import__('subprocess').SubprocessError) as exc:
        print('Registration failed: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        return 1
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)
        if acquired:
            lock.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
