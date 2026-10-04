"""Check project inspection through a short-lived installed MCP process."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from support import installation, require_idle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    report = {'status': 'FAIL', 'scope': 'local installed MCP project inspection',
              'host_chatgpt': 'NOT_TESTED', 'executor_run': 'NOT_RUN', 'checks': {}}
    try:
        root, receipt, app, config, cfg = installation(args.root)
        require_idle(root, for_self_test=True)
        if args.project_id not in {p['id'] for p in cfg['projects']}:
            raise ValueError('Register this project before running its smoke test')
        meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}
        calls = [('list_projects', {}),
                 ('read_file', {'project_id': args.project_id, 'path': 'README.md', 'limit': 65536}),
                 ('git_status', {'project_id': args.project_id}),
                 ('get_project_capabilities', {'project_id': args.project_id}),
                 ('preflight_task', {'project_id': args.project_id, 'executor': 'command', 'profile': 'smoke'})]
        messages = [{'jsonrpc': '2.0', 'id': i, 'method': 'tools/call',
                     'params': {'name': name, 'arguments': arguments, '_meta': meta}}
                    for i, (name, arguments) in enumerate(calls, 1)]
        run = subprocess.run([receipt['python'], str(root / 'maintenance/launch.py'),
                              '--root', str(root), '--mode', 'stdio'],
                             input=''.join(json.dumps(m) + '\n' for m in messages),
                             text=True, encoding='utf-8', capture_output=True, timeout=30)
        if run.returncode:
            raise ValueError('MCP process failed; raw output omitted')
        rows = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
        if len(rows) != len(calls) or {r.get('id') for r in rows} != set(range(1, 6)):
            raise ValueError('MCP response set is incomplete')
        results = {}
        for row in rows:
            result = row.get('result', {})
            if 'error' in row or result.get('isError'):
                raise ValueError('A project inspection call failed; raw output omitted')
            results[row['id']] = result['structuredContent']['data']
        if args.project_id not in {p['id'] for p in results[1]}:
            raise ValueError('Registered project was not returned')
        if not isinstance(results[2].get('text'), str) or not results[2].get('sha256'):
            raise ValueError('README preview or hash missing')
        if not results[3].get('head'):
            raise ValueError('Git HEAD missing')
        if not isinstance(results[4], dict):
            raise ValueError('Capabilities response missing')
        if results[5].get('status') != 'eligible' or results[5].get('task_created') is not False:
            raise ValueError('Configured smoke profile is not eligible')
        report.update(status='PASS', project_id=args.project_id,
                      runtime_version=receipt['runtime_version'],
                      readme_sha256=results[2]['sha256'],
                      git_head=results[3]['head'], git_clean=results[3]['clean'])
        report['checks'] = {name: True for name, _ in calls}
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
