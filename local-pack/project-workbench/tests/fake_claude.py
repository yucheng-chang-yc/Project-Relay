"""Local protocol fixture only. No real Claude Code, model, auth or billing is used."""
import json
from pathlib import Path
import sys

argv = sys.argv
assert "-p" in argv and argv[argv.index("--output-format") + 1] == "json"
assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
schema = json.loads(argv[argv.index("--json-schema") + 1])
assert "summary" in schema["required"]
packet = json.loads(sys.stdin.read().split("\n", 1)[1])
goal = packet["goal"]
print("Fixture diagnostic written to stderr; stdout must remain parseable.", file=sys.stderr)
if goal == "auth-failure":
    print(json.dumps({"type": "result", "is_error": True, "result": "fixture: not authenticated"}))
    raise SystemExit(1)
if goal == "malformed-output":
    print("not JSON")
    raise SystemExit(0)
report = {"summary": "Claude protocol fixture completed", "findings": [],
          "tests": ["Fixture only; real Claude Code and authentication remain untested"],
          "changed_files": ["claude-analysis.txt"]}
if goal == "invalid-schema":
    report["tests"] = "wrong field type"
Path("claude-analysis.txt").write_text("Claude fixture\n" + goal, encoding="utf-8")
result = {"type": "result", "subtype": "success", "is_error": False,
          "structured_output": report, "session_id": "fixture-session"}
if goal == "permission-failure":
    result["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "fixture denied"}}]
print(json.dumps(result, ensure_ascii=False))
