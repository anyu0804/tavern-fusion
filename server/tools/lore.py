"""Lorebook tools: `st_query_lorebook`, `st_evolve_lore_entry`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..chat_sync import (
    exclusive_file_lock,
)
from ..fs_safety import ensure_under_dir, sanitize_filename_stem
from ..lore_engine import (
    NormalizedLoreEntry,
    evolve_lorebook_file,
    load_lorebook_file,
)
from ..runtime.context import RuntimeContext

# MCP input schemas (verbatim from the pre-refactor MCP_TOOL_SCHEMAS list)
ST_QUERY_LOREBOOK_SCHEMA: dict[str, Any] = {
    "name": "st_query_lorebook",
    "description": "Queries SillyTavern World Info JSON files using hybrid keyword/regex + selective secondary gates + local/dense semantic similarity.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "lorebook_names": {"type": "array", "items": {"type": "string"}},
            "query_text": {"type": "string"},
            "token_budget": {"type": "integer", "default": 2048},
        },
        "required": ["query_text"],
    },
}

ST_EVOLVE_LORE_ENTRY_SCHEMA: dict[str, Any] = {
    "name": "st_evolve_lore_entry",
    "description": "Dramaturgi Lore Evolution tool: inserts, updates, toggles, archives, or deletes entries in SillyTavern worlds/*.json.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "lorebook_name": {"type": "string"},
            "operation": {"type": "string", "enum": ["upsert", "toggle", "archive", "delete"], "default": "upsert"},
            "entry_payload": {"type": "object"},
        },
        "required": ["lorebook_name", "entry_payload"],
    },
}


def st_query_lorebook(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    book_names = args.get("lorebook_names") or []
    if isinstance(book_names, str):
        book_names = [book_names]
    query_text = str(args.get("query_text", ""))
    budget = int(args.get("token_budget", 2048))

    entries: list[NormalizedLoreEntry] = []
    worlds_dir = (ctx.st_data_root / "worlds").resolve()
    worlds_dir.mkdir(parents=True, exist_ok=True)
    target_files = (
        [ensure_under_dir(worlds_dir / (b if b.endswith(".json") else f"{b}.json"), worlds_dir) for b in book_names]
        if book_names
        else sorted(worlds_dir.glob("*.json"))
    )
    for p in target_files:
        if p.exists():
            try:
                entries.extend(load_lorebook_file(p))
            except Exception:
                continue

    res = ctx.lore_engine.evaluate_and_splice(
        entries=entries,
        chat_messages=[],
        latest_user_input=query_text,
        token_budget=budget,
    )
    return {
        "queried_books": [p.stem for p in target_files if p.exists()],
        "total_entries_scanned": len(entries),
        "used_tokens": res["used_tokens"],
        "active_entries": res["active_trace"],
        "evicted_entries": res["evicted_trace"],
    }


def st_evolve_lore_entry(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    raw_name = sanitize_filename_stem(Path(str(args["lorebook_name"]).strip()).stem)
    worlds_dir = (ctx.st_data_root / "worlds").resolve()
    worlds_dir.mkdir(parents=True, exist_ok=True)
    lore_path = ensure_under_dir(worlds_dir / f"{raw_name}.json", worlds_dir)
    op = str(args.get("operation", "upsert"))
    payload = args["entry_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    # Serialize read-modify-write on the lorebook file against concurrent evolutions
    with exclusive_file_lock(lore_path):
        return evolve_lorebook_file(lore_path, op, payload)  # type: ignore[arg-type]
