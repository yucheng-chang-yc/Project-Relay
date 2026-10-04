"""Start one facade per clean installation. A tunnel uses this stdio entrypoint."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from support import installation, prepare_shutdown_signals


def main():
    stopping = False
    def stop_signal(*_):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise KeyboardInterrupt
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--mode', choices=('stdio', 'http'), required=True)
    args = parser.parse_args()
    lock = None
    acquired = False
    child = None
    try:
        prepare_shutdown_signals(stop_signal)
        root, receipt, app, config, cfg = installation(args.root)
        lock = root / 'state' / 'facade.lock'
        with lock.open('x', encoding='utf-8') as stream:
            json.dump({'pid': os.getpid(), 'mode': args.mode}, stream)
        acquired = True
        argv = [receipt['python'], str(app / 'scripts' / 'serve.py'), '--config', str(config)]
        if args.mode == 'stdio':
            argv.append('--stdio')
        # The tunnel starts this stdio facade with its own control-plane key.
        # Local executors need their provider credentials, not tunnel/admin keys.
        runtime_env = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith('CONTROL_PLANE_')
                       and key.upper() != 'OPENAI_ADMIN_KEY'}
        child = subprocess.Popen(argv, cwd=app, env=runtime_env)
        lock.write_text(json.dumps({'pid': os.getpid(), 'child_pid': child.pid, 'mode': args.mode}), encoding='utf-8')
        # Bounded waits also let a pending Windows SIGBREAK handler run.
        while True:
            try:
                return child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        return 130
    except FileExistsError:
        print('A facade lock exists. Stop the existing HTTP/tunnel process first. After a crash, inspect the recorded PID and its serve child before removing state/facade.lock.', file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError) as exc:
        print('Startup failed: ' + type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        # Ctrl+C may reach the facade just before the helper sends Ctrl+Break.
        # A second console event must not interrupt lock/child cleanup.
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        # A failed stop must preserve the lock for operator inspection.
        if acquired and (child is None or child.poll() is not None):
            lock.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
