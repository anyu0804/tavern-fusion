#!/usr/bin/env python3
"""Zero-Dependency Python MCP Server & Sidecar Control Plane entry point (`mcp_server.py`).

Speaks standard Model Context Protocol (MCP `2024-11-05` / `2025-03-26`) JSON-RPC 2.0
over `stdin`/`stdout` (supporting both newline-delimited JSON and Content-Length framing),
plus `--cli <tool_name>` and `--sidecar-op <op_name>` modes used by Antigravity Lifecycle
Hooks (`hooks/*.py`) and the permanent `Tavern Stage Sidecar` (`sidecars/tavern_stage/`).

This module is only the assembly + CLI entry. The implementation lives in:
- `server/protocol/stdio.py`   JSON-RPC stdio transport (`StdioMCPServer`)
- `server/runtime/`            paths, settings, ST root resolution, `SessionStore`
- `server/tools/`              the 14 MCP tools + sidecar ops, registry name -> (schema, handler)
- `server/policy/`             policy gate + minor-risk gate
- `server/prompt/`             macros, presets, regex pipeline, prompt frame compiler
`TavernFusionRuntime`, `StdioMCPServer`, `MCP_TOOL_SCHEMAS` and `FIRST_RUN_NOTICE*` stay
importable from here for backwards compatibility.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from server.lore_engine import NormalizedLoreEntry
from server.protocol.stdio import StdioMCPServer
from server.runtime import (  # noqa: F401  (re-exported for backwards-compatible imports)
    DEFAULT_STATE_DIR,
    FIRST_RUN_NOTICE,
    FIRST_RUN_NOTICE_V1,
    FIRST_RUN_NOTICE_VERSION,
    RuntimeContext,
    SessionStore,
)
from server.tools import MCP_TOOL_SCHEMAS, TOOL_REGISTRY, ToolSpec, call_tool  # noqa: F401
from server.tools import _common, stage


class TavernFusionRuntime(RuntimeContext):
    """Stateful coordinator bridging Antigravity, MCP tools, Hooks, Sidecar, and SillyTavern disk data.

    Paths, settings and session state come from `RuntimeContext` / `SessionStore`
    (`server/runtime/`); tool implementations live in `server/tools/`. The methods below are
    thin compatibility wrappers so existing callers (`runtime.tool_st_commit_turn(...)`,
    `runtime.handle_sidecar_op(...)`) keep working unchanged.
    """

    BYOK_SUPPORTED_ACTIONS = stage.BYOK_SUPPORTED_ACTIONS

    def __init__(self, st_data_root: str | Path | None = None, state_dir: str | Path | None = None) -> None:
        super().__init__(st_data_root=st_data_root, state_dir=state_dir)
        # The long-lived server opens the lore vector cache eagerly (as before the refactor).
        _ = self.lore_engine

    # -- shared helpers ------------------------------------------------
    def find_character_card_path(self, character_query: str) -> Path:
        return _common.find_character_card_path(self, character_query)

    def collect_merged_lore_entries(
        self,
        card_v3: dict[str, Any],
        extra_books: list[str] | None = None,
    ) -> list[NormalizedLoreEntry]:
        return _common.collect_merged_lore_entries(self, card_v3, extra_books)

    def _resolve_card_and_identity(
        self,
        char_query: str,
        session: dict[str, Any] | None = None,
        *,
        include_avatar_data_uri: bool = False,
    ) -> tuple[Path, dict[str, Any], str, str]:
        return _common.resolve_card_and_identity(
            self, char_query, session, include_avatar_data_uri=include_avatar_data_uri
        )

    def _validate_contained_path(
        self,
        raw_path: str | Path,
        *,
        default_subdir: str | None = None,
        allowed_suffixes: tuple[str, ...] = (),
        label: str = "path",
    ) -> Path:
        return _common.validate_contained_path(
            self, raw_path, default_subdir=default_subdir, allowed_suffixes=allowed_suffixes, label=label
        )

    # -- 14 MCP tools --------------------------------------------------
    def tool_st_session_bootstrap(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_session_bootstrap", args)

    def tool_st_dump_prompt_frame(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_dump_prompt_frame", args)

    def tool_st_commit_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_commit_turn", args)

    def tool_st_publish_stage_suggestions(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_publish_stage_suggestions", args)

    def tool_st_read_card(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_read_card", args)

    def tool_st_build_export_card(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_build_export_card", args)

    def tool_st_lint_card_and_lore(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_lint_card_and_lore", args)

    def tool_st_query_lorebook(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_query_lorebook", args)

    def tool_st_evolve_lore_entry(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_evolve_lore_entry", args)

    def tool_st_manage_chat_branch(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_manage_chat_branch", args)

    def tool_st_stage_floor_op(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_stage_floor_op", args)

    def tool_st_switch_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_switch_profile", args)

    def tool_st_manage_variables(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_manage_variables", args)

    def tool_st_compile_stage_widget(self, args: dict[str, Any]) -> dict[str, Any]:
        return call_tool(self, "st_compile_stage_widget", args)

    # -- Sidecar control plane (`--sidecar-op`) -------------------------
    def handle_sidecar_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        return stage.handle_sidecar_op(self, op, args)

    def _sidecar_snapshot(self) -> dict[str, Any]:
        return stage.sidecar_snapshot(self)

    def _sidecar_prepare_agent_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        return stage.sidecar_prepare_agent_turn(self, args)

    def _sidecar_byok_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        return stage.sidecar_byok_turn(self, args)

    def _sidecar_read_lorebook(self, args: dict[str, Any]) -> dict[str, Any]:
        return stage.sidecar_read_lorebook(self, args)

    def _sidecar_import_card(self, args: dict[str, Any]) -> dict[str, Any]:
        return stage.sidecar_import_card(self, args)


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Tavern Fusion 3.0 MCP Server & Sidecar CLI")
    parser.add_argument("--data-root", default=None, help="SillyTavern user data directory")
    parser.add_argument("--state-dir", default=None, help="Plugin persistent state directory")
    parser.add_argument("--cli", metavar="TOOL_NAME", help="Invoke a single MCP tool via CLI and exit")
    parser.add_argument("--sidecar-op", metavar="OP_NAME", help="Invoke a Sidecar batch operation and exit")
    parser.add_argument("--args", default="{}", help="JSON string of arguments (or '-' to read JSON from stdin)")
    parsed = parser.parse_args()

    raw_args_str = sys.stdin.read() if parsed.args == "-" else parsed.args
    call_args = json.loads(raw_args_str) if raw_args_str.strip() else {}

    runtime = TavernFusionRuntime(st_data_root=parsed.data_root, state_dir=parsed.state_dir)
    try:
        if parsed.sidecar_op:
            out = runtime.handle_sidecar_op(parsed.sidecar_op, call_args)
            sys.stdout.write(json.dumps({"ok": True, "data": out}, ensure_ascii=False) + "\n")
            return

        if parsed.cli:
            server = StdioMCPServer(runtime)
            resp = server.handle_rpc({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": parsed.cli, "arguments": call_args},
            })
            sys.stdout.write(json.dumps(resp, ensure_ascii=False, indent=2) + "\n")
            return

        StdioMCPServer(runtime).serve_forever()
    except Exception as exc:
        if parsed.sidecar_op:
            sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) + "\n")
            sys.exit(0)
        raise
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
