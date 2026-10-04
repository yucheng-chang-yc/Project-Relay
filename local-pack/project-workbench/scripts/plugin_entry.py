"""Use an installed clean runtime for the portable local plugin."""
import os
from pathlib import Path
import sys

root = Path(os.environ.get('PROJECT_WORKBENCH_ROOT', str(Path.home() / 'ProjectRelay'))).expanduser().resolve()
launcher = root / 'maintenance' / 'launch.py'
if not launcher.is_file():
    print('Install the clean Workbench package first, or set PROJECT_WORKBENCH_ROOT to its installation root.', file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(launcher.parent))
from launch import main
sys.argv = [str(launcher), '--root', str(root), '--mode', 'stdio']
raise SystemExit(main())
