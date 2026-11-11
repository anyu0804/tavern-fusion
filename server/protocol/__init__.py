"""Transport layer: MCP JSON-RPC 2.0 over stdio."""

from __future__ import annotations

from .stdio import StdioMCPServer

__all__ = ["StdioMCPServer"]
