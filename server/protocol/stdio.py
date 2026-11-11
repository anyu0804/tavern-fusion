"""MCP JSON-RPC 2.0 stdio transport (`StdioMCPServer`).

Speaks Model Context Protocol (`2024-11-05` / `2025-03-26`) over stdin/stdout with both
newline-delimited JSON and `Content-Length` framing. Tool execution is delegated to the
`server.tools` registry with the runtime context bound as the first handler argument.
"""

from __future__ import annotations

from functools import partial
import json
import sys
from typing import Any, Callable

from ..runtime.context import RuntimeContext
from ..tools import MCP_TOOL_SCHEMAS, TOOL_REGISTRY, ToolSpec


class StdioMCPServer:
    """Pure-Python MCP JSON-RPC 2.0 Server supporting newline & Content-Length framing."""

    def __init__(self, runtime: RuntimeContext, tools: dict[str, ToolSpec] | None = None) -> None:
        self.runtime = runtime
        self._tools = tools if tools is not None else TOOL_REGISTRY
        self._tool_schemas: list[dict[str, Any]] = (
            MCP_TOOL_SCHEMAS if tools is None else [spec.schema for spec in tools.values()]
        )
        # name -> callable(args); kept as an attribute for backwards compatibility.
        self._dispatch_map: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            name: partial(spec.handler, runtime) for name, spec in self._tools.items()
        }

    def handle_rpc(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if req_id is None:
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion", "2024-11-05")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "st-antigravity-mcp", "version": "3.0.3-preview"},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            # Always return the full tool catalog so parallel engineering conversations
            # never lose tool visibility; Switch B tool blocking is enforced per-conversation
            # via `rp_conv_id == conversationId` in `hooks/pre_tool_guard.py`.
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._tool_schemas}}

        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

        if method == "tools/call":
            self.runtime.refresh_data_root()
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            fn = self._dispatch_map.get(tool_name)
            if fn is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            try:
                out = fn(tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error executing {tool_name}: {exc}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def serve_forever(self) -> None:
        stdin_bin = sys.stdin.buffer
        stdout_bin = sys.stdout.buffer

        while True:
            first_line = stdin_bin.readline()
            if not first_line:
                break
            stripped = first_line.strip()
            if not stripped:
                continue

            use_content_length = False
            if stripped.lower().startswith(b"content-length:"):
                use_content_length = True
                length = int(stripped.split(b":", 1)[1].strip())
                while True:
                    hdr = stdin_bin.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                raw_body = stdin_bin.read(length)
            else:
                raw_body = stripped

            try:
                req_obj = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            resp = self.handle_rpc(req_obj)
            if resp is not None:
                encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                if use_content_length:
                    stdout_bin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
                else:
                    stdout_bin.write(encoded + b"\n")
                stdout_bin.flush()
