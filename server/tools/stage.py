"""Tavern Stage tools (`st_publish_stage_suggestions`, `st_compile_stage_widget`) and the sidecar
control-plane operations behind `--sidecar-op` (`handle_sidecar_op`).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import time
from typing import Any

from ..byok_backend import (
    STREAM_PREVIEW_FILE,
    build_byok_cached_messages,
    build_byok_messages,
    byok_chat_completion_stream,
    byok_chat_completion_with_usage,
    read_byok_api_key,
    resolve_byok_config,
)
from ..card_codec import (
    detect_message_emotion,
    list_character_expressions,
    read_character_card,
    write_character_card_png,
)
from ..chat_sync import (
    exclusive_file_lock,
    list_chat_files,
    list_groups,
    read_jsonl_chat,
    write_jsonl_chat_atomic,
)
from ..fs_safety import atomic_write_json, atomic_write_text, ensure_under_dir, sanitize_filename_stem
from ..lore_engine import (
    lint_lorebook_file,
    load_lorebook_file,
)
from ..policy import evaluate_minor_risk_gate, evaluate_policy_compliance
from ..prompt import (
    get_active_persona,
    list_openai_presets,
    load_openai_preset,
    normalize_epistemic_level,
)
from ..runtime.context import RuntimeContext
from ..runtime.notice import FIRST_RUN_NOTICE, FIRST_RUN_NOTICE_VERSION
from ..widget_compiler import compile_stage_widget_html
from ._common import (
    find_character_card_path,
    resolve_card_and_identity,
    validate_contained_path,
)
from .card import st_lint_card_and_lore
from .chat import st_commit_turn, st_manage_variables, st_stage_floor_op
from .lore import st_evolve_lore_entry, st_query_lorebook
from .session import st_dump_prompt_frame, st_session_bootstrap, st_switch_profile

BYOK_SUPPORTED_ACTIONS = ("send_turn", "swipe_reroll")
BYOK_CANCEL_FILE = "byok_cancel.flag"
_CARD_SUMMARY_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_CARD_SUMMARY_CACHE_MAX = 512


# MCP input schemas (verbatim from the pre-refactor MCP_TOOL_SCHEMAS list)
ST_PUBLISH_STAGE_SUGGESTIONS_SCHEMA: dict[str, Any] = {
    "name": "st_publish_stage_suggestions",
    "description": "Publishes 3 in-character player reply options or expanded player intent directly to the Tavern Stage Sidecar composer without committing a character floor.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["reply_suggestions", "expanded_intent"], "default": "reply_suggestions"},
            "suggestions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["suggestions"],
    },
}

ST_COMPILE_STAGE_WIDGET_SCHEMA: dict[str, Any] = {
    "name": "st_compile_stage_widget",
    "description": "Compiles a CSP-compliant Antigravity /generative_ui HTML Stage snapshot widget (inline <=500px or standalone) with theme variables, Swipes, and Lorebook drawer.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string"},
            "placement": {"type": "string", "enum": ["inline", "standalone"], "default": "standalone"},
            "output_html_path": {"type": "string"},
        },
    },
}


def st_publish_stage_suggestions(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Publishes player reply suggestions or expanded intent directly to Tavern Stage Sidecar."""
    raw_list = args.get("suggestions") or []
    if isinstance(raw_list, str):
        raw_list = [raw_list]
    suggestions = [str(x).strip() for x in raw_list if str(x).strip()][:5]
    kind = str(args.get("kind", "reply_suggestions"))
    payload = {
        "kind": kind,
        "suggestions": suggestions,
        "updated_at": time.time(),
    }
    atomic_write_json(ctx.stage_suggestions_file, payload, indent=2)

    def _clear_busy_if_exists(cur: dict[str, Any]) -> dict[str, Any]:
        if cur:
            cur["pending_uncommitted_turn"] = False
            cur["stage_busy"] = False
        return cur

    if ctx.sessions.exists():
        ctx.sessions.update(_clear_busy_if_exists)
    return {"published": len(suggestions), "kind": kind, "suggestions": suggestions}


def st_compile_stage_widget(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.sessions.load()
    char_query = str(args.get("character_name") or session.get("card_file") or session.get("character_name") or "").strip()
    if not char_query:
        raise ValueError("character_name is required to compile the Stage widget.")
    placement = str(args.get("placement", "standalone"))
    raw_out_html = args.get("output_html_path")
    safe_out_html = (
        validate_contained_path(ctx, 
            raw_out_html,
            default_subdir="widgets",
            allowed_suffixes=(".html", ".htm"),
            label="output_html_path",
        )
        if raw_out_html is not None and str(raw_out_html).strip() != ""
        else None
    )

    card_path, bundle, char_name, avatar_stem = resolve_card_and_identity(ctx, 
        char_query,
        session,
        include_avatar_data_uri=True,
    )
    card_v3 = bundle["card"]

    chat_path_str = session.get("chat_path")
    if (
        chat_path_str
        and Path(chat_path_str).exists()
        and session.get("character_name") == char_name
        and session.get("avatar_stem", avatar_stem) == avatar_stem
    ):
        chat_p = Path(chat_path_str)
        chat_file_name = chat_p.name
        header, messages = read_jsonl_chat(chat_p)
    else:
        chats = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=char_name)
        if chats:
            chat_file_name = chats[0].name
            header, messages = read_jsonl_chat(chats[0])
        else:
            chat_file_name = "unsaved_preview.jsonl"
            header, messages = {"chat_metadata": {"variables": {}}}, []

    dump_info = st_dump_prompt_frame(ctx, {"character_name": card_path.name})
    return compile_stage_widget_html(
        character_card=card_v3,
        avatar_data_uri=bundle["avatar_data_uri"],
        messages=messages,
        variables=header.get("chat_metadata", {}).get("variables", {}),
        active_lore_trace=dump_info["active_lore"],
        dump_report=dump_info["dump_report"],
        chat_file_name=chat_file_name,
        placement=placement,
        mode=str(session.get("mode", "play")),
        output_html_path=safe_out_html,
    )


def handle_sidecar_op(ctx: RuntimeContext, op: str, args: dict[str, Any]) -> dict[str, Any]:
    if op == "snapshot":
        return sidecar_snapshot(ctx)
    if op == "bootstrap":
        return st_session_bootstrap(ctx, args)
    if op == "bind_rp_conv":
        rp_cid = str(args.get("rp_conv_id") or "").strip()
        sess = ctx.sessions.bind(rp_cid)
        return {"rp_conv_id": sess["rp_conv_id"]}
    if op == "floor_op":
        return st_stage_floor_op(ctx, args)
    if op == "prepare_agent_turn":
        return sidecar_prepare_agent_turn(ctx, args)
    if op == "byok_turn":
        return sidecar_byok_turn(ctx, args)
    if op == "cancel_turn":
        return sidecar_cancel_turn(ctx, args)
    if op == "retry_uncommitted_turn":
        return sidecar_retry_uncommitted_turn(ctx)
    if op == "byok_status":
        status = resolve_byok_config(ctx.load_plugin_settings(), ctx.state_dir)
        status["last_usage"] = ctx.sessions.load().get("last_byok_usage")
        return status
    if op == "read_lorebook":
        return sidecar_read_lorebook(ctx, args)
    if op == "evolve_lore":
        return st_evolve_lore_entry(ctx, args)
    if op == "query_lore":
        return st_query_lorebook(ctx, args)
    if op == "lint":
        return st_lint_card_and_lore(ctx, args)
    if op == "switch_profile":
        return st_switch_profile(ctx, args)
    if op == "manage_variables":
        return st_manage_variables(ctx, args)
    if op == "import_card":
        return sidecar_import_card(ctx, args)
    if op == "clear_suggestions":
        ctx.stage_suggestions_file.unlink(missing_ok=True)
        return {"cleared": True}
    if op == "ack_first_run_notice":
        updated = ctx.save_plugin_settings({
            "first_run_notice_ack_v1": True,
            "first_run_cli_shown_v1": True,
            "first_run_notice_ack_version": FIRST_RUN_NOTICE_VERSION,
            "first_run_cli_shown_version": FIRST_RUN_NOTICE_VERSION,
            "first_run_ack_at": int(time.time()),
        })
        return {
            "acknowledged": True,
            "first_run_notice_ack_v1": bool(updated.get("first_run_notice_ack_v1")),
        }
    if op == "ack_minor_risk_warn":
        card_file = str(args.get("card_file") or "").strip()
        if not card_file:
            raise ValueError("Missing card_file for ack_minor_risk_warn.")
        c_path = find_character_card_path(ctx, card_file)
        gate = evaluate_minor_risk_gate(read_character_card(c_path, include_avatar_data_uri=False))
        if gate["gate_action"] == "block_load":
            raise ValueError("硬拦截角色卡不允许解除未成年风险闸。")
        cur_cfg = ctx.load_plugin_settings()
        acked = list(cur_cfg.get("minor_warn_acked_cards") or [])
        if c_path.name not in acked:
            acked.append(c_path.name)
        ctx.save_plugin_settings({"minor_warn_acked_cards": acked})
        return {"acknowledged": True, "card_file": c_path.name}
    raise ValueError(f"Unknown sidecar operation: {op}")


def sidecar_snapshot(ctx: RuntimeContext) -> dict[str, Any]:
    char_dir = ctx.st_data_root / "characters"
    worlds_dir = ctx.st_data_root / "worlds"
    cards_summary: list[dict[str, Any]] = []
    if char_dir.exists():
        for p in sorted(char_dir.iterdir(), key=lambda item: (item.name.lower(), item.name)):
            if p.is_file() and p.suffix.lower() in (".png", ".json"):
                try:
                    st_info = p.stat()
                    cache_key = str(p.resolve())
                    cached_entry = _CARD_SUMMARY_CACHE.get(cache_key)
                    if cached_entry and cached_entry[0] == st_info.st_size and cached_entry[1] == st_info.st_mtime_ns:
                        base_meta = cached_entry[2]
                    else:
                        b = read_character_card(p, include_avatar_data_uri=False, fast_metadata_only=True)
                        d = b["card"]["data"]
                        c_gate = evaluate_minor_risk_gate(b)
                        base_meta = {
                            "file": p.name,
                            "name": d["name"],
                            "scenario": str(d.get("scenario", ""))[:120],
                            "tags": list(d.get("tags") or [])[:6],
                            "world": str(d.get("extensions", {}).get("world", "")),
                            "spec": b["detected_chunk"],
                            "has_dual_chunks": b["has_dual_chunks"],
                            "minor_gate_action": c_gate["gate_action"],
                            "minor_gate_reasons": c_gate["reasons"],
                        }
                        if len(_CARD_SUMMARY_CACHE) >= _CARD_SUMMARY_CACHE_MAX:
                            _CARD_SUMMARY_CACHE.clear()
                        _CARD_SUMMARY_CACHE[cache_key] = (st_info.st_size, st_info.st_mtime_ns, base_meta)
                    c_name = base_meta["name"]
                    exprs_for_card = list_character_expressions(ctx.st_data_root, p.stem) or list_character_expressions(
                        ctx.st_data_root, c_name
                    )
                    cards_summary.append({
                        **base_meta,
                        "expressions_count": len(exprs_for_card),
                    })
                except Exception:
                    continue

    session = ctx.sessions.load()
    loadable_cards = [c for c in cards_summary if c.get("minor_gate_action") != "block_load"]
    if not session.get("character_name") and loadable_cards:
        # Auto-select first safe card for Sidecar display without claiming an active global RP chat
        try:
            st_session_bootstrap(ctx, {
                "character_name": loadable_cards[0]["file"],
                "mode": "play",
                "activate_rp": False,
            })
            session = ctx.sessions.load()
        except Exception:
            pass

    active_char = str(session.get("character_name") or "")
    messages: list[dict[str, Any]] = []
    header: dict[str, Any] = {"chat_metadata": {"variables": {}}}
    chat_files_info: list[dict[str, Any]] = []
    active_chat_file = ""
    active_card_data: dict[str, Any] = {}
    active_card_file = ""
    expressions: list[str] = []
    current_expression: str | None = None
    dump_data: dict[str, Any] = {}
    doctor_data: dict[str, Any] = {"healthy": True, "total_issues": 0, "card_issues": [], "lorebook_issues": []}

    plugin_cfg = ctx.load_plugin_settings()
    byok_cfg = resolve_byok_config(plugin_cfg, ctx.state_dir)

    if active_char:
        try:
            c_query = str(session.get("card_file") or active_char)
            c_path, c_bundle, resolved_char, avatar_stem = resolve_card_and_identity(ctx, c_query, session)
            active_char = resolved_char
            active_card_file = c_path.name
            active_card_data = c_bundle["card"]["data"]
            expressions = list_character_expressions(ctx.st_data_root, Path(active_card_file).stem) or list_character_expressions(
                ctx.st_data_root, active_char
            )
            all_chats = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=active_char)
            for cp in all_chats[:20]:
                chat_files_info.append({
                    "file": cp.name,
                    "mtime": cp.stat().st_mtime,
                    "size": cp.stat().st_size,
                })
            c_target = (
                Path(session["chat_path"])
                if session.get("chat_path") and Path(session["chat_path"]).exists()
                else (all_chats[0] if all_chats else None)
            )
            if c_target is not None:
                active_chat_file = c_target.name
                header, messages = read_jsonl_chat(c_target)
            last_char_msgs = [m for m in messages if not m.get("is_user")]
            if last_char_msgs:
                last_m = last_char_msgs[-1]
                current_expression = (
                    last_m.get("extra", {}).get("expression")
                    or detect_message_emotion(str(last_m.get("mes", "")), expressions)
                )
            elif expressions:
                current_expression = "neutral" if "neutral" in expressions else expressions[0]

            active_cache_mode = byok_cfg["cache_mode"] if plugin_cfg.get("generation_backend") == "byok" else "off"
            dump_data = st_dump_prompt_frame(ctx, {"character_name": active_card_file, "cache_mode": active_cache_mode})
            doctor_data = st_lint_card_and_lore(ctx, {"character_name": active_card_file})
        except Exception as err:
            dump_data = {"dump_report": f"Inspector error: {err}", "active_lore": [], "prefix_cache_ratio_pct": 0}

    lorebooks = (
        sorted((p.stem for p in worlds_dir.glob("*.json")), key=lambda s: (s.lower(), s))
        if worlds_dir.exists()
        else []
    )
    suggestions_obj: dict[str, Any] = {}
    if ctx.stage_suggestions_file.exists():
        try:
            suggestions_obj = json.loads(ctx.stage_suggestions_file.read_text(encoding="utf-8-sig"))
        except Exception:
            suggestions_obj = {}

    acked_warn_cards = set(plugin_cfg.get("minor_warn_acked_cards") or [])
    active_minor_gate = dict(
        doctor_data.get("minor_risk_gate")
        or {"gate_action": "pass", "blocked": False, "red_screen_warn": False, "signals": [], "reasons": []}
    )
    active_minor_gate["card_file"] = active_card_file
    active_minor_gate["character_name"] = active_char
    active_minor_gate["warn_acked"] = bool(
        active_minor_gate.get("gate_action") == "red_screen_warn" and active_card_file in acked_warn_cards
    )

    meta = header.get("chat_metadata", {})
    return {
        "data_root_name": ctx.st_data_root.name,
        "plugin_settings": plugin_cfg,
        "byok": {**byok_cfg, "last_usage": session.get("last_byok_usage")},
        "first_run_notice": FIRST_RUN_NOTICE,
        "minor_risk_gate": active_minor_gate,
        "presets": list_openai_presets(ctx.st_data_root),
        "lorebooks": lorebooks,
        "groups": list_groups(ctx.st_data_root),
        "cards": cards_summary,
        "session": {
            "active": bool(session.get("active", False)),
            "rp_conv_id": str(session.get("rp_conv_id") or ""),
            "character_name": active_char,
            "card_file": active_card_file,
            "chat_file": active_chat_file,
            "mode": str(session.get("mode", "play")),
            "epistemic_level": normalize_epistemic_level(session.get("epistemic_level", 0)),
            "agentic_hands": bool(session.get("agentic_hands", False)),
            "director_mode": bool(session.get("director_mode", False)),
            "persona_name": str(session.get("persona_name") or get_active_persona(ctx.st_data_root)["name"]),
            "preset_name": str(session.get("preset_name") or "Default"),
            "stage_busy": bool(session.get("stage_busy", False)),
            "pending_uncommitted_turn": bool(session.get("pending_uncommitted_turn", False)),
            "pending_turn_mode": str(session.get("pending_turn_mode", "normal")),
            "last_persistence_error": session.get("last_persistence_error") or None,
            "authors_note": str(meta.get("note_prompt", "")),
            "an_depth": int(meta.get("note_depth", 4)),
        },
        "active_card": {
            "name": active_card_data.get("name", ""),
            "description": active_card_data.get("description", ""),
            "personality": active_card_data.get("personality", ""),
            "scenario": active_card_data.get("scenario", ""),
            "system_prompt": active_card_data.get("system_prompt", ""),
            "world": active_card_data.get("extensions", {}).get("world", "") if isinstance(active_card_data.get("extensions"), dict) else "",
            "alternate_greetings_count": len(active_card_data.get("alternate_greetings") or []),
        },
        "expressions": expressions,
        "current_expression": current_expression,
        "chats": chat_files_info,
        "variables": meta.get("variables", {}),
        "messages": messages,
        "dump": dump_data,
        "doctor": doctor_data,
        "suggestions": suggestions_obj,
    }


def sidecar_prepare_agent_turn(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Prepares session state & returns the formatted prompt to send to Antigravity built-in model."""
    session = ctx.sessions.load()
    char_name = str(args.get("character_name") or session.get("character_name") or "").strip()
    if not char_name:
        raise ValueError("Select a character before dispatching to the Antigravity model.")
    if session.get("character_name") != char_name or not session.get("chat_path"):
        st_session_bootstrap(ctx, {"character_name": char_name, "mode": args.get("mode", "play")})
        session = ctx.sessions.load()

    c_check_path, c_check_bundle, _, _ = resolve_card_and_identity(ctx, 
        str(session.get("card_file") or char_name), session
    )
    prep_minor_gate = evaluate_minor_risk_gate(c_check_bundle)
    if prep_minor_gate["gate_action"] == "block_load":
        raise ValueError(
            f"[Card Doctor 未成年风险闸] 角色卡「{char_name}」({c_check_path.name}) 触发拒载保护："
            f"{'；'.join(prep_minor_gate['reasons'])}"
        )

    action_kind = str(args.get("action_kind", "send_turn"))
    user_text = str(args.get("user_text", "")).strip()
    mode = str(session.get("mode", "play"))
    rp_conv_id = str(args.get("rp_conv_id") or session.get("rp_conv_id") or "").strip()

    ok, detail = evaluate_policy_compliance(user_text, state_dir=ctx.state_dir)
    if not ok:
        raise ValueError(detail)

    is_director_turn = action_kind == "director_intervention" or (
        action_kind == "send_turn" and bool(session.get("director_mode", False))
    )

    if is_director_turn:
        if not user_text:
            raise ValueError("Enter a director command or meta-discussion message first.")
        # Append backstage floor with `extra.is_backstage: true` so it renders as a collapsible Backstage Note
        # and is automatically quarantined when returning to 0%/50% immersion!
        st_stage_floor_op(ctx, {
            "character_name": char_name,
            "operation": "append_user_floor",
            "new_text": user_text,
            "extra_meta": {"is_backstage": True, "source": "tavern_stage_director"},
        })
        prompt_to_agent = f"[STAGE_DIRECTOR_INTERVENTION] {user_text}"
        session["active"] = True
        session["rp_conv_id"] = rp_conv_id
        session["stage_busy"] = True
        session["pending_uncommitted_turn"] = True
        session["pending_turn_mode"] = "director"
        session["stop_retry_count"] = 0
        session["last_user_input"] = user_text

    elif action_kind == "send_turn":
        if not user_text:
            raise ValueError("Message text cannot be empty.")
        # Append user floor immediately so the Sidecar UI shows user bubble right away!
        st_stage_floor_op(ctx, {
            "character_name": char_name,
            "operation": "append_user_floor",
            "new_text": user_text,
        })
        prompt_to_agent = user_text
        session["active"] = True
        session["rp_conv_id"] = rp_conv_id
        session["stage_busy"] = True
        session["pending_uncommitted_turn"] = True
        session["pending_turn_mode"] = "normal"
        session["stop_retry_count"] = 0
        session["last_user_input"] = user_text

    elif action_kind == "swipe_reroll":
        guidance = f" Direction hint: {user_text}" if user_text else ""
        prompt_to_agent = (
            f"[STAGE_SWIPE_REROLL] Draft one fresh, dramatically distinct alternative reply for {char_name} "
            f"responding to the latest scene in `{Path(session['chat_path']).name}` (--{mode} mode). "
            f"Output ONLY {char_name}'s in-character prose (\"...\" and *...*) without calling any tools.{guidance}"
        )
        session["active"] = True
        session["rp_conv_id"] = rp_conv_id
        session["stage_busy"] = True
        session["pending_uncommitted_turn"] = True
        session["pending_turn_mode"] = "swipe"
        session["stop_retry_count"] = 0

    elif action_kind == "kartoittaja_branches":
        prompt_to_agent = (
            f"[STAGE_KARTOITTAJA_BRANCHES] Invoke Kartoittaja branching workflow for {char_name}: draft 3 genuinely distinct "
            f"dramatic continuations (e.g. guarded, bold, intimate/reflective) for the latest floor and quietly append each "
            f"using `st_commit_turn(character_name=\"{char_name}\", char_mes=..., swipe_mode=True)` without engineering preamble."
        )
        session["active"] = True
        session["rp_conv_id"] = rp_conv_id
        session["stage_busy"] = True
        session["pending_uncommitted_turn"] = True
        session["pending_turn_mode"] = "kartoittaja"
        session["stop_retry_count"] = 0

    elif action_kind == "suggest_replies":
        prompt_to_agent = (
            f"[STAGE_SUGGEST_REPLIES] Based on {char_name}'s latest floor in `{Path(session['chat_path']).name}`, "
            f"propose 3 distinct in-character reply options for {{{{user}}}} (formatted with *actions* and \"dialogue\") "
            f"and quietly publish them via `st_publish_stage_suggestions(kind=\"reply_suggestions\", suggestions=[...])` "
            f"without engineering preamble. Do not call `st_commit_turn`."
        )
        session["active"] = True
        session["rp_conv_id"] = rp_conv_id
        session["stage_busy"] = True
        session["pending_uncommitted_turn"] = False
        session["pending_turn_mode"] = "suggestions"

    elif action_kind == "expand_intent":
        if not user_text:
            raise ValueError("Enter a brief player intent in the composer first to expand it.")
        prompt_to_agent = (
            f"[STAGE_EXPAND_INTENT] Expand {{{{user}}}}'s brief action/dialogue intent into 3 polished roleplay variations "
            f"(preserving player intent without speaking for {char_name}): `{user_text}`. "
            f"Quietly publish via `st_publish_stage_suggestions(kind=\"expanded_intent\", suggestions=[...])` without engineering preamble or `st_commit_turn`."
        )
        session["active"] = True
        session["rp_conv_id"] = rp_conv_id
        session["stage_busy"] = True
        session["pending_uncommitted_turn"] = False
        session["pending_turn_mode"] = "suggestions"
    else:
        raise ValueError(f"Unknown prepare_agent_turn kind: {action_kind}")

    already_flag = action_kind in ("send_turn", "director_intervention") or is_director_turn
    prep_updates: dict[str, Any] = {
        "active": True,
        "rp_conv_id": rp_conv_id,
        "stage_busy": True,
        "pending_uncommitted_turn": session["pending_uncommitted_turn"],
        "pending_turn_mode": session["pending_turn_mode"],
        "user_floor_already_appended": already_flag,
        "pending_sidecar_launch": {
            "prompt": prompt_to_agent,
            "action_kind": "director_intervention" if is_director_turn else action_kind,
            "user_text": user_text,
            "user_floor_already_appended": already_flag,
            "ts": time.time(),
        },
    }
    if "stop_retry_count" in session:
        prep_updates["stop_retry_count"] = session["stop_retry_count"]
    if "last_user_input" in session:
        prep_updates["last_user_input"] = session["last_user_input"]
    ctx.sessions.update(prep_updates)

    return {
        "ready": True,
        "character_name": char_name,
        "action_kind": action_kind,
        "prompt": prompt_to_agent,
        "gentle_notice": detail.removeprefix("gentle_notice:") if detail.startswith("gentle_notice:") else "",
    }


class _StreamPreviewWriter:
    """Throttled writer for the live BYOK preview file polled by the Stage sidecar."""

    MIN_INTERVAL_S = 0.12

    def __init__(self, path: Path, action_kind: str) -> None:
        self.path = path
        self.action_kind = action_kind
        self.started_at = time.time()
        self._last_write = 0.0
        self._pending = ""

    def write(self, text: str, force: bool = False) -> None:
        self._pending = text
        now = time.time()
        if not force and now - self._last_write < self.MIN_INTERVAL_S:
            return
        self._last_write = now
        try:
            atomic_write_json(self.path, {
                "active": True,
                "action_kind": self.action_kind,
                "started_at": self.started_at,
                "text": self._pending,
            }, indent=0)
        except OSError:
            pass  # The preview is cosmetic; never fail the turn over it.


def _is_active_stream_preview(preview_path: Path, max_age_s: float = 180.0) -> bool:
    if not preview_path.is_file():
        return False
    try:
        obj = json.loads(preview_path.read_text(encoding="utf-8-sig"))
        if not isinstance(obj, dict) or not obj.get("active"):
            return False
        return (time.time() - float(obj.get("started_at") or 0.0)) < max_age_s
    except Exception:
        return False


def sidecar_cancel_turn(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Signals an in-flight BYOK turn to abort via `byok_cancel.flag`, or force-resets stuck busy state."""
    force_reset = bool(args.get("force_reset", False))
    cancel_path = Path(ctx.state_dir) / BYOK_CANCEL_FILE
    preview_path = Path(ctx.state_dir) / STREAM_PREVIEW_FILE
    sess = ctx.sessions.load() if ctx.sessions.exists() else {}
    is_active_byok = _is_active_stream_preview(preview_path, max_age_s=600.0) or (
        bool(sess.get("stage_busy")) and not bool(sess.get("pending_uncommitted_turn"))
    )
    atomic_write_text(cancel_path, str(time.time()))
    if force_reset or not is_active_byok:
        preview_path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)
        if ctx.sessions.exists():
            ctx.sessions.update({
                "stage_busy": False,
                "pending_uncommitted_turn": False,
                "user_floor_already_appended": False,
                "pending_sidecar_launch": None,
            })
    return {"cancelled": True, "force_reset": force_reset}


def sidecar_retry_uncommitted_turn(ctx: RuntimeContext) -> dict[str, Any]:
    """Replays `last_persistence_error.uncommitted_turn` saved by the Stop hook on lock contention."""
    sess = ctx.sessions.load()
    err_info = sess.get("last_persistence_error")
    uncommitted = err_info.get("uncommitted_turn") if isinstance(err_info, dict) else None
    if not isinstance(uncommitted, dict) or not str(uncommitted.get("char_mes") or "").strip():
        raise ValueError("当前没有待重试落盘的回复记录。")
    commit_res = st_commit_turn(ctx, dict(uncommitted))

    def _clear_err(cur: dict[str, Any]) -> dict[str, Any]:
        cur.pop("last_persistence_error", None)
        cur["persistence_failure_count"] = 0
        return cur

    ctx.sessions.update(_clear_err)
    return {"retried": True, "commit": commit_res}


def sidecar_byok_turn(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Runs one stage turn through the user's own OpenAI-compatible API key instead of the host model.

    Reuses `sidecar_prepare_agent_turn` (policy gate, minor-risk gate, user floor append) and
    `tool_st_dump_prompt_frame` (the same ST_RUNTIME_FRAME the PreInvocation hook injects), then
    commits the reply directly via `tool_st_commit_turn` -- no host conversation, no Stop hook.
    """
    action_kind = str(args.get("action_kind", "send_turn"))
    if action_kind not in BYOK_SUPPORTED_ACTIONS:
        raise ValueError(f"BYOK 后端暂只支持发送回合与重抽（Swipe），不支持「{action_kind}」。")
    init_sess = ctx.sessions.load()
    if init_sess.get("director_mode"):
        raise ValueError("BYOK 后端暂不支持导演穿梭模式，请先关闭导演模式或切回宿主后端。")

    preview_path = Path(ctx.state_dir) / STREAM_PREVIEW_FILE
    cancel_path = Path(ctx.state_dir) / BYOK_CANCEL_FILE
    if _is_active_stream_preview(preview_path, max_age_s=180.0):
        raise ValueError("上一轮 BYOK 生成仍在进行中，请等待完成或先点击停止。")
    cancel_path.unlink(missing_ok=True)

    cfg = resolve_byok_config(ctx.load_plugin_settings(), ctx.state_dir)
    api_key = read_byok_api_key(ctx.state_dir)
    if not cfg["ready"]:
        missing = []
        if not cfg["key_present"]:
            missing.append("API Key（环境变量 TAVERN_BYOK_API_KEY 或状态目录 byok_api_key 文件）")
        if not cfg["model"]:
            missing.append("模型名")
        raise ValueError("BYOK 后端未配置完整：缺少 " + "、".join(missing))

    pre_chat_path = Path(init_sess["chat_path"]) if init_sess.get("chat_path") and Path(init_sess["chat_path"]).exists() else None
    pre_floor_count: int | None = None
    if pre_chat_path is not None:
        try:
            _, pre_msgs = read_jsonl_chat(pre_chat_path)
            pre_floor_count = len(pre_msgs)
        except Exception:
            pre_floor_count = None

    prep_args = {k: v for k, v in args.items() if k != "rp_conv_id"}
    prep_args["action_kind"] = action_kind
    prep = sidecar_prepare_agent_turn(ctx, prep_args)
    session = ctx.sessions.load()
    already_appended = bool(session.get("user_floor_already_appended", False))
    active_chat_path = Path(session["chat_path"]) if session.get("chat_path") else None
    appended_new_user_floor = False
    if already_appended and active_chat_path is not None and active_chat_path.exists():
        if pre_chat_path != active_chat_path:
            appended_new_user_floor = True
        elif pre_floor_count is not None:
            try:
                _, post_prep_msgs = read_jsonl_chat(active_chat_path)
                appended_new_user_floor = len(post_prep_msgs) == pre_floor_count + 1
            except Exception:
                appended_new_user_floor = False

    # Clear both `pending_sidecar_launch` and `pending_uncommitted_turn` immediately so host
    # PreToolUse / Stop hooks in a bound conversation never collide with an in-flight BYOK turn.
    ctx.sessions.update({
        "pending_sidecar_launch": None,
        "pending_uncommitted_turn": False,
    })

    def _check_cancelled() -> None:
        if cancel_path.exists():
            cancel_path.unlink(missing_ok=True)
            raise ValueError("BYOK 生成已取消")

    committed = False
    try:
        cache_mode = cfg["cache_mode"]
        frame = st_dump_prompt_frame(ctx, {
            "character_name": session.get("card_file") or prep["character_name"],
            # For `send_turn`, the user floor is already on disk; for `swipe_reroll`, any direction
            # hint belongs strictly in `prep["prompt"]` rather than as a fake in-scene user floor.
            "user_input": "",
            "_swipe_reroll": action_kind == "swipe_reroll",
            "_exclude_trailing_user": already_appended and cache_mode == "off",
            "agentic_hands": False,
            "cache_mode": cache_mode,
            "_include_parts": cache_mode != "off",
        })
        if cache_mode != "off":
            messages = build_byok_cached_messages(
                frame["frame_parts"],
                prep["prompt"],
                cache_control=cfg["cache_control"],
                user_already_in_history=already_appended,
            )
        else:
            messages = build_byok_messages(frame["ephemeral_frame"], prep["prompt"])
        preset = load_openai_preset(ctx.st_data_root, session.get("preset_name"))
        call_kwargs = {
            "base_url": cfg["base_url"],
            "api_key": api_key,
            "model": cfg["model"],
            "messages": messages,
            "temperature": preset.get("temperature"),
            "max_tokens": int(args["max_tokens"]) if args.get("max_tokens") else None,
        }
        _check_cancelled()
        if cfg["stream"]:
            preview = _StreamPreviewWriter(preview_path, action_kind)
            preview.write("", force=True)

            def _on_stream_delta(text_so_far: str) -> None:
                _check_cancelled()
                preview.write(text_so_far)

            reply, usage = byok_chat_completion_stream(on_delta=_on_stream_delta, **call_kwargs)
        else:
            reply, usage = byok_chat_completion_with_usage(**call_kwargs)
        _check_cancelled()
        extra_meta: dict[str, Any] = {"generation_backend": "byok", "byok_model": cfg["model"], "byok_cache_mode": cache_mode}
        if usage.get("prompt_tokens") is not None:
            extra_meta["byok_prompt_tokens"] = usage["prompt_tokens"]
        if usage.get("cached_tokens") is not None:
            extra_meta["byok_cached_tokens"] = usage["cached_tokens"]
        commit = st_commit_turn(ctx, {
            "character_name": session.get("card_file") or prep["character_name"],
            "user_mes": None,
            "char_mes": reply,
            "swipe_mode": action_kind == "swipe_reroll",
            "extra_meta": extra_meta,
        })
        committed = True
        ctx.sessions.update({"last_byok_usage": {**usage, "cache_mode": cache_mode, "at": int(time.time())}})
    finally:
        if not committed and appended_new_user_floor and active_chat_path is not None and active_chat_path.exists():
            clean_u = str(args.get("user_text", "")).strip()
            try:
                with exclusive_file_lock(active_chat_path):
                    rb_hdr, rb_msgs = read_jsonl_chat(active_chat_path)
                    if (
                        len(rb_msgs) > 1
                        and bool(rb_msgs[-1].get("is_user"))
                        and str(rb_msgs[-1].get("mes", "")).strip() == clean_u
                    ):
                        rb_msgs.pop()
                        write_jsonl_chat_atomic(active_chat_path, rb_hdr, rb_msgs)
            except Exception:
                pass
        # The committed floor (or the error toast) replaces the live preview.
        preview_path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)
        # Never leave the stage busy or a pending turn for the host Stop hook to pick up.
        ctx.sessions.update({
            "pending_uncommitted_turn": False,
            "user_floor_already_appended": False,
            "stage_busy": False,
            "pending_sidecar_launch": None,
        })
    return {
        "committed": True,
        "backend": "byok",
        "model": cfg["model"],
        "character_name": prep["character_name"],
        "turn_hash": commit.get("turn_hash"),
        "gentle_notice": prep.get("gentle_notice", ""),
        "cache_mode": cache_mode,
        "usage": usage,
    }


def sidecar_read_lorebook(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    worlds_dir = (ctx.st_data_root / "worlds").resolve()
    worlds_dir.mkdir(parents=True, exist_ok=True)
    b_name = str(args.get("lorebook_name") or "").strip()
    if not b_name:
        files = sorted(worlds_dir.glob("*.json"), key=lambda item: (item.name.lower(), item.name))
        if not files:
            return {"lorebook_name": "", "entries": [], "issues": []}
        target = files[0]
    else:
        fname = b_name if b_name.endswith(".json") else f"{b_name}.json"
        target = ensure_under_dir(worlds_dir / fname, worlds_dir)
    entries = load_lorebook_file(target)
    issues = lint_lorebook_file(target)
    return {
        "lorebook_name": target.stem,
        "entries": [
            {
                "uid": e.uid,
                "comment": e.comment,
                "keys": e.keys,
                "selective": e.selective,
                "keys_secondary": e.keys_secondary,
                "selective_logic": e.selective_logic,
                "content": e.content,
                "constant": e.constant,
                "vectorized": e.vectorized,
                "enabled": e.enabled,
                "order": e.order,
                "position": e.position,
                "depth": e.depth,
                "sticky": e.sticky,
                "cooldown": e.cooldown,
            }
            for e in sorted(entries, key=lambda x: (x.order, x.uid))
        ],
        "issues": issues,
    }


def sidecar_import_card(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    filename = str(args.get("filename") or "imported_card.png").strip()
    b64_data = str(args.get("base64_data") or "").strip()
    if not b64_data:
        raise ValueError("Missing base64_data for card import.")
    if "," in b64_data and b64_data.startswith("data:"):
        b64_data = b64_data.split(",", 1)[1]
    raw_bytes = base64.b64decode(b64_data)

    char_dir = (ctx.st_data_root / "characters").resolve()
    char_dir.mkdir(parents=True, exist_ok=True)
    stem = sanitize_filename_stem(Path(filename).stem)
    out_png = ensure_under_dir(char_dir / f"{stem}.png", char_dir)

    if filename.lower().endswith(".json") or raw_bytes.lstrip().startswith((b"{", b"\xef\xbb\xbf{")):
        card_obj = json.loads(raw_bytes.decode("utf-8-sig"))
        minor_gate = evaluate_minor_risk_gate(card_obj)
        if minor_gate["gate_action"] == "block_load":
            raise ValueError(
                f"[Card Doctor 未成年风险闸] 默认拒绝导入该角色卡：{'；'.join(minor_gate['reasons'])}"
            )
        res = write_character_card_png(card_obj, out_png)
    else:
        # Parse & re-export through Seppä codec so dual chunks & Card-is-Data sanitization apply!
        tmp_in = ctx.state_dir / f".import_{stem}.png"
        try:
            tmp_in.write_bytes(raw_bytes)
            parsed = read_character_card(tmp_in, include_avatar_data_uri=False)
            minor_gate = evaluate_minor_risk_gate(parsed)
            if minor_gate["gate_action"] == "block_load":
                raise ValueError(
                    f"[Card Doctor 未成年风险闸] 默认拒绝导入该角色卡：{'；'.join(minor_gate['reasons'])}"
                )
            res = write_character_card_png(parsed["card"], out_png, base_png_bytes=raw_bytes)
        finally:
            tmp_in.unlink(missing_ok=True)

    st_session_bootstrap(ctx, {"character_name": out_png.name, "mode": "play"})
    res["minor_risk_gate"] = minor_gate
    return res
