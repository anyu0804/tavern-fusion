"""MCP tool registry: tool name -> `ToolSpec(schema, handler)`.

Every handler has the signature `handler(ctx: RuntimeContext, args: dict) -> dict`, where `ctx`
is a `server.runtime.context.RuntimeContext` (the MCP server passes its `TavernFusionRuntime`,
hooks pass a bare `RuntimeContext`). The registry order is the `tools/list` order.
"""

from __future__ import annotations

from typing import Any, Callable, NamedTuple

from ..runtime.context import RuntimeContext
from . import card, chat, lore, session, stage

ToolHandler = Callable[[RuntimeContext, dict[str, Any]], dict[str, Any]]


class ToolSpec(NamedTuple):
    schema: dict[str, Any]
    handler: ToolHandler


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "st_session_bootstrap": ToolSpec(session.ST_SESSION_BOOTSTRAP_SCHEMA, session.st_session_bootstrap),
    "st_commit_turn": ToolSpec(chat.ST_COMMIT_TURN_SCHEMA, chat.st_commit_turn),
    "st_publish_stage_suggestions": ToolSpec(stage.ST_PUBLISH_STAGE_SUGGESTIONS_SCHEMA, stage.st_publish_stage_suggestions),
    "st_compile_stage_widget": ToolSpec(stage.ST_COMPILE_STAGE_WIDGET_SCHEMA, stage.st_compile_stage_widget),
    "st_dump_prompt_frame": ToolSpec(session.ST_DUMP_PROMPT_FRAME_SCHEMA, session.st_dump_prompt_frame),
    "st_read_card": ToolSpec(card.ST_READ_CARD_SCHEMA, card.st_read_card),
    "st_build_export_card": ToolSpec(card.ST_BUILD_EXPORT_CARD_SCHEMA, card.st_build_export_card),
    "st_lint_card_and_lore": ToolSpec(card.ST_LINT_CARD_AND_LORE_SCHEMA, card.st_lint_card_and_lore),
    "st_query_lorebook": ToolSpec(lore.ST_QUERY_LOREBOOK_SCHEMA, lore.st_query_lorebook),
    "st_evolve_lore_entry": ToolSpec(lore.ST_EVOLVE_LORE_ENTRY_SCHEMA, lore.st_evolve_lore_entry),
    "st_manage_chat_branch": ToolSpec(chat.ST_MANAGE_CHAT_BRANCH_SCHEMA, chat.st_manage_chat_branch),
    "st_stage_floor_op": ToolSpec(chat.ST_STAGE_FLOOR_OP_SCHEMA, chat.st_stage_floor_op),
    "st_switch_profile": ToolSpec(session.ST_SWITCH_PROFILE_SCHEMA, session.st_switch_profile),
    "st_manage_variables": ToolSpec(chat.ST_MANAGE_VARIABLES_SCHEMA, chat.st_manage_variables),
}

# `tools/list` payload (same list object shape as the pre-refactor module constant).
MCP_TOOL_SCHEMAS: list[dict[str, Any]] = [spec.schema for spec in TOOL_REGISTRY.values()]

TOOL_NAMES: tuple[str, ...] = tuple(TOOL_REGISTRY)


def call_tool(ctx: RuntimeContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Runs tool `name` against `ctx`. Raises `KeyError` for an unknown tool."""
    return TOOL_REGISTRY[name].handler(ctx, args)


def handle_sidecar_op(ctx: RuntimeContext, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Tavern Stage control-plane operation (`--sidecar-op`)."""
    return stage.handle_sidecar_op(ctx, op, args)


__all__ = [
    "MCP_TOOL_SCHEMAS",
    "TOOL_NAMES",
    "TOOL_REGISTRY",
    "ToolHandler",
    "ToolSpec",
    "call_tool",
    "handle_sidecar_op",
]
