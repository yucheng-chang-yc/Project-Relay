from pathlib import Path
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys


def discover_cli(name, npm_entry):
    executable = shutil.which(name)
    if not executable:
        return None
    p = Path(executable)
    if os.name == "nt" and p.suffix.lower() in (".cmd", ".bat", ".ps1"):
        script = p.parent / "node_modules" / npm_entry
        node = shutil.which("node")
        return [node, str(script)] if node and script.is_file() else None
    return [executable]


def discover_codex():
    return discover_cli("codex", "@openai/codex/bin/codex.js")


def discover_claude():
    return discover_cli("claude", "@anthropic-ai/claude-code/cli.js")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path)
    parser.add_argument("--project-id", default="sandbox")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProjectWorkbench" if os.name == "nt" else Path.home() / ".project-workbench"
    config = (args.config or default / "config.json").resolve()
    if config.exists():
        raise SystemExit("Existing configuration preserved. Edit it deliberately or choose another --config path.")
    if args.demo:
        project = source / "demo-project"
        if project.exists() and any(project.iterdir()):
            raise SystemExit("Existing demo directory preserved. Choose an explicit project instead.")
        project.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(project), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(project), "config", "user.name", "Workbench Demo"], check=True)
        subprocess.run(["git", "-C", str(project), "config", "user.email", "demo@example.invalid"], check=True)
        (project / "README.md").write_text("# Workbench isolated demo\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-m", "Initialize isolated demo"], check=True, capture_output=True)
    elif args.project:
        project = args.project.expanduser().resolve()
    else:
        raise SystemExit("Choose --demo or an exact --project Git root.")
    if not project.is_dir():
        raise SystemExit("Project directory does not exist")
    head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True)
    if head.returncode:
        raise SystemExit("Initial PoC tasks require an existing Git commit.")
    profile = [sys.executable, "-c", "import time;print('Local CLI started',flush=True);time.sleep(2);print('Local CLI completed',flush=True)"]
    data = {"version": 1, "data_dir": str(config.parent / "state"), "port": args.port,
            "auth_token": secrets.token_urlsafe(36), "projects": [{"id": args.project_id,
            "name": project.name, "root": str(project), "writable": True,
            "profiles": {"smoke": profile}, "codex_command": discover_codex(),
            "claude_command": discover_claude(),
            "claude_allowed_tools": ["Read", "Edit", "Write", "Glob", "Grep"], "claude_max_turns": 20}]}
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        config.chmod(0o600)
    print("Configuration created: " + str(config))
    print("Codex detected: " + ("yes" if data["projects"][0]["codex_command"] else "no; configure codex_command after installation"))
    print("Claude Code detected: " + ("yes" if data["projects"][0]["claude_command"] else "no; configure claude_command after installation"))
    print("Run scripts/serve.py --config <configuration path> to start the local dashboard.")


if __name__ == "__main__":
    main()
