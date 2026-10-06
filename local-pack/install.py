"""Fresh installation of the packaged runtime. No old installation/state is imported."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / 'tools'))
from support import check_entries, default_root, mcp_command_text, mcp_stdio_argv, plain_path, project_record, read_json, sha, write_json
from verify_package import verify

SOURCE = Path(__file__).resolve().parent
PACKAGE_VERSION = '0.2.0-preview.3'
RUNTIME_VERSION = '0.2.0-preview.3'
NATIVE_ACK = 'local-account-r-execution-without-os-containment'


def cli_command(name, npm_entry):
    found = shutil.which(name)
    if not found:
        return None
    p = Path(found)
    if os.name == 'nt' and p.suffix.lower() in ('.cmd', '.bat', '.ps1'):
        node = shutil.which('node')
        entry = p.parent / 'node_modules' / npm_entry
        return [node, str(entry)] if node and entry.is_file() else None
    return [str(p.resolve())]


def install(args):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or later is required')
    root = plain_path(args.root)
    if root == SOURCE or root.is_relative_to(SOURCE) or SOURCE.is_relative_to(root):
        raise ValueError('Installation root and unpacked source must be separate')
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError('Target is not empty. Choose another root; existing installations are preserved')
    if not 1 <= args.port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    if args.demo and args.project:
        raise ValueError('Choose either --demo or --project')
    if bool(args.rscript) != args.ack_native_r:
        raise ValueError('--rscript and --ack-native-r must be supplied together')
    if args.rscript and not (args.project or args.demo):
        raise ValueError('Native R configuration requires a project')
    rscript = plain_path(args.rscript) if args.rscript else None
    if rscript and (not rscript.is_file() or rscript.name.lower() not in ('rscript', 'rscript.exe')):
        raise ValueError('Supply the exact installed Rscript executable')
    manifest = verify(SOURCE)
    if manifest['package_version'] != PACKAGE_VERSION or manifest['runtime_version'] != RUNTIME_VERSION:
        raise ValueError('Package identity differs from this installer')
    check_entries(SOURCE, manifest['files'])
    project = project_record(args.project, args.project_id, args.writable) if args.project else None
    if project:
        p = Path(project['root'])
        if p == root or root.is_relative_to(p) or p.is_relative_to(root):
            raise ValueError('External project and installation root must be separate')
    if args.demo and not shutil.which('git'):
        raise ValueError('Git is required for the isolated demo')
    plan = {'package_version': PACKAGE_VERSION, 'runtime_version': RUNTIME_VERSION,
            'root': str(root), 'projects': [args.project_id] if (args.project or args.demo) else [],
            'fresh_state': True, 'connection': 'configure separately; no account or tunnel changes'}
    if args.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    root.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix='.workbench-install-', dir=root.parent))
    published = False
    try:
        app_relative = f'app/{RUNTIME_VERSION}/project-workbench'
        app = staged / app_relative
        # Copy only hashed package members; never scoop up an unlisted local config/key.
        for item in manifest['files']:
            rel = item['path']
            if rel.startswith('project-workbench/'):
                dest = staged / app_relative / rel[len('project-workbench/'):]
            elif rel.startswith('tools/'):
                dest = staged / 'maintenance' / rel[len('tools/'):]
            elif rel.startswith('docs/'):
                dest = staged / rel
            elif rel == 'LICENSE':
                dest = staged / rel
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / rel, dest)
        for name in ('config', 'state', 'connection'):
            (staged / name).mkdir()
        if os.name != 'nt':
            (staged / 'config').chmod(0o700)
            (staged / 'state').chmod(0o700)
        if args.demo:
            demo = staged / 'projects' / 'demo'
            demo.mkdir(parents=True)
            for argv in (['init', '-b', 'main'], ['config', 'user.name', 'Project Relay Demo'],
                         ['config', 'user.email', 'demo@example.invalid']):
                subprocess.run(['git', '-C', str(demo), *argv], check=True, capture_output=True)
            (demo / 'README.md').write_text('# Project Relay isolated demo\n', encoding='utf-8')
            subprocess.run(['git', '-C', str(demo), 'add', 'README.md'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(demo), 'commit', '-m', 'Initialize isolated demo'],
                           check=True, capture_output=True)
            project = project_record(demo, args.project_id, True)
            project['root'] = str(root / 'projects' / 'demo')
        if project:
            project['codex_command'] = cli_command('codex', '@openai/codex/bin/codex.js')
            project['claude_command'] = cli_command('claude', '@anthropic-ai/claude-code/cli.js')
            project['claude_allowed_tools'] = ['Read', 'Edit', 'Write', 'Glob', 'Grep']
            project['claude_max_turns'] = 40
            if rscript:
                project['compute_runtime'] = {'backend': 'native_trusted', 'rscript': str(rscript),
                                              'trust_acknowledgement': NATIVE_ACK}
        cfg = {'version': 1, 'data_dir': str(root / 'state'), 'port': args.port,
               'auth_token': secrets.token_urlsafe(36), 'projects': [project] if project else [],
               'binary_transfer': {'allowed_download_hosts': []},
               'file_snapshots': {'max_bytes': 20971520, 'snapshot_ttl_seconds': 86400,
                                  'approval_ttl_seconds': 900, 'max_store_bytes': 1073741824}}
        write_json(staged / 'config' / 'config.json', cfg, private=True)
        python = str(Path(sys.executable).resolve())
        argv = mcp_stdio_argv(root, python)
        write_json(staged / 'connection' / 'stdio.json',
                   {'mcpServers': {'project-workbench': {'type': 'stdio', 'command': argv[0],
                    'args': argv[1:], 'cwd': str(root)}}})
        command = mcp_command_text(root, python)
        (staged / 'connection' / 'mcp-command.txt').write_text(command + '\n', encoding='utf-8')
        for mode, filename in (('http', 'Start-HTTP.ps1'), ('stdio', 'Start-MCP.ps1')):
            text = "$ErrorActionPreference = 'Stop'\n" + \
                   "$TaskReceipt = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'INSTALLATION.json') -Raw -Encoding UTF8 | ConvertFrom-Json\n" + \
                   f"& $TaskReceipt.python (Join-Path $PSScriptRoot 'maintenance/launch.py') --root $PSScriptRoot --mode {mode}\nexit $LASTEXITCODE\n"
            (staged / filename).write_text(text, encoding='utf-8-sig')
        installed_files = []
        for item in manifest['files']:
            rel = item['path']
            if rel.startswith('project-workbench/'):
                dest = app_relative + '/' + rel[len('project-workbench/'):]
            elif rel.startswith('tools/'):
                dest = 'maintenance/' + rel[len('tools/'):]
            elif rel.startswith('docs/'):
                dest = rel
            elif rel == 'LICENSE':
                dest = rel
            else:
                continue
            installed_files.append({**item, 'path': dest})
        for name in ('Start-HTTP.ps1', 'Start-MCP.ps1'):
            p = staged / name
            installed_files.append({'path': name, 'bytes': p.stat().st_size, 'sha256': sha(p)})
        check_entries(staged, installed_files)
        write_json(staged / 'INSTALLATION.json', {'schema': 'workbench.clean-install.v1',
            'status': 'installed', 'package_version': PACKAGE_VERSION, 'runtime_version': RUNTIME_VERSION,
            'python': python, 'app_relative': app_relative,
            'package_manifest_sha256': sha(SOURCE / 'PACKAGE_MANIFEST.json'),
            'installed_files': installed_files, 'host_validation': 'pending'})
        if root.exists():
            root.rmdir()  # Only the previously checked empty directory can be removed.
        staged.rename(root)
        published = True
        print(json.dumps({**plan, 'status': 'installed',
                          'next_step': 'Run maintenance/check_install.py --root <root> --self-test'},
                         ensure_ascii=False, indent=2))
        return 0
    finally:
        if not published:
            shutil.rmtree(staged)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=default_root())
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--project', type=Path)
    parser.add_argument('--project-id', default='sandbox')
    parser.add_argument('--writable', action='store_true', help='Allow writes in the explicitly supplied Git project')
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--rscript', type=Path)
    parser.add_argument('--ack-native-r', action='store_true',
                        help='Acknowledge local-account R execution without OS containment')
    parser.add_argument('--plan', action='store_true', help='Validate and print the plan without writing')
    try:
        return install(parser.parse_args(argv))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print('Installation failed: ' + type(exc).__name__ + (': ' + str(exc) if isinstance(exc, ValueError) else ''), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
