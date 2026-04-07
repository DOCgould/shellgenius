"""
OpenClaw Skill Server — lightweight HTTP server for tool-call dispatch.

This runs as a local sidecar process that OpenClaw calls into when the LLM
invokes a ShellGenius tool. It uses only stdlib (http.server + json) so
there are zero external dependencies.

Protocol:
    POST /tool-call
    Body: {"name": "shell_compose", "parameters": {"description": "..."}}
    Response: {"result": {...}}

    GET /health
    Response: {"status": "ok", "version": "0.1.0"}

    GET /manifest
    Response: <full skill manifest JSON>
"""

from __future__ import annotations

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

from shellgenius.agent import ShellGeniusAgent, AgentContext
from shellgenius.openclaw.skill import generate_manifest, SKILL_VERSION


DEFAULT_PORT = 9747


class SkillHandler(BaseHTTPRequestHandler):
    """HTTP handler for OpenClaw skill server."""

    agent: ShellGeniusAgent  # set at class level before serving

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response({"status": "ok", "version": SKILL_VERSION})
        elif self.path == "/manifest":
            self._json_response(generate_manifest())
        elif self.path == "/tools":
            self._json_response({"tools": self.agent.as_tools()})
        else:
            self._json_response({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path == "/tool-call":
            body = self._read_body()
            if body is None:
                return
            tool_name = body.get("name", "")
            params = body.get("parameters", {})
            result = self.agent.handle_tool_call(tool_name, params)
            self._json_response({"result": result})
        elif self.path == "/setup":
            info = self.agent.setup()
            self._json_response({"result": info})
        else:
            self._json_response({"error": "not found"}, status=404)

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            self._json_response({"error": f"Invalid JSON: {e}"}, status=400)
            return None

    def _json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Quieter logging
        if "/health" not in (args[0] if args else ""):
            sys.stderr.write(f"[ShellGenius] {format % args}\n")


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    """Start the skill server."""
    agent = ShellGeniusAgent()
    setup_info = agent.setup()
    SkillHandler.agent = agent

    server = HTTPServer((host, port), SkillHandler)
    print(f"ShellGenius skill server running on {host}:{port}")
    print(f"  Shell: {setup_info['shell']} ({setup_info['version']})")
    print(f"  Tools: {setup_info['tools_available']} detected")
    print(f"  Modern: {', '.join(setup_info['modern_tools']) or 'none'}")
    print(f"  CWD: {setup_info['cwd']}")
    print()
    print(f"Endpoints:")
    print(f"  GET  /health    — health check")
    print(f"  GET  /manifest  — skill manifest")
    print(f"  GET  /tools     — available tools")
    print(f"  POST /tool-call — invoke a tool")
    print(f"  POST /setup     — reinitialize agent")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ShellGenius OpenClaw Skill Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()
    serve(port=args.port, host=args.host)
