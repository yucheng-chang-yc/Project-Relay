from pathlib import Path
import hashlib
import json
import time
from urllib.parse import urlsplit

from . import __version__
from .core import WorkbenchError
from .events import EventsError

MODERN_PROTOCOLS = ("2026-07-28",)
# Caching hints (ttlMs, cacheScope) for cacheable MCP 2026-07-28 results. UI templates are versioned by URI and hold
# no user data; artifact resources are user data and are never cached.
UI_TTL_MS = 300000
CACHE_HINTS = {"server/discover": (300000, "public"), "tools/list": (300000, "public"),
               "resources/list": (300000, "public"), "resources/read": None}
DIAGNOSED_METHODS = ("initialize", "server/discover", "events/list", "events/subscribe", "events/unsubscribe")


def _redact(value, key=None):
    """Remove signing secrets, auth material and callback URL paths/queries from diagnostic copies."""
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and key is not None:
        lowered = key.lower()
        if lowered in ("secret", "authorization", "token", "auth_token", "api_key", "password", "approval_token"):
            return "[redacted]"
        if lowered == "url":
            try:
                parsed = urlsplit(value)
                return f"{parsed.scheme}://{parsed.hostname}/[path-and-query-redacted]" if parsed.hostname else "[redacted]"
            except ValueError:
                return "[redacted]"
    return value
INSTRUCTIONS = ("Resolve a registered project. Preserve the task goal/constraints/acceptance. Reuse idempotency keys after "
                "disconnects. Read results before review. Commit/push only within actual user authority. MCP 2.0 clients can "
                "subscribe to the task.finished event (webhook); its data carries only task_id, terminal status and "
                "result_sha256, and the result is read with get_task_result. To bring one local file into the "
                "conversation without a project, call request_file_snapshot with its exact absolute path; the user "
                "approves that file once in the inline conversation card, then read it with read_file_snapshot_bytes. Keep ordinary file exchange in the conversation; show open_workbench only when a task view is useful. The transfer probe is developer diagnostics, not a user setup or transfer step.")
UI_URI = "ui://project-workbench/tasks-v3.html"
TRANSFER_URI = "ui://project-workbench/transfer-v3.html"
FILE_URI = "ui://project-workbench/file-snapshot-v3.html"
UI_FILES = {UI_URI: ("tasks.html", "Project Relay Tasks", "Local project task status, results and manual review handoff."),
            TRANSFER_URI: ("transfer-spike.html", "Project Relay Transfer Diagnostics", "Developer-only host file transport checks; not a routine user workflow."),
            FILE_URI: ("file-snapshot.html", "Project Relay File Request",
                       "Shows one requested local file path and lets the user approve or deny a single read-only snapshot.")}
# Keep older saved template pointers resolvable; new tool calls use the v3 UI.
UI_FILES.update({"ui://project-workbench/tasks-v2.html": UI_FILES[UI_URI],
                 "ui://project-workbench/transfer-v2.html": UI_FILES[TRANSFER_URI],
                 "ui://project-workbench/file-snapshot-v2.html": UI_FILES[FILE_URI],
                 "ui://project-workbench/tasks-v0.1.0.html": UI_FILES[UI_URI],
                 "ui://project-workbench/transfer-spike-v1.html": UI_FILES[TRANSFER_URI],
                 "ui://project-workbench/file-snapshot-v1.html": UI_FILES[FILE_URI]})
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def string(maximum=10000):
    return {"type": "string", "maxLength": maximum}


def array(item=None, maximum=100):
    return {"type": "array", "items": item or string(), "maxItems": maximum}


PROJECT = {"project_id": string(64)}
TASK = {"task_id": string(64)}
WINDOW = {"offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 262144}}
TASK_FIELDS = {**PROJECT, "goal": string(), "constraints": array(string(), 30),
               "acceptance": array(string(), 30), "artifacts": array(string(1000), 30),
               "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
               "idempotency_key": string(128)}
TASK_FIELDS.update({"inputs": array({"type": "object", "properties": {"input_id": string(64), "destination": string(1000)},
    "required": ["input_id", "destination"], "additionalProperties": False}, 30),
    "parent_task_id": string(64), "parent_result_sha256": string(64), "compute_runtime": {"type": "boolean"}})


def definition(name, description, properties=None, required=(), readonly=True, ui=False):
    d = {"name": name, "title": name.replace("_", " ").title(), "description": description,
         "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required),
                         "additionalProperties": False},
         "outputSchema": {"type": "object", "properties": {"data": {}}, "required": ["data"],
                          "additionalProperties": False},
         "annotations": {"readOnlyHint": readonly, "destructiveHint": not readonly,
                          "idempotentHint": readonly, "openWorldHint": name in
                          ("start_codex_task", "start_claude_task", "start_command_task", "git_push")}}
    if ui:
        d["_meta"] = {"ui": {"resourceUri": UI_URI}, "openai/outputTemplate": UI_URI}
    return d


TOOLS = [
    definition("list_projects", "List locally registered project roots and fixed command profiles."),
    definition("get_project_capabilities", "Inspect implemented features, sanitized executor policy and limits. Does not launch models; authentication and model availability remain unknown.",
               PROJECT, ("project_id",)),
    definition("preflight_task", "Check known local task prerequisites without creating tasks. Eligible describes local configuration; authentication/model success remain unverified. Rechecked at execution/integration.",
               {**PROJECT, "executor": {"type": "string", "enum": ["command", "codex", "claude"]},
                "required_capabilities": array(string(64), 30), "profile": string(64)}, ("project_id", "executor")),
    definition("list_directory", "List authorized direct children and metadata with bounded pages. Reuse next_cursor; changed listings invalidate cursors. Git metadata and escaping aliases are filtered.",
               {**PROJECT, "path": string(1000), "cursor": string(80),
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ("project_id",)),
    definition("stat_file", "Inspect authorized file/directory metadata; optionally hash a regular file up to 500 MiB. No file content is returned.",
               {**PROJECT, "path": string(1000), "include_sha256": {"type": "boolean"}}, ("project_id", "path")),
    definition("read_file", "Read a UTF-8 preview in bounded byte windows. Complete a trailing character using at most 3 extra bytes; reuse next_offset. Invalid UTF-8 is marked lossy.",
               {**PROJECT, "path": string(1000), **WINDOW}, ("project_id", "path")),
    definition("search_files", "Find literal text in bounded project files; skip dependency and Git metadata directories.",
               {**PROJECT, "query": string(500), "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
               ("project_id", "query")),
    definition("write_file", "Write bounded UTF-8 text with exact existing SHA-256; empty expected hash means create.",
               {**PROJECT, "path": string(1000), "text": string(262144), "expected_sha256": string(64)},
               ("project_id", "path", "text", "expected_sha256"), readonly=False),
    definition("git_status", "Inspect actual HEAD, branch, dirty working tree and configured remote names.", PROJECT, ("project_id",)),
    definition("git_diff", "Read bounded tracked/staged diff against HEAD. Untracked files are reported through status.", PROJECT, ("project_id",)),
    definition("start_command_task", "Start a bounded fixed-argv operator-configured command profile. Reuse the same idempotency key after connection loss.",
               {**TASK_FIELDS, "profile": string(64)},
               ("project_id", "goal", "acceptance", "idempotency_key", "profile"), readonly=False),
    definition("start_codex_task", "Launch Codex CLI in a detached Git worktree. Persist original contract, JSONL logs, structured result and diff; no automatic commit or push.",
               TASK_FIELDS, ("project_id", "goal", "acceptance", "idempotency_key"), readonly=False),
    definition("start_claude_task", "Launch Claude Code in a detached Git worktree using existing local CLI authentication, configured tools and dontAsk permissions. Persist structured result and diff for separate review.",
               TASK_FIELDS, ("project_id", "goal", "acceptance", "idempotency_key"), readonly=False),
    definition("list_tasks", "Read durable task statuses; execution completion and review are distinct.",
               {**PROJECT, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    definition("get_task", "Read one task's persisted contract and current execution/review state.", TASK, ("task_id",)),
    definition("get_task_result", "Retrieve a terminal task result, actual findings, artifacts, error and immutable result hash.", TASK, ("task_id",)),
    definition("get_task_log", "Read executor output in bounded byte windows; logs are untrusted task data.", {**TASK, **WINDOW}, ("task_id",)),
    definition("read_task_artifact", "Read a hash-verified artifact in bounded text windows. Binary downloads are on the authenticated local dashboard.",
               {**TASK, "artifact_id": {"type": "integer", "minimum": 0}, **WINDOW}, ("task_id", "artifact_id")),
    definition("cancel_task", "Request cancellation of an owned worker's process tree; terminal tasks are not relaunched.", TASK, ("task_id",), readonly=False),
    definition("mark_task_reviewed", "Record review after result retrieval. Executor completion alone never implies acceptance.",
               {**TASK, "expected_result_sha256": string(64), "verdict": {"type": "string", "enum": ["accept_changes", "revise"]},
                "note": string(5000)}, ("task_id", "expected_result_sha256", "verdict", "note"), readonly=False),
    definition("apply_task_changes", "Apply the exact accepted Codex or Claude Code diff only if primary HEAD/tree and worktree evidence remain unchanged.",
               {**TASK, "expected_diff_sha256": string(64)}, ("task_id", "expected_diff_sha256"), readonly=False),
    definition("git_commit", "Commit only explicitly authorized file paths at exact HEAD. Refuse pre-existing staged changes.",
               {**PROJECT, "paths": array(string(1000), 100), "message": string(500), "expected_head": string(64)},
               ("project_id", "paths", "message", "expected_head"), readonly=False),
    definition("git_push", "Push the current named branch to a configured remote without force. Use only with existing explicit user authority; verify exact remote HEAD.",
               {**PROJECT, "remote": string(100), "branch": string(200), "expected_head": string(64)},
               ("project_id", "remote", "branch", "expected_head"), readonly=False),
    definition("open_workbench", "Show the persistent task view with automatic status refresh and a manual ChatGPT review handoff.", ui=True),
]

FILE_PARAM = {"type": "object", "properties": {"download_url": string(10000), "file_id": string(256),
    "mime_type": string(256), "file_name": string(1000)}, "required": ["download_url", "file_id"], "additionalProperties": False}
TOOLS += [
    definition("stage_project_input", "Snapshot an explicitly selected authorized primary project file, including uncommitted bytes; does not import other dirty files.",
        {**PROJECT, "path": string(1000), "expected_sha256": string(64), "idempotency_key": string(128)},
        ("project_id", "path", "expected_sha256", "idempotency_key"), readonly=False),
    definition("stage_binary_input", "Stage exact host-file bytes in the authorized project input store. Supply independently computed SHA/size; signed URLs are never persisted. Host compatibility remains unverified.",
        {**PROJECT, "file": FILE_PARAM, "expected_sha256": string(64), "expected_size_bytes": {"type": "integer", "minimum": 0, "maximum": 20971520}, "idempotency_key": string(128)},
        ("project_id", "file", "expected_sha256", "expected_size_bytes", "idempotency_key"), readonly=False),
    definition("stage_prior_artifact", "Copy a selected exact same-project prior artifact into durable inputs without a Human download/upload. Does not imply artifact trust or acceptance.",
        {**PROJECT, **TASK, "artifact_id": {"type": "integer", "minimum": 0}, "expected_result_sha256": string(64), "expected_sha256": string(64), "idempotency_key": string(128)},
        ("project_id", "task_id", "artifact_id", "expected_result_sha256", "expected_sha256", "idempotency_key"), readonly=False),
    definition("get_task_snapshot", "Get exact input snapshot, Work Unit navigation identity and sealed predecessor identities. These do not own project state or acceptance.", TASK, ("task_id",)),
    definition("get_work_unit", "Navigate trial tasks/reviews/artifacts for a substantive question. This does not own project truth, Stage/Gate or acceptance.", {"work_unit_id": string(64)}, ("work_unit_id",)),
    definition("get_artifact_resource", "Return a hash-bound MCP binary resource link. A resource link does not prove ChatGPT file materialization.",
        {**TASK, "artifact_id": {"type": "integer", "minimum": 0}, "expected_sha256": string(64)}, ("task_id", "artifact_id", "expected_sha256")),
    definition("read_artifact_chunk", "Widget transport only: read exact binary windows; bytes are hidden metadata, never UTF-8 previews. SHA round-trip is required.",
        {**TASK, "artifact_id": {"type": "integer", "minimum": 0}, "expected_sha256": string(64), **WINDOW}, ("task_id", "artifact_id", "expected_sha256")),
    definition("read_artifact_bytes", "Read a hash-verified artifact byte window as Base64 for model-side reconstruction. Call in bounded windows, decode bytes in a code tool, verify each chunk SHA and the final whole-file SHA; never treat metadata alone as file access.",
        {**TASK, "artifact_id": {"type": "integer", "minimum": 0}, "expected_sha256": string(64), **WINDOW}, ("task_id", "artifact_id", "expected_sha256")),
    definition("record_review_object", "Append a hash-bound evidence Review Object. PASS is not Human acceptance, release, closure or automatic apply.",
        {**TASK, "expected_result_sha256": string(64), "expected_snapshot_sha256": string(64), "verdict": {"type": "string", "enum": ["PASS", "REVISE"]},
         "note": string(5000), "idempotency_key": string(128)}, ("task_id", "expected_result_sha256", "expected_snapshot_sha256", "verdict", "note", "idempotency_key"), readonly=False),
    definition("get_review_object", "Read historical immutable review content and its current evidence validity; invalidation never rewrites history.", {"review_id": string(80)}, ("review_id",)),
    definition("open_transfer_spike", "Developer diagnostics only. Open only when explicitly asked to diagnose host file transport. Routine file exchange uses stage/read tools directly; this card is not required. Programmatic upload and next-turn model access remain separately verified."),
    definition("request_file_snapshot", "Ask the user to authorize one read-only snapshot of one local file by exact absolute path (e.g. C:\\Users\\name\\Downloads\\data.zip). No project is needed. Nothing is read until the user approves the exact path in the Project Relay card. Retrying with the same idempotency_key returns the same request or snapshot; a new key asks again.",
        {"path": string(1000), "idempotency_key": string(128)}, ("path", "idempotency_key")),
    definition("prepare_file_snapshot_approval", "Widget only: return the request's current status and, while it awaits approval, a fresh single-use approval token for the card.",
        {"request_id": string(64)}, ("request_id",)),
    definition("approve_file_snapshot", "Widget only: record the user's approve/deny decision for one snapshot request using the card's single-use approval token, then take the snapshot.",
        {"request_id": string(64), "approval_token": string(128), "decision": {"type": "string", "enum": ["approve", "deny"]}},
        ("request_id", "approval_token", "decision")),
    definition("get_file_snapshot", "Read the status of a file snapshot request or snapshot: awaiting approval, denied, failed, ready (with snapshot_id, size, SHA-256, expiry) or expired.",
        {"request_id": string(64), "snapshot_id": string(64)}),
    definition("read_file_snapshot_bytes", "Read an approved file snapshot as Base64 byte windows (max 262144 bytes). Start at offset 0 and follow next_offset until eof; decode each window in a code tool, check chunk_sha256, then verify the whole-file SHA-256. No further approval is needed for the same snapshot.",
        {"snapshot_id": string(64), "expected_sha256": string(64), **WINDOW}, ("snapshot_id", "expected_sha256")),
]
next(t for t in TOOLS if t["name"] == "stage_binary_input")["_meta"] = {"openai/fileParams": ["file"]}
next(t for t in TOOLS if t["name"] == "stage_binary_input")["annotations"]["openWorldHint"] = True
next(t for t in TOOLS if t["name"] == "read_artifact_chunk")["_meta"] = {"ui": {"visibility": ["app"]}, "openai/visibility": "private"}
next(t for t in TOOLS if t["name"] == "open_transfer_spike")["_meta"] = {"ui": {"resourceUri": TRANSFER_URI}, "openai/outputTemplate": TRANSFER_URI}
next(t for t in TOOLS if t["name"] == "request_file_snapshot")["_meta"] = {"ui": {"resourceUri": FILE_URI}, "openai/outputTemplate": FILE_URI,
    "openai/toolInvocation/invoking": "Preparing file approval", "openai/toolInvocation/invoked": "Awaiting your approval"}
next(t for t in TOOLS if t["name"] == "open_workbench")["title"] = "Open Project Relay"
next(t for t in TOOLS if t["name"] == "open_transfer_spike")["title"] = "Developer Transfer Diagnostics"
# The approving tool is callable only from the widget; the model never receives the approval token either.
for _name in ("prepare_file_snapshot_approval", "approve_file_snapshot"):
    next(t for t in TOOLS if t["name"] == _name)["_meta"] = {"ui": {"visibility": ["app"]}, "openai/visibility": "private",
                                                             "openai/widgetAccessible": True}


def validate(value, schema, path="arguments"):
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": isinstance(value, int) and not isinstance(value, bool),
             "boolean": isinstance(value, bool)}
    if kind and not valid.get(kind, False):
        raise WorkbenchError(f"Invalid {path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise WorkbenchError(f"Invalid {path}: unrecognized value")
    if kind == "object":
        props = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            raise WorkbenchError(f"Missing required field in {path}")
        if schema.get("additionalProperties") is False and any(k not in props for k in value):
            raise WorkbenchError(f"Unexpected field in {path}")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], path + "." + k)
    if kind == "array":
        if len(value) > schema.get("maxItems", 1000):
            raise WorkbenchError(f"Too many items in {path}")
        for i, v in enumerate(value):
            validate(v, schema.get("items", {}), f"{path}[{i}]")
    if kind == "string" and len(value) > schema.get("maxLength", 1000000):
        raise WorkbenchError(f"Text exceeds limit in {path}")
    if kind == "integer" and not schema.get("minimum", -1e30) <= value <= schema.get("maximum", 1e30):
        raise WorkbenchError(f"Number outside limits in {path}")


class MCP:
    def __init__(self, runtime):
        self.runtime = runtime

    def call(self, name, arguments):
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            raise WorkbenchError("Unknown tool")
        validate(arguments, tool["inputSchema"])
        if name in ("start_command_task", "start_codex_task", "start_claude_task"):
            args = arguments.copy()
            project_id = args.pop("project_id")
            key = args.pop("idempotency_key")
            kind = {"start_command_task": "command", "start_codex_task": "codex", "start_claude_task": "claude"}[name]
            return self.runtime.start_task(project_id, kind, args, key)
        if name == "open_workbench":
            return {"projects": self.runtime.list_projects(), "tasks": self.runtime.list_tasks(),
                    "events_supported": True, "handoff": "manual_or_task_finished_event"}
        if name == "open_transfer_spike":
            return {"status": "host_route_unverified", "projects": self.runtime.list_projects(), "tasks": self.runtime.list_tasks(), "max_bytes": 20971520}
        methods = {"stage_binary_input": "stage_binary_input", "stage_prior_artifact": "stage_prior_artifact", "stage_project_input": "stage_project_input", "get_work_unit": "get_work_unit",
                   "get_task_snapshot": "snapshot", "get_artifact_resource": "artifact_resource",
                   "read_artifact_chunk": "artifact_chunk", "read_artifact_bytes": "artifact_chunk",
                   "record_review_object": "record_review_object", "get_review_object": "get_review_object"}
        if name in methods:
            return getattr(self.runtime.loop, methods[name])(**arguments)
        files = {"request_file_snapshot": "request", "prepare_file_snapshot_approval": "prepare", "approve_file_snapshot": "approve",
                 "get_file_snapshot": "get", "read_file_snapshot_bytes": "read"}
        if name in files:
            return getattr(self.runtime.files, files[name])(**arguments)
        return getattr(self.runtime, name)(**arguments)

    def resource(self, uri=UI_URI):
        ui = Path(__file__).resolve().parents[1] / "ui" / UI_FILES[uri][0]
        return {"uri": uri, "mimeType": "text/html;profile=mcp-app", "text": ui.read_text(encoding="utf-8"),
                "_meta": {"ui": {"prefersBorder": True, "csp": {"connectDomains": ["https://" + host for host in self.runtime.config.get("binary_transfer", {}).get("allowed_download_hosts", [])
                    if isinstance(host, str) and host and all(c.isalnum() or c in '.-' for c in host)] if uri == TRANSFER_URI else [], "resourceDomains": []}},
                          "openai/widgetDescription": UI_FILES[uri][2]}}

    def handle(self, message):
        response = self._handle(message)
        try:
            self._diagnose(message, response)
        except Exception:
            pass  # Diagnostics must never change protocol behaviour.
        return response

    def _diagnose(self, message, response):
        """Append one redacted line per MCP request to <data_dir>/mcp-discovery.log.

        Every request records method, protocol version, client identity and request _meta key names.
        initialize, server/discover and events/* also record the redacted request and full response.
        tools/call records the tool name, isError and result _meta key names (never values or arguments).
        resources/read records the URI and, per content, MIME type, size, SHA-256 and resource _meta, not the body.
        Secrets and callback URL paths/queries are removed.
        """
        if not isinstance(message, dict):
            return
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        record = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "method": method, "id": message.get("id"),
                  "protocolVersion": meta.get("io.modelcontextprotocol/protocolVersion") or params.get("protocolVersion"),
                  "clientInfo": meta.get("io.modelcontextprotocol/clientInfo") or params.get("clientInfo"),
                  "request_meta_keys": sorted(meta)}
        result = response.get("result") if isinstance(response, dict) and isinstance(response.get("result"), dict) else None
        if method == "tools/call":
            record["tool"] = params.get("name")
            if result is not None:
                record["isError"] = bool(result.get("isError"))
                record["result_meta_keys"] = sorted(result.get("_meta") or {})
        if method == "resources/read":
            record["uri"] = params.get("uri")
            if result is not None:
                record["contents"] = [{"uri": c.get("uri"), "mimeType": c.get("mimeType"),
                                       "text_bytes": len(c["text"].encode("utf-8")) if isinstance(c.get("text"), str) else None,
                                       "text_sha256": hashlib.sha256(c["text"].encode("utf-8")).hexdigest() if isinstance(c.get("text"), str) else None,
                                       "blob_chars": len(c["blob"]) if isinstance(c.get("blob"), str) else None,
                                       "_meta": c.get("_meta")} for c in result.get("contents", []) if isinstance(c, dict)]
        if isinstance(response, dict) and "error" in response:
            record["error"] = response["error"]
        if method in DIAGNOSED_METHODS:
            record["request"] = _redact(params)
            record["response"] = _redact(response)
        path = self.runtime.data / "mcp-discovery.log"
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        ident = message.get("id")
        method = message["method"]
        params = message.get("params", {})
        if "id" not in message:
            return None
        # MCP 2.0 (SEP-2575): stateless requests carry their protocol version in params._meta.
        # Requests without it keep the legacy initialize-based behaviour unchanged.
        meta = params.get("_meta") if isinstance(params, dict) else None
        requested = meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
        modern = requested is not None or method == "server/discover"
        if requested is not None and requested not in MODERN_PROTOCOLS:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32022, "message": "Unsupported protocol version",
                    "data": {"supported": list(MODERN_PROTOCOLS), "requested": requested}}}
        try:
            if method == "server/discover":
                result = {"supportedVersions": list(MODERN_PROTOCOLS),
                          "capabilities": {"tools": {}, "resources": {}, "events": {}},
                          "instructions": INSTRUCTIONS}
            elif method == "events/list":
                result = self.runtime.events.list_events(params)
            elif method == "events/subscribe":
                result = self.runtime.events.subscribe(params)
            elif method == "events/unsubscribe":
                result = self.runtime.events.unsubscribe(params)
            elif method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in PROTOCOLS else PROTOCOLS[0]
                result = {"protocolVersion": version, "serverInfo": {"name": "project-workbench", "version": __version__},
                          "capabilities": {"tools": {}, "resources": {}},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    data = self.call(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps({"data": data}, ensure_ascii=False)}],
                              "structuredContent": {"data": data}}
                    if params.get("name") == "read_artifact_chunk":
                        encoded = data.pop("base64")
                        result = {"content": [{"type": "text", "text": "Verified artifact byte window"}],
                                  "structuredContent": {"data": data}, "_meta": {"bytes_base64": encoded}}
                    elif params.get("name") == "read_file_snapshot_bytes":
                        result = {"content": [{"type": "text", "text": "File snapshot byte window; decode structuredContent.data.base64 in a code tool"}],
                                  "structuredContent": {"data": data}}
                    elif params.get("name") == "read_artifact_bytes":
                        result = {"content": [{"type": "text", "text": "Verified artifact byte window; decode structuredContent.data.base64 in a code tool"}],
                                  "structuredContent": {"data": data}}
                    elif params.get("name") == "get_artifact_resource":
                        result["content"].append({"type": "resource_link", "uri": data["uri"], "name": data["name"], "mimeType": data["mimeType"]})
                except WorkbenchError as e:
                    result = {"content": [{"type": "text", "text": str(e)}],
                              "structuredContent": {"data": {"error": str(e)}}, "isError": True}
            elif method == "resources/list":
                result = {"resources": [{"uri": uri, "name": item[1], "mimeType": "text/html;profile=mcp-app"}
                    for uri, item in UI_FILES.items()]}
            elif method == "resources/read":
                uri = params.get("uri")
                result = {"contents": [self.resource(uri) if uri in UI_FILES else self.runtime.loop.read_resource(uri)]}
            else:
                return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
            if modern:
                result = {"resultType": "complete", **result}
                result["_meta"] = {**result.get("_meta", {}), "io.modelcontextprotocol/serverInfo":
                                   {"name": "project-workbench", "version": __version__}}
                if method in CACHE_HINTS:
                    # MCP 2026-07-28 caching: these results MUST carry ttlMs/cacheScope. Without them a client
                    # treats every result as immediately stale; ChatGPT then could not serve the widget template.
                    ui = method == "resources/read" and params.get("uri") in UI_FILES
                    result["ttlMs"], result["cacheScope"] = CACHE_HINTS[method] if method != "resources/read" else (
                        (UI_TTL_MS, "public") if ui else (0, "private"))
            return {"jsonrpc": "2.0", "id": ident, "result": result}
        except EventsError as e:
            error = {"code": e.code, "message": str(e)[:2000]}
            if e.data is not None:
                error["data"] = e.data
            return {"jsonrpc": "2.0", "id": ident, "error": error}
        except (WorkbenchError, TypeError, ValueError, KeyError) as e:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": str(e)[:2000]}}
        except Exception:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": "Internal operation failed; inspect local runtime logs"}}
