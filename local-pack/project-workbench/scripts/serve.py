"""Portable plugin launcher. Configuration stays outside the plugin package."""
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.server import main

if "--config" not in sys.argv:
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProjectWorkbench" / "config.json" if os.name == "nt" else Path.home() / ".project-workbench" / "config.json"
    config = os.environ.get("PROJECT_WORKBENCH_CONFIG", str(default))
    if not Path(config).is_file():
        print("Configure Project Workbench locally before connecting this plugin.", file=sys.stderr)
        raise SystemExit(2)
    sys.argv.extend(["--config", config])
main()
