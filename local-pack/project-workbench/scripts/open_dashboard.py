from pathlib import Path
import json
import os
import webbrowser

default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProjectWorkbench" / "config.json" if os.name == "nt" else Path.home() / ".project-workbench" / "config.json"
config = Path(os.environ.get("PROJECT_WORKBENCH_CONFIG", str(default)))
data = json.loads(config.read_text(encoding="utf-8-sig"))
# Fragment is not sent in HTTP requests; UI clears it immediately and stores token only in memory.
webbrowser.open(f"http://127.0.0.1:{data.get('port',8765)}/#token=" + data["auth_token"])
