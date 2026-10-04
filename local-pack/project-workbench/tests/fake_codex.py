"""Local subprocess fixture. It is deliberately not a real Codex invocation."""
import json
from pathlib import Path
import sys

prompt = sys.stdin.read()
packet = json.loads(prompt.split("\n", 1)[1])
Path("analysis.txt").write_text("分析完成\n" + packet["goal"], encoding="utf-8")
Path("new file.txt").write_text("new content\n", encoding="utf-8")
index = sys.argv.index("--output-last-message")
Path(sys.argv[index + 1]).write_text(json.dumps({"summary": "Local fake executor fixture completed",
    "findings": [], "tests": ["Fixture process only; no real Codex/model was invoked"],
    "changed_files": ["analysis.txt"]}, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"type": "turn.completed", "fixture": True}))
