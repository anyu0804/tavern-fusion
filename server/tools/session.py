"""Session tools: `st_session_bootstrap`, `st_dump_prompt_frame`, `st_switch_profile`."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from ..card_codec import (
    list_character_expressions,
)
from ..chat_sync import (
    create_new_chat_session,
    exclusive_file_lock,
    list_chat_files,
    read_jsonl_chat,
    write_jsonl_chat_atomic,
)
from ..policy import evaluate_minor_risk_gate
from ..prompt import (
    compile_st_prompt_frame,
    get_active_persona,
    load_openai_preset,
    normalize_epistemic_level,
    resolve_st_macros,
)
from ..runtime.context import RuntimeContext
from ..runtime.notice import FIRST_RUN_NOTICE, FIRST_RUN_NOTICE_VERSION
from ._common import collect_merged_lore_entries, resolve_card_and_identity

# MCP input schemas (verbatim from the pre-refactor MCP_TOOL_SCHEMAS list)
ST_SESSION_BOOTSTRAP_SCHEMA: dict[str, Any] = {
    "name": "st_session_bootstrap",
    "description": "Bootstraps or resumes a SillyTavern character session (--play or --chat), loading CCv2/CCv3 card, Persona, Preset, and latest .jsonl chat log.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string", "description": "Character name or PNG filename (e.g. 'Seraphina')."},
            "mode": {"type": "string", "enum": ["play", "chat"], "default": "play"},
            "new_chat": {"type": "boolean", "default": False},
            "chat_file": {"type": "string", "description": "Optional specific .jsonl chat filename to activate."},
            "greeting_index": {"type": "integer", "default": 0},
            "persona_name": {"type": "string"},
            "preset_name": {"type": "string"},
            "lorebooks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["character_name"],
    },
}

ST_DUMP_PROMPT_FRAME_SCHEMA: dict[str, Any] = {
    "name": "st_dump_prompt_frame",
    "description": "Dry-runs the Hybrid Lorebook Engine and full OpenAI Settings prompt_order compiler (--dump-prompt), returning active @D lore, budget usage, evicted entries, and assembled prompt.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string"},
            "user_input": {"type": "string"},
            "token_budget": {"type": "integer", "default": 2048},
            "mode": {"type": "string", "enum": ["play", "chat"]},
        },
    },
}

ST_SWITCH_PROFILE_SCHEMA: dict[str, Any] = {
    "name": "st_switch_profile",
    "description": "Switches active OpenAI Preset, User Persona, --play/--chat mode, policy_mode, or per-chat Author's Note & @D depth.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "preset_name": {"type": "string"},
            "persona_name": {"type": "string"},
            "mode": {"type": "string", "enum": ["play", "chat"]},
            "epistemic_level": {"type": "integer", "enum": [0, 50, 100]},
            "agentic_hands": {"type": "boolean"},
            "director_mode": {"type": "boolean"},
            "policy_mode": {"type": "string", "enum": ["host_gentle", "strict_block"]},
            "authors_note": {"type": "string"},
            "an_depth": {"type": "integer"},
        },
    },
}


def st_session_bootstrap(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    char_query = str(args["character_name"]).strip()
    mode = str(args.get("mode", "play")).lstrip("-")
    force_new = bool(args.get("new_chat", False))
    greeting_idx = int(args.get("greeting_index", 0))

    prev = ctx.sessions.load()
    card_path, card_bundle, char_name, avatar_stem = resolve_card_and_identity(ctx, char_query, prev)
    minor_gate = evaluate_minor_risk_gate(card_bundle)
    if minor_gate["gate_action"] == "block_load":
        raise ValueError(
            f"[Card Doctor 未成年风险闸] 默认拒绝装载角色卡「{char_name}」 ({card_path.name})："
            f"{'；'.join(minor_gate['reasons'])}"
        )
    card_v3 = card_bundle["card"]

    persona = get_active_persona(ctx.st_data_root, args.get("persona_name"))
    preset = load_openai_preset(ctx.st_data_root, args.get("preset_name"))

    existing_chats = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=char_name)
    requested_chat = str(args.get("chat_file", "")).strip()
    chosen_existing = None
    if requested_chat and existing_chats:
        for p in existing_chats:
            if p.name == requested_chat or p.stem == requested_chat:
                chosen_existing = p
                break

    if chosen_existing is not None:
        active_chat_path = chosen_existing
        seeded_new = False
    elif existing_chats and not force_new:
        active_chat_path = existing_chats[0]
        seeded_new = False
    else:
        raw_first = resolve_st_macros(
            str(card_v3["data"].get("first_mes", "")),
            char_name=char_name,
            user_name=persona["name"],
        )
        alt_greets = [
            resolve_st_macros(str(g), char_name=char_name, user_name=persona["name"])
            for g in (card_v3["data"].get("alternate_greetings") or [])
        ]
        created = create_new_chat_session(
            ctx.st_data_root,
            character_name=char_name,
            user_name=persona["name"],
            greeting_text=raw_first,
            alternate_greetings=alt_greets,
            greeting_index=greeting_idx,
            avatar_stem=avatar_stem,
        )
        active_chat_path = Path(created["chat_path"])
        seeded_new = True

    header, messages = read_jsonl_chat(active_chat_path)
    rp_conv_id = str(args.get("rp_conv_id") or "").strip()
    activate_rp = bool(args.get("activate_rp", True))
    session_state = {
        "active": activate_rp,
        "rp_conv_id": rp_conv_id,
        "character_name": char_name,
        "card_file": card_path.name,
        "avatar_stem": avatar_stem,
        "card_path": str(card_path),
        "chat_path": str(active_chat_path),
        "persona_name": persona["name"],
        "preset_name": preset["preset_name"],
        "mode": mode,
        "extra_lorebooks": args.get("lorebooks", prev.get("extra_lorebooks", [])),
        "last_bootstrapped_at": time.time(),
        "pending_uncommitted_turn": False,
        "pending_turn_mode": "normal",
        "stage_busy": False,
        "stop_retry_count": 0,
        # Reset conversation prefix cache tracker on character/chat switch
        "bootstrapped_conv_id": "",
        "bootstrapped_char": "",
    }
    ctx.sessions.save(session_state)
    settings_updates: dict[str, Any] = {}
    if (ctx.st_data_root / "characters").exists() and "mock_st_data" not in str(ctx.st_data_root):
        settings_updates["last_st_data_root"] = str(ctx.st_data_root)

    cur_cfg = ctx.load_plugin_settings()
    emit_first_run = bool(
        activate_rp
        and not cur_cfg.get("first_run_notice_ack_v1")
        and not cur_cfg.get("first_run_cli_shown_v1")
    )
    if emit_first_run:
        settings_updates["first_run_cli_shown_v1"] = True
        settings_updates["first_run_cli_shown_version"] = FIRST_RUN_NOTICE_VERSION
    if settings_updates:
        ctx.save_plugin_settings(settings_updates)

    expressions = list_character_expressions(ctx.st_data_root, avatar_stem) or list_character_expressions(
        ctx.st_data_root, char_name
    )
    result: dict[str, Any] = {
        "status": "active",
        "character_name": char_name,
        "card_file": card_path.name,
        "avatar_stem": avatar_stem,
        "detected_card_spec": card_bundle["detected_chunk"],
        "mode": f"--{mode}",
        "resumed_existing_chat": not seeded_new,
        "chat_file": active_chat_path.name,
        "total_floors": len(messages),
        "recent_messages": messages[-6:],
        "variables": header.get("chat_metadata", {}).get("variables", {}),
        "scenario": card_v3["data"].get("scenario", ""),
        "persona": persona,
        "preset_name": preset["preset_name"],
        "available_expressions": expressions,
        "sidecar_pill": "[Tavern Stage](sidecar://tavern-fusion/tavern_stage/)",
    }
    if emit_first_run:
        result["first_run_notice"] = FIRST_RUN_NOTICE
    if minor_gate["gate_action"] == "red_screen_warn":
        result["minor_risk_gate"] = minor_gate
    return result


def st_dump_prompt_frame(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.sessions.load()
    char_query = str(args.get("character_name") or session.get("card_file") or session.get("character_name") or "").strip()
    if not char_query:
        raise ValueError("No character_name specified and no active session found.")
    user_input = str(args.get("user_input", ""))
    budget = int(args.get("token_budget", 2048))
    mode = str(args.get("mode") or session.get("mode") or "play").lstrip("-")
    compact_delta = bool(args.get("compact_agent_delta", False))
    boundary_notice = str(args.get("boundary_notice", ""))
    epistemic_level = normalize_epistemic_level(
        args.get("epistemic_level", session.get("epistemic_level", 0))
    )
    agentic_hands = bool(args.get("agentic_hands", session.get("agentic_hands", False)))
    director_mode = bool(args.get("director_mode", session.get("director_mode", False)))
    quarantine_backstage = (epistemic_level < 100) and (not director_mode)

    card_path, card_bundle, char_name, avatar_stem = resolve_card_and_identity(ctx, char_query, session)
    card_v3 = card_bundle["card"]

    chat_path_str = session.get("chat_path")
    if (
        chat_path_str
        and Path(chat_path_str).exists()
        and session.get("character_name") == char_name
        and session.get("avatar_stem", avatar_stem) == avatar_stem
    ):
        header, messages = read_jsonl_chat(chat_path_str)
    else:
        chats = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=char_name)
        if chats:
            header, messages = read_jsonl_chat(chats[0])
        else:
            header, messages = {"chat_metadata": {"variables": {}, "timed_effects": {}}}, []

    entries = collect_merged_lore_entries(ctx, card_v3, session.get("extra_lorebooks"))
    lore_res = ctx.lore_engine.evaluate_and_splice(
        entries=entries,
        chat_messages=messages,
        latest_user_input=user_input,
        timed_state=header.get("chat_metadata", {}).get("timed_effects"),
        token_budget=budget,
        quarantine_backstage=quarantine_backstage,
    )
    persona = get_active_persona(ctx.st_data_root, session.get("persona_name"))
    preset = load_openai_preset(ctx.st_data_root, session.get("preset_name"))
    frame = compile_st_prompt_frame(
        card_v3,
        persona,
        preset,
        lore_res,
        header,
        mode=mode,
        compact_agent_delta=compact_delta,
        boundary_notice=boundary_notice,
        epistemic_level=epistemic_level,
        agentic_hands=agentic_hands,
        director_mode=director_mode,
        cache_mode=str(args.get("cache_mode") or "off"),
    )
    result = {
        "character_name": char_name,
        "preset_name": frame["preset_name"],
        "epistemic_level": frame["epistemic_level"],
        "agentic_hands": frame["agentic_hands"],
        "director_mode": frame["director_mode"],
        "quarantined_backstage_count": frame["quarantined_backstage_count"],
        "lexical_sample_count": frame["lexical_sample_count"],
        "ordered_identifiers": frame["ordered_identifiers"],
        "prefix_cache_ratio_pct": frame["prefix_cache_ratio_pct"],
        "static_tokens": frame["static_tokens"],
        "dynamic_tokens": frame["dynamic_tokens"],
        "lore_used_tokens": lore_res["used_tokens"],
        "lore_budget": budget,
        "active_lore": lore_res["active_trace"],
        "evicted_lore": lore_res["evicted_trace"],
        "dump_report": frame["dump_report"],
        "ephemeral_frame": frame["ephemeral_frame"],
        "next_timed_state": lore_res["next_timed_state"],
    }
    if args.get("_include_parts"):
        # Internal (BYOK backend): structured frame parts for a multi-message request.
        result["frame_parts"] = {
            "cache_mode": frame["cache_mode"],
            "static_prefix_block": frame["static_prefix_block"],
            "history_messages": frame["history_messages"],
            "dynamic_tail_block": frame["dynamic_tail_block"],
        }
    return result


def st_switch_profile(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if args.get("preset_name"):
        preset = load_openai_preset(ctx.st_data_root, str(args["preset_name"]))
        patch["preset_name"] = preset["preset_name"]
        patch["bootstrapped_conv_id"] = ""
    if args.get("persona_name"):
        persona = get_active_persona(ctx.st_data_root, str(args["persona_name"]))
        patch["persona_name"] = persona["name"]
        patch["bootstrapped_conv_id"] = ""
    if args.get("mode"):
        patch["mode"] = str(args["mode"]).lstrip("-")
    if "epistemic_level" in args and args["epistemic_level"] is not None:
        patch["epistemic_level"] = normalize_epistemic_level(args["epistemic_level"])
        patch["bootstrapped_conv_id"] = ""
    if "agentic_hands" in args and args["agentic_hands"] is not None:
        patch["agentic_hands"] = bool(args["agentic_hands"])
    if "director_mode" in args and args["director_mode"] is not None:
        patch["director_mode"] = bool(args["director_mode"])
        patch["bootstrapped_conv_id"] = ""
    if args.get("policy_mode"):
        ctx.save_plugin_settings({"policy_mode": str(args["policy_mode"])})
    backend_patch: dict[str, Any] = {}
    if args.get("generation_backend") in ("host", "byok"):
        backend_patch["generation_backend"] = str(args["generation_backend"])
    if "byok_base_url" in args and args["byok_base_url"] is not None:
        backend_patch["byok_base_url"] = str(args["byok_base_url"]).strip()
    if "byok_model" in args and args["byok_model"] is not None:
        backend_patch["byok_model"] = str(args["byok_model"]).strip()
    if args.get("byok_cache_mode") in ("off", "prefix_pinned"):
        backend_patch["byok_cache_mode"] = str(args["byok_cache_mode"])
    if "byok_cache_control" in args and args["byok_cache_control"] is not None:
        backend_patch["byok_cache_control"] = bool(args["byok_cache_control"])
    if "byok_stream" in args and args["byok_stream"] is not None:
        backend_patch["byok_stream"] = bool(args["byok_stream"])
    if backend_patch:
        ctx.save_plugin_settings(backend_patch)

    session = ctx.sessions.update(patch) if patch else ctx.sessions.load()
    chat_path = session.get("chat_path")
    if chat_path and Path(chat_path).exists() and ("authors_note" in args or "an_depth" in args):
        c_path_obj = Path(chat_path)
        with exclusive_file_lock(c_path_obj):
            header, messages = read_jsonl_chat(c_path_obj)
            meta = header.setdefault("chat_metadata", {})
            if "authors_note" in args:
                meta["note_prompt"] = str(args["authors_note"])
            if "an_depth" in args:
                meta["note_depth"] = int(args["an_depth"])
            write_jsonl_chat_atomic(c_path_obj, header, messages)

    return {"updated_session": session, "plugin_settings": ctx.load_plugin_settings()}
