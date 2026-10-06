"""Check local installation; optional self-test is a closed, short-lived MCP process."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from support import installation, require_idle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    result = {'schema': 'workbench.install-check.v1', 'status': 'FAIL', 'checks': {},
              'host_chatgpt': 'NOT_TESTED', 'platform': sys.platform}
    try:
        root, receipt, app, config, cfg = installation(args.root)
        result['checks']['installed_file_hashes'] = True
        result['checks']['configuration'] = True
        result['runtime_version'] = receipt['runtime_version']
        result['package_version'] = receipt['package_version']
        result['projects'] = [p['id'] for p in cfg.get('projects', [])]
        result['available'] = {name: bool(shutil.which(name)) for name in ('git', 'codex', 'claude', 'Rscript', 'tunnel-client')}
        result['available_scope'] = 'Executable discovery on PATH only; exact project-configured executables are reported separately.'
        result['project_compute_runtime'] = {
            p['id']: {'backend': p['compute_runtime']['backend'],
                      'rscript_exists': Path(p['compute_runtime']['rscript']).is_file()}
            for p in cfg.get('projects', [])
            if p.get('compute_runtime', {}).get('backend') == 'native_trusted'}
        if args.self_test:
            require_idle(root, for_self_test=True)
            params = {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}}
            messages = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover', 'params': params},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': params},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'resources/list', 'params': params},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                 'params': {**params, 'name': 'list_projects', 'arguments': {}}},
                {'jsonrpc': '2.0', 'id': 5, 'method': 'resources/read',
                 'params': {**params, 'uri': 'ui://project-workbench/file-snapshot-v3.html'}},
                {'jsonrpc': '2.0', 'id': 6, 'method': 'resources/read',
                 'params': {**params, 'uri': 'ui://project-workbench/shared-folder-v1.html'}},
                {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                 'params': {**params, 'name': 'list_shared_folders', 'arguments': {}}}]
            cmd = [receipt['python'], str(root / 'maintenance' / 'launch.py'), '--root', str(root), '--mode', 'stdio']
            run = subprocess.run(cmd, input=''.join(json.dumps(m) + '\n' for m in messages),
                                 text=True, encoding='utf-8', capture_output=True, timeout=30)
            if run.returncode:
                raise ValueError('MCP self-test process failed (credentials and raw output omitted)')
            rows = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
            if len(rows) != len(messages) or any('error' in r for r in rows):
                raise ValueError('MCP self-test returned invalid or error responses')
            by_id = {r['id']: r['result'] for r in rows}
            if set(by_id) != {1, 2, 3, 4, 5, 6, 7}:
                raise ValueError('MCP response IDs differ')
            actual = by_id[1].get('_meta', {}).get('io.modelcontextprotocol/serverInfo', {}).get('version')
            if actual != receipt['runtime_version']:
                raise ValueError('Actual MCP runtime version differs from the installation receipt')
            result['actual_mcp_runtime_version'] = actual
            for ident in (1, 2, 3, 5, 6):
                r = by_id[ident]
                if r.get('ttlMs') != 300000 or r.get('cacheScope') != 'public':
                    raise ValueError('Missing discovery/resource caching hints')
            names = {t['name'] for t in by_id[2]['tools']}
            needed = {'request_file_snapshot', 'get_file_snapshot', 'read_file_snapshot_bytes',
                      'start_codex_task', 'start_claude_task', 'get_task_result',
                      'request_shared_folder', 'read_shared_file_bytes', 'commit_shared_file_write', 'archive_task'}
            if not needed <= names or by_id[4].get('isError') or by_id[7].get('isError'):
                raise ValueError('Required tools or project query failed')
            content = by_id[5]['contents'][0]
            if content['mimeType'] != 'text/html;profile=mcp-app' or '</html>' not in content['text']:
                raise ValueError('Snapshot approval resource is incomplete')
            result['checks']['mcp_stdio'] = True
            result['checks']['cache_hints'] = True
            result['checks']['snapshot_template'] = True
            result['tool_count'] = len(names)
            result['snapshot_template_sha256'] = hashlib.sha256(content['text'].encode()).hexdigest()
            shared = by_id[6]['contents'][0]
            if shared['mimeType'] != 'text/html;profile=mcp-app' or '</html>' not in shared['text']:
                raise ValueError('Shared-folder approval resource is incomplete')
            result['checks']['shared_folder_template'] = True
            result['checks']['shared_folder_query'] = True
            result['shared_folder_count'] = len(by_id[7]['structuredContent']['data'])
        result['status'] = 'PASS'
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        result['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
