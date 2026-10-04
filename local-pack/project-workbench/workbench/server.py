from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .core import Runtime, WorkbenchError
from .mcp import MCP, MODERN_PROTOCOLS, PROTOCOLS


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, runtime):
        super().__init__(address, Handler)
        self.runtime = runtime
        self.mcp = MCP(runtime)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Malformed method/path can contain an unread body or credentials.
        # Log only known routes/methods, never the original request line.
        command = getattr(self, "command", None)
        if command not in ("GET", "POST", "HEAD", "OPTIONS"):
            print("HTTP [invalid-request]", file=sys.stderr)
            return
        try:
            path = urlsplit(getattr(self, "path", "") or "").path
        except ValueError:
            path = ""
        if path not in ("/", "/health", "/mcp", "/api/call", "/api/download"):
            path = "[unknown-route]"
        print("HTTP " + command + " " + path, file=sys.stderr)

    def send_bytes(self, code, body=b"", content_type="application/json"):
        self.send_response(code)
        if self.command == "POST" and not getattr(self, "body_consumed", False):
            # An unread request body must not be parsed as the next keep-alive request.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, code, value):
        self.send_bytes(code, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def allowed_origin(self):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self.send_json(403, {"error": "Invalid local host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed_hosts}:
            self.send_json(403, {"error": "Origin is not allowed"})
            return False
        return True

    def authenticated(self):
        if not self.allowed_origin():
            return False
        token = self.server.runtime.config.get("auth_token", "")
        given = self.headers.get("Authorization", "")
        if len(token) < 32 or not hmac.compare_digest(given, "Bearer " + token):
            self.send_json(401, {"error": "Local access token required"})
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/health"):
            if not self.allowed_origin():
                return
            if path == "/health":
                self.send_json(200, {"service": "project-workbench", "version": __version__, "events_supported": True})
            else:
                ui = Path(__file__).resolve().parents[1] / "ui" / "tasks.html"
                self.send_bytes(200, ui.read_bytes(), "text/html; charset=utf-8")
            return
        if not self.authenticated():
            return
        if path == "/mcp":
            self.send_bytes(405)
            return
        if path == "/api/download":
            try:
                q = parse_qs(urlsplit(self.path).query)
                p, a = self.server.runtime.artifact_path(q["task_id"][0], int(q["artifact_id"][0]))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(p.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with p.open("rb") as f:
                    for b in iter(lambda: f.read(1024 * 1024), b""):
                        self.wfile.write(b)
            except (WorkbenchError, KeyError, ValueError) as e:
                self.send_json(400, {"error": str(e)})
            return
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        self.body_consumed = False
        if not self.authenticated():
            return
        path = urlsplit(self.path).path
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
                raise WorkbenchError("One Content-Length and no Transfer-Encoding required")
            length = int(lengths[0])
            if not 0 < length <= 1024 * 1024:
                raise WorkbenchError("Invalid request size")
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise WorkbenchError("JSON content type required")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise WorkbenchError("Incomplete request body")
            self.body_consumed = True
            body = json.loads(raw)
            if path == "/mcp":
                version = self.headers.get("MCP-Protocol-Version", "2025-03-26")
                if version not in PROTOCOLS + MODERN_PROTOCOLS:
                    raise WorkbenchError("Unsupported MCP protocol version; this spike has no MCP Events/2.0")
                result = self.server.mcp.handle(body)
                if result is None:
                    self.send_bytes(202)
                else:
                    self.send_json(200, result)
            elif path == "/api/call":
                self.send_json(200, {"data": self.server.mcp.call(body["name"], body.get("arguments", {}))})
            else:
                self.send_json(404, {"error": "Not found"})
        except (WorkbenchError, ValueError, KeyError, TypeError) as e:
            self.send_json(400, {"error": str(e)[:2000]})
        except Exception as e:
            print(f"Local operation failed: {type(e).__name__}", file=sys.stderr)
            self.send_json(500, {"error": "Local operation failed"})


def stdio(runtime):
    # MCP wire is UTF-8 even on Windows with a legacy console/pipe code page.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    mcp = MCP(runtime)
    for line in sys.stdin.buffer:
        try:
            if len(line) > 1024 * 1024:
                raise ValueError("Request exceeds size limit")
            result = mcp.handle(json.loads(line))
        except (ValueError, UnicodeError):
            result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    runtime = Runtime(args.config)
    # Long-lived facades deliver MCP Events from the persistent outbox (independent of any open widget).
    runtime.events.start_dispatcher()
    if args.stdio:
        stdio(runtime)
    else:
        port = args.port if args.port is not None else runtime.config.get("port", 8765)
        if len(runtime.config.get("auth_token", "")) < 32:
            raise WorkbenchError("Configure a strong local token before running HTTP")
        server = Server(("127.0.0.1", port), runtime)
        print(f"Project Workbench: http://127.0.0.1:{server.server_address[1]}/", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
