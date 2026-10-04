"""Small CLI-specific boundaries; task durability and review remain in the runtime."""
from pathlib import Path
import json

from .core import WorkbenchError, canonical

AGENT_KINDS = ("codex", "claude")
RESULT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "array", "items": {"type": "string"}},
        "changed_files": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "findings", "tests", "changed_files"]}
DEFAULT_CLAUDE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]


def command_profile(project, name):
    profile = project.get("profiles", {}).get(name)
    if not isinstance(profile, list) or not profile or not profile[0] or not all(
            isinstance(v, str) and "\x00" not in v for v in profile):
        raise WorkbenchError("Choose a preconfigured fixed-argv command profile")
    return profile


def configured_command(kind, project):
    executable = project.get(kind + "_command")
    if not isinstance(executable, list) or not executable or not executable[0] or not all(
            isinstance(v, str) and "\x00" not in v for v in executable):
        raise WorkbenchError(f"No valid {kind} executable argv configured for this project")
    return executable


def claude_policy(project):
    allowed = project.get("claude_allowed_tools", DEFAULT_CLAUDE_TOOLS)
    if not isinstance(allowed, list) or not allowed or len(allowed) > 50 or not all(
            isinstance(v, str) and v and not v.startswith("-") and "\x00" not in v and len(v) < 1000 for v in allowed):
        raise WorkbenchError("claude_allowed_tools must be a bounded operator-configured allowlist")
    turns = project.get("claude_max_turns", 20)
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 100:
        raise WorkbenchError("claude_max_turns must be between 1 and 100")
    tools = list(DEFAULT_CLAUDE_TOOLS)
    if any(v == "Bash" or v.startswith("Bash(") for v in allowed):
        raise WorkbenchError("Native Bash is disabled in the computational trial; use the restricted command adapter")
    return {"allowed": allowed, "tools": tools, "max_turns": turns}


def agent_command(kind, project, directory):
    executable = configured_command(kind, project)
    if kind == "codex":
        schema = directory / "result-schema.json"
        schema.write_text(canonical(RESULT_SCHEMA), encoding="utf-8")
        return [*executable, "exec", "--sandbox", "workspace-write", "--json",
                "--output-schema", str(schema), "--output-last-message", str(directory / "codex-result.json"), "-"]
    if kind == "claude":
        policy = claude_policy(project)
        restricted = directory / "restricted-mcp.json"
        mcp_config = str(restricted) if restricted.exists() else '{"mcpServers":{}}'
        if restricted.exists() and "Bash" in policy["tools"]:
            raise WorkbenchError("Restricted computational task cannot enable native Bash")
        allowed = [*policy["allowed"], "mcp__wb_runtime__run", "mcp__wb_runtime__get_run", "mcp__wb_runtime__terminate_run"] if restricted.exists() else policy["allowed"]
        # No bare mode: it would change subscription authentication to API-key use.
        # Restrict MCP and built-in tool discovery; permission rules are not an OS sandbox.
        return [*executable, "-p", "--output-format", "json", "--json-schema", canonical(RESULT_SCHEMA),
                *(["--restricted"] if restricted.exists() else []),
                "--permission-mode", "dontAsk", "--tools", ",".join(policy["tools"]),
                "--strict-mcp-config", "--mcp-config", mcp_config,
                *([] if restricted.exists() else ["--disallowedTools", "mcp__*"]), "--disable-slash-commands",
                "--allowedTools", *allowed, "--max-turns", str(policy["max_turns"])]
    raise WorkbenchError("Unsupported agent adapter")


def agent_result(kind, directory):
    path = directory / ("codex-result.json" if kind == "codex" else "claude-output.json")
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise WorkbenchError(f"{kind} structured output is missing or exceeds 1 MiB")
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as e:
        raise WorkbenchError(f"{kind} returned invalid JSON; inspect executor log") from e
    metadata = {}
    if kind == "claude":
        if not isinstance(wrapper, dict) or wrapper.get("is_error") or wrapper.get("permission_denials"):
            raise WorkbenchError("Claude Code reported an execution or permission failure; inspect executor log")
        if wrapper.get("subtype", "success") != "success":
            raise WorkbenchError("Claude Code did not report successful completion")
        report = wrapper.get("structured_output")
        session = wrapper.get("session_id")
        if isinstance(session, str) and len(session) <= 200:
            metadata["executor_session_id"] = session
    else:
        report = wrapper
    if not isinstance(report, dict) or set(report) != set(RESULT_SCHEMA["required"]) or not isinstance(report["summary"], str):
        raise WorkbenchError(f"{kind} result does not match the required schema")
    for key in ("findings", "tests", "changed_files"):
        if not isinstance(report[key], list) or not all(isinstance(v, str) for v in report[key]):
            raise WorkbenchError(f"{kind} result contains an invalid result field")
    if len(canonical(report).encode("utf-8")) > 256 * 1024:
        raise WorkbenchError(f"{kind} result exceeds 256 KiB")
    return report, metadata
