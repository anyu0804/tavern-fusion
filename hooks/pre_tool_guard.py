#!/usr/bin/env python3
"""Antigravity `PreToolUse` Chokepoint Guard (`hooks/pre_tool_guard.py`).

Fixes all Windows & Antigravity 2.0 compatibility contracts:
1. Covers both current Antigravity 2.0 file tools (`write_file`, `edit_file` with `Path`)
   and legacy tool names (`write_to_file`, `replace_file_content`, `multi_replace_file_content`
   with `TargetFile`), normalizing Windows backslashes (`\\` -> `/`).
2. Blocks raw writes to SillyTavern `/chats/*.jsonl` or `/characters/*.png` so all mutations
   go through atomic `st_*` tools.
3. Emits standard `"decision": "allow"` or `"deny"` per Antigravity Hooks spec.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hooks.session_init import discover_st_data_from_workspaces
from server.policy import evaluate_policy_compliance
from server.runtime.context import RuntimeContext
from server.tools import TOOL_NAMES

FILE_MUTATION_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
})

# Exact Tavern Fusion MCP tool names derived from the canonical `server.tools` registry.
# The host may prefix MCP tools with a server namespace (e.g. `mcp_st-mcp_st_commit_turn`),
# so names are matched as a suffix after a separator instead of a bare `"st_" in name`
# substring check, which also matched unrelated tools such as `list_dir`.
ST_TOOL_NAMES = frozenset(TOOL_NAMES)
_ST_TOOL_RE = re.compile(r"(?:^|[_\-.:/])(" + "|".join(sorted(ST_TOOL_NAMES)) + r")$")


def is_tavern_tool(tool_name: str) -> bool:
    return bool(_ST_TOOL_RE.search(tool_name))


def main() -> None:
    raw_in = sys.stdin.read().strip()
    payload = json.loads(raw_in) if raw_in else {}
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or payload.get("toolName") or "")
    tool_args = tool_call.get("args") or payload.get("toolInput") or {}
    ws_paths = payload.get("workspacePaths") or []
    conv_id = str(payload.get("conversationId") or "").strip()

    # 0. Switch B Physical Tool Unplugging: When an RP session is in a pure theater turn
    # (`agentic_hands=False` and `director_mode=False`), physically deny any tool invocation.
    if conv_id:
        discovered = discover_st_data_from_workspaces(ws_paths)
        runtime = RuntimeContext(st_data_root=discovered)
        try:
            sess = runtime.sessions.load()
            if (
                bool(sess.get("active"))
                and str(sess.get("rp_conv_id") or "") == conv_id
                and bool(sess.get("pending_uncommitted_turn"))
                and sess.get("pending_turn_mode", "normal") in ("normal", "swipe")
                and not bool(sess.get("agentic_hands", False))
                and not bool(sess.get("director_mode", False))
            ):
                sys.stdout.write(
                    json.dumps({
                        "decision": "deny",
                        "reason": (
                            "Pure Theater Mode (Dimension Mixer Switch B OFF): Tools are physically unplugged "
                            "during pure roleplay turns to protect narrative immersion. Output in-character prose only."
                        ),
                    })
                    + "\n"
                )
                return
        finally:
            runtime.close()

    # 1. Block generic file tools from corrupting `.jsonl` chat logs or `.png` character cards
    if tool_name in FILE_MUTATION_TOOLS:
        raw_target = str(tool_args.get("Path") or tool_args.get("TargetFile") or "")
        norm_target = raw_target.replace("\\", "/").lower()
        if ("/chats/" in norm_target or "/group chats/" in norm_target) and norm_target.endswith(".jsonl"):
            sys.stdout.write(
                json.dumps({
                    "decision": "deny",
                    "reason": (
                        "Direct raw writes to SillyTavern `.jsonl` chat logs are blocked by Tavern Fusion "
                        "to prevent race conditions and broken swipe headers. Call `st_commit_turn`, "
                        "`st_stage_floor_op`, or `st_manage_chat_branch` instead."
                    ),
                })
                + "\n"
            )
            return
        if "/characters/" in norm_target and norm_target.endswith(".png"):
            sys.stdout.write(
                json.dumps({
                    "decision": "deny",
                    "reason": (
                        "Direct raw writes to Character Card `.png` files are blocked. Call "
                        "`st_build_export_card` to guarantee synchronized `ccv3` + `chara` chunks."
                    ),
                })
                + "\n"
            )
            return

    # 2. Validate & allow Tavern Fusion MCP tool calls (`*st_*`)
    if is_tavern_tool(tool_name):
        for path_key in (
            "output_filename",
            "lorebook_name",
            "chat_file",
            "card_filename",
            "output_html_path",
            "portrait_image_path",
        ):
            val = str(tool_args.get(path_key, ""))
            if ".." in Path(val.replace("\\", "/")).parts or ".." in val:
                sys.stdout.write(
                    json.dumps({
                        "decision": "deny",
                        "reason": f"Path traversal ('..') is forbidden in `{path_key}`.",
                    })
                    + "\n"
                )
                return

        discovered = discover_st_data_from_workspaces(ws_paths)
        runtime = RuntimeContext(st_data_root=discovered)
        try:
            serialized_args = json.dumps(tool_args, ensure_ascii=False)
            compliant, violation = evaluate_policy_compliance(serialized_args, state_dir=runtime.state_dir)
        finally:
            runtime.close()

        if not compliant:
            sys.stdout.write(
                json.dumps({
                    "decision": "deny",
                    "reason": f"Blocked by Policy Chokepoint: {violation}",
                })
                + "\n"
            )
            return

        sys.stdout.write(
            json.dumps({
                "decision": "allow",
                "reason": "Verified Tavern Fusion atomic MCP operation.",
            })
            + "\n"
        )
        return

    sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")


if __name__ == "__main__":
    main()
