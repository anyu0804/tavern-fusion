"""MCP tool registry: tool name -> `ToolSpec(schema, handler)`.

Every handler has the signature `handler(ctx: RuntimeContext, args: dict) -> dict`, where `ctx`
is a `server.runtime.context.RuntimeContext` (the MCP server passes its `TavernFusionRuntime`,
hooks pass a bare `RuntimeContext`). The registry order is the `tools/list` order.

`TOOL_NAMES` is exposed at module import time without importing any heavy tool/codec submodules so
lightweight lifecycle hooks (such as `hooks/pre_tool_guard.py`) can share the single source of truth
at near-zero import cost; `TOOL_REGISTRY` and `MCP_TOOL_SCHEMAS` load on first access.
"""

from __future__ import annotations

from functools import wraps
import importlib
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

if TYPE_CHECKING:
    from ..runtime.context import RuntimeContext

ToolHandler = Callable[["RuntimeContext", dict[str, Any]], dict[str, Any]]


class ToolSpec(NamedTuple):
    schema: dict[str, Any]
    handler: ToolHandler


# Single source of truth: name -> (submodule, schema_attr, handler_attr) in `tools/list` order.
_REGISTRY_SPECS: dict[str, tuple[str, str, str]] = {
    "st_session_bootstrap": ("session", "ST_SESSION_BOOTSTRAP_SCHEMA", "st_session_bootstrap"),
    "st_commit_turn": ("chat", "ST_COMMIT_TURN_SCHEMA", "st_commit_turn"),
    "st_publish_stage_suggestions": ("stage", "ST_PUBLISH_STAGE_SUGGESTIONS_SCHEMA", "st_publish_stage_suggestions"),
    "st_compile_stage_widget": ("stage", "ST_COMPILE_STAGE_WIDGET_SCHEMA", "st_compile_stage_widget"),
    "st_dump_prompt_frame": ("session", "ST_DUMP_PROMPT_FRAME_SCHEMA", "st_dump_prompt_frame"),
    "st_read_card": ("card", "ST_READ_CARD_SCHEMA", "st_read_card"),
    "st_build_export_card": ("card", "ST_BUILD_EXPORT_CARD_SCHEMA", "st_build_export_card"),
    "st_lint_card_and_lore": ("card", "ST_LINT_CARD_AND_LORE_SCHEMA", "st_lint_card_and_lore"),
    "st_query_lorebook": ("lore", "ST_QUERY_LOREBOOK_SCHEMA", "st_query_lorebook"),
    "st_evolve_lore_entry": ("lore", "ST_EVOLVE_LORE_ENTRY_SCHEMA", "st_evolve_lore_entry"),
    "st_manage_chat_branch": ("chat", "ST_MANAGE_CHAT_BRANCH_SCHEMA", "st_manage_chat_branch"),
    "st_stage_floor_op": ("chat", "ST_STAGE_FLOOR_OP_SCHEMA", "st_stage_floor_op"),
    "st_switch_profile": ("session", "ST_SWITCH_PROFILE_SCHEMA", "st_switch_profile"),
    "st_manage_variables": ("chat", "ST_MANAGE_VARIABLES_SCHEMA", "st_manage_variables"),
}

TOOL_NAMES: tuple[str, ...] = tuple(_REGISTRY_SPECS)

_cached_registry: dict[str, ToolSpec] | None = None
_cached_schemas: list[dict[str, Any]] | None = None


def validate_required_args(tool_name: str, schema: dict[str, Any], args: Any) -> dict[str, Any]:
    """Validates `args` against `schema['inputSchema']['required']` with actionable error messages."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(
            f"Invalid arguments for `{tool_name}`: expected a JSON object, got {type(args).__name__}."
        )
    input_schema = schema.get("inputSchema") or {}
    required = input_schema.get("required") or ()
    props = input_schema.get("properties") or {}
    missing: list[str] = []
    for req_key in required:
        if req_key not in args or args[req_key] is None:
            prop_type = (props.get(req_key) or {}).get("type")
            missing.append(f"'{req_key}' ({prop_type})" if prop_type else f"'{req_key}'")
    if missing:
        joined = ", ".join(missing)
        plural = "s" if len(missing) > 1 else ""
        accepted = ", ".join(props.keys())
        raise ValueError(
            f"Missing required argument{plural} for `{tool_name}`: {joined}. "
            f"Accepted parameters: [{accepted}]."
        )
    return args


def _wrap_handler(tool_name: str, schema: dict[str, Any], raw_fn: ToolHandler) -> ToolHandler:
    @wraps(raw_fn)
    def _checked(ctx: "RuntimeContext", args: dict[str, Any]) -> dict[str, Any]:
        checked = validate_required_args(tool_name, schema, args)
        return raw_fn(ctx, checked)

    return _checked


def _get_registry() -> dict[str, ToolSpec]:
    global _cached_registry, _cached_schemas
    if _cached_registry is None:
        modules: dict[str, Any] = {}
        reg: dict[str, ToolSpec] = {}
        for name, (mod_name, schema_attr, fn_attr) in _REGISTRY_SPECS.items():
            mod = modules.get(mod_name)
            if mod is None:
                mod = importlib.import_module(f"{__name__}.{mod_name}")
                modules[mod_name] = mod
            schema = getattr(mod, schema_attr)
            raw_fn = getattr(mod, fn_attr)
            reg[name] = ToolSpec(schema, _wrap_handler(name, schema, raw_fn))
        _cached_registry = reg
        _cached_schemas = [spec.schema for spec in reg.values()]
    return _cached_registry


def __getattr__(attr: str) -> Any:
    if attr == "TOOL_REGISTRY":
        return _get_registry()
    if attr == "MCP_TOOL_SCHEMAS":
        _get_registry()
        return _cached_schemas
    raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")


def call_tool(ctx: "RuntimeContext", name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Runs tool `name` against `ctx` (with required-argument validation). Raises `KeyError` for an unknown tool."""
    return _get_registry()[name].handler(ctx, args)


def handle_sidecar_op(ctx: "RuntimeContext", op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Tavern Stage control-plane operation (`--sidecar-op`)."""
    from . import stage

    return stage.handle_sidecar_op(ctx, op, args)


__all__ = [
    "MCP_TOOL_SCHEMAS",
    "TOOL_NAMES",
    "TOOL_REGISTRY",
    "ToolHandler",
    "ToolSpec",
    "call_tool",
    "handle_sidecar_op",
    "validate_required_args",
]
