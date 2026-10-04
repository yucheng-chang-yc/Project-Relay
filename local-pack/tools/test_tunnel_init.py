"""Exercise the real full tunnel-client init preflight without keys/network/run."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from support import installation, mcp_command_text
from connect_chatgpt import child_environment, executable, mcp_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--tunnel-client', default='tunnel-client')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = {'status': 'FAIL', 'platform': sys.platform, 'scope': 'real external client init preflight only',
              'network_connection': 'NOT_RUN', 'runtime_start': 'NOT_RUN', 'host_chatgpt': 'NOT_TESTED', 'cases': []}
    try:
        root, receipt, _app, _config, _cfg = installation(args.root)
        client = executable(args.tunnel_client)
        with tempfile.TemporaryDirectory(prefix='ProjectRelay-init-probe-') as temp:
            scratch = Path(temp)
            profiles = scratch / 'profiles'
            profiles.mkdir()
            commands = [('installed_command', mcp_command(root, receipt))]
            for name, folder in [('space_unicode_paths', '分析 relay with spaces'),
                                 ('apostrophe_paths', "analyst's relay")]:
                sample = scratch / folder
                (sample / 'maintenance').mkdir(parents=True)
                (sample / 'maintenance/launch.py').write_text('# init preflight fixture; not executed\n')
                python = scratch / (folder + ' Python') / Path(receipt['python']).name
                python.parent.mkdir()
                # Temporary interpreter copies test executable path parsing;
                # init only checks the path. They are never started as Python.
                shutil.copy2(receipt['python'], python)
                commands.append((name, mcp_command_text(sample, python)))
            for i, (name, command) in enumerate(commands):
                profile = 'relay-path-probe-' + str(i)
                run = subprocess.run([client, 'init', '--sample', 'sample_mcp_stdio_local',
                        '--profile-dir', str(profiles), '--profile', profile,
                        '--tunnel-id', 'tunnel_' + '1' * 32, '--mcp-command', command,
                        '--health-listen-addr', '127.0.0.1:0'],
                        env=child_environment(profiles), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                generated = profiles / (profile + '.yaml')
                exact = False
                if run.returncode == 0 and generated.is_file():
                    values = re.findall(r'^\s*command:\s*(.+)$', generated.read_text(encoding='utf-8-sig'), re.M)
                    exact = len(values) == 1 and json.loads(values[0]) == command
                report['cases'].append({'case': name, 'exit_code': run.returncode,
                                        'generated_exact_command': exact})
            if all(x['exit_code'] == 0 and x['generated_exact_command'] for x in report['cases']):
                report['status'] = 'PASS'
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
