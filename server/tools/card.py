"""Card tools: `st_read_card`, `st_build_export_card`, `st_lint_card_and_lore`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..card_codec import (
    lint_character_card,
    read_character_card,
    write_character_card_png,
)
from ..fs_safety import ensure_under_dir, sanitize_filename_stem
from ..lore_engine import (
    lint_lorebook_file,
)
from ..policy import evaluate_minor_risk_gate
from ..runtime.context import RuntimeContext
from ._common import find_character_card_path, validate_contained_path

# MCP input schemas (verbatim from the pre-refactor MCP_TOOL_SCHEMAS list)
ST_READ_CARD_SCHEMA: dict[str, Any] = {
    "name": "st_read_card",
    "description": "Losslessly parses a SillyTavern Character Card PNG (tEXt/zTXt/iTXt, preferring ccv3 over chara) or JSON file.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "card_filename": {"type": "string"},
            "include_avatar_base64": {"type": "boolean", "default": False},
        },
        "required": ["card_filename"],
    },
}

ST_BUILD_EXPORT_CARD_SCHEMA: dict[str, Any] = {
    "name": "st_build_export_card",
    "description": "Seppä card forge: validates Character Card V3 schema, enforces Card-is-Data security sanitization, and atomically exports a PNG with synchronized chara (V2) + ccv3 (V3) chunks.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "card_v3_json": {"type": "object"},
            "output_filename": {"type": "string"},
            "portrait_image_path": {"type": "string"},
        },
        "required": ["card_v3_json", "output_filename"],
    },
}

ST_LINT_CARD_AND_LORE_SCHEMA: dict[str, Any] = {
    "name": "st_lint_card_and_lore",
    "description": "Card Doctor & World Info Doctor static linter: detects missing ccv3 chunks, system_prompt wiping presets without {{original}}, selective:false secondary-key traps, leaked creator_notes, and broken macros.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string"},
            "lorebook_name": {"type": "string"},
        },
    },
}


def st_read_card(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    card_path = find_character_card_path(ctx, str(args["card_filename"]))
    bundle = read_character_card(
        card_path,
        include_avatar_data_uri=bool(args.get("include_avatar_base64", False)),
    )
    bundle.pop("raw_card", None)
    return bundle


def st_build_export_card(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    card_json = args["card_v3_json"]
    if isinstance(card_json, str):
        card_json = json.loads(card_json)
    minor_gate = evaluate_minor_risk_gate(card_json)
    if minor_gate["gate_action"] == "block_load":
        raise ValueError(
            f"[Card Doctor 未成年风险闸] 默认拒绝铸造/导出未成年风险角色卡：{'；'.join(minor_gate['reasons'])}"
        )
    out_name = sanitize_filename_stem(Path(str(args["output_filename"]).strip()).stem) + ".png"
    char_dir = (ctx.st_data_root / "characters").resolve()
    char_dir.mkdir(parents=True, exist_ok=True)
    out_path = ensure_under_dir(char_dir / out_name, char_dir)
    raw_portrait = args.get("portrait_image_path")
    portrait_path = (
        validate_contained_path(ctx, raw_portrait, label="portrait_image_path")
        if raw_portrait
        else None
    )
    return write_character_card_png(
        card_payload=card_json,
        output_png_path=out_path,
        base_png_path=portrait_path,
    )


def st_lint_card_and_lore(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Card Doctor & World Info Doctor static audit tool."""
    card_issues: list[dict[str, Any]] = []
    lore_issues: list[dict[str, Any]] = []
    inspected_card = None
    inspected_lore = None

    char_q = str(args.get("character_name") or "").strip()
    lore_q = str(args.get("lorebook_name") or "").strip()
    if not char_q and not lore_q:
        sess = ctx.sessions.load()
        char_q = str(sess.get("character_name") or "")

    minor_gate: dict[str, Any] = {
        "gate_action": "pass",
        "blocked": False,
        "red_screen_warn": False,
        "signals": [],
        "reasons": [],
    }
    if char_q:
        c_path = find_character_card_path(ctx, char_q)
        bundle = read_character_card(c_path, include_avatar_data_uri=False)
        inspected_card = c_path.name
        minor_gate = evaluate_minor_risk_gate(bundle)
        card_issues = lint_character_card(bundle)
        linked = str(bundle["card"]["data"].get("extensions", {}).get("world", "")).strip()
        if linked and not lore_q:
            lore_q = linked

    if lore_q:
        fname = lore_q if lore_q.endswith(".json") else f"{lore_q}.json"
        l_path = ensure_under_dir(ctx.st_data_root / "worlds" / fname, ctx.st_data_root / "worlds")
        inspected_lore = l_path.name
        lore_issues = lint_lorebook_file(l_path)

    total = len(card_issues) + len(lore_issues)
    return {
        "healthy": total == 0,
        "total_issues": total,
        "inspected_card": inspected_card,
        "inspected_lorebook": inspected_lore,
        "minor_risk_gate": minor_gate,
        "card_issues": card_issues,
        "lorebook_issues": lore_issues,
    }
