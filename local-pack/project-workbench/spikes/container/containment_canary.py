"""Run only inside the proposed container. Reports observable deny assertions."""
import json
from pathlib import Path
import socket
result = {}
try:
    Path('/root/workbench-escape').write_text('escape')
    result['outside_write_denied'] = False
except OSError:
    result['outside_write_denied'] = True
try:
    with socket.create_connection(('1.1.1.1', 443), timeout=2):
        result['network_denied'] = False
except OSError:
    result['network_denied'] = True
result['daemon_socket_absent'] = not any(Path(p).exists() for p in ('/var/run/docker.sock', '/run/podman/podman.sock'))
result['host_drive_absent'] = not Path('/mnt/c/Users').exists()
result['project_write_allowed'] = False
try:
    Path('canary-output.txt').write_text('container project output')
    result['project_write_allowed'] = True
except OSError:
    pass
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if all(result.values()) else 1)
