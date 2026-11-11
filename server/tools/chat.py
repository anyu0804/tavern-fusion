"""Chat tools: `st_commit_turn`, `st_manage_chat_branch`, `st_stage_floor_op`, `st_manage_variables`."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from ..card_codec import (
    detect_message_emotion,
    list_character_expressions,
)
from ..chat_sync import (
    append_turn_to_chat,
    create_new_chat_session,
    exclusive_file_lock,
    get_character_chat_dir,
    list_chat_files,
    manage_chat_branch,
    read_jsonl_chat,
    stage_floor_operation,
    write_jsonl_chat_atomic,
)
from ..fs_safety import atomic_write_json, ensure_under_dir
from ..policy import evaluate_policy_compliance
from ..prompt import (
    get_active_persona,
    load_st_settings,
    normalize_epistemic_level,
    resolve_st_macros,
    run_regex_pipeline,
)
from ..runtime.context import RuntimeContext
from ._common import collect_merged_lore_entries, resolve_card_and_identity

# MCP input schemas (verbatim from the pre-refactor MCP_TOOL_SCHEMAS list)
ST_COMMIT_TURN_SCHEMA: dict[str, Any] = {
    "name": "st_commit_turn",
    "description": "Cleanses RP prose with SillyTavern Regex rules, substitutes macros, infers expression sprite, and atomically appends user + character messages (or a new Swipe) to the active .jsonl file.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string"},
            "user_mes": {"type": "string"},
            "char_mes": {"type": "string"},
            "swipe_mode": {"type": "boolean", "default": False},
            "variables_delta": {"type": "object"},
            "extra_meta": {"type": "object"},
        },
        "required": ["char_mes"],
    },
}

ST_MANAGE_CHAT_BRANCH_SCHEMA: dict[str, Any] = {
    "name": "st_manage_chat_branch",
    "description": "Creates a new chat, switches Swipe index on the latest floor, forks a checkpoint branch .jsonl, or rolls back floors.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string"},
            "action": {"type": "string", "enum": ["new", "switch_swipe", "fork_checkpoint", "rollback"]},
            "target_id": {"type": "integer", "default": 0},
        },
        "required": ["character_name", "action"],
    },
}

ST_STAGE_FLOOR_OP_SCHEMA: dict[str, Any] = {
    "name": "st_stage_floor_op",
    "description": "Interactive Stage floor revision tool: edits any floor in-place, deletes a floor, switches or prunes Swipes on any floor, or forks a branch from any floor.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "character_name": {"type": "string"},
            "chat_file": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": [
                    "edit_floor",
                    "delete_floor",
                    "switch_swipe",
                    "prune_swipes",
                    "fork_from_floor",
                    "append_user_floor",
                    "toggle_backstage",
                ],
            },
            "floor_index": {"type": "integer", "default": -1},
            "swipe_id": {"type": "integer", "default": 0},
            "new_text": {"type": "string"},
            "activate_fork": {"type": "boolean", "default": True},
        },
        "required": ["operation"],
    },
}

ST_MANAGE_VARIABLES_SCHEMA: dict[str, Any] = {
    "name": "st_manage_variables",
    "description": "Gets, sets, increments, deletes, or lists chat-scoped or global SillyTavern variables (compatible with /getvar and /setvar).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "enum": ["chat", "global"], "default": "chat"},
            "op": {"type": "string", "enum": ["get", "set", "inc", "del", "list"], "default": "list"},
            "key": {"type": "string"},
            "value": {},
        },
    },
}


def st_commit_turn(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    session = ctx.sessions.load()
    char_query = str(args.get("character_name") or session.get("card_file") or session.get("character_name") or "").strip()
    if not char_query:
        raise ValueError("character_name is required to commit a turn.")

    card_path, card_bundle, char_name, avatar_stem = resolve_card_and_identity(ctx, char_query, session)
    card_v3 = card_bundle["card"]
    persona = get_active_persona(ctx.st_data_root, session.get("persona_name"))

    chat_path_str = session.get("chat_path")
    if (
        chat_path_str
        and Path(chat_path_str).exists()
        and session.get("character_name") == char_name
        and session.get("avatar_stem", avatar_stem) == avatar_stem
    ):
        chat_path = Path(chat_path_str)
    else:
        existing = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=char_name)
        if existing:
            chat_path = existing[0]
        else:
            created = create_new_chat_session(
                ctx.st_data_root,
                character_name=char_name,
                user_name=persona["name"],
                greeting_text=resolve_st_macros(
                    str(card_v3["data"].get("first_mes", "")), char_name, persona["name"]
                ),
                avatar_stem=avatar_stem,
            )
            chat_path = Path(created["chat_path"])

    raw_user = args.get("user_mes")
    raw_char = str(args["char_mes"])

    ok, reason = evaluate_policy_compliance(raw_char, state_dir=ctx.state_dir)
    if not ok:
        raise ValueError(f"Turn commit blocked by policy gate: {reason}")

    header, messages = read_jsonl_chat(chat_path)
    vars_dict = header.get("chat_metadata", {}).get("variables", {})
    if isinstance(args.get("variables_delta"), dict):
        vars_dict.update(args["variables_delta"])
    pre_macro_vars = dict(vars_dict)

    extra_meta = dict(args.get("extra_meta") or {})
    is_backstage = bool(extra_meta.get("is_backstage", False) or session.get("director_mode", False))
    if is_backstage:
        extra_meta["is_backstage"] = True
    epistemic_level = normalize_epistemic_level(session.get("epistemic_level", 0))

    cleaned_char = run_regex_pipeline(
        raw_char,
        st_data_root=ctx.st_data_root,
        card_extensions=card_v3["data"].get("extensions"),
        is_storage_pass=True,
        epistemic_level=epistemic_level,
        is_backstage=is_backstage,
    )
    cleaned_char = resolve_st_macros(cleaned_char, char_name, persona["name"], vars_dict)
    cleaned_user = (
        resolve_st_macros(str(raw_user), char_name, persona["name"], vars_dict)
        if raw_user is not None
        else None
    )
    # Persist `{{setvar::k::v}}` macros applied during macro expansion: `append_turn_to_chat`
    # re-reads the chat from disk, so in-memory mutations must be diffed into the commit delta
    # or they would be silently lost.
    macro_setvar_delta = {
        k: v for k, v in vars_dict.items() if k not in pre_macro_vars or pre_macro_vars[k] != v
    }

    entries = collect_merged_lore_entries(ctx, card_v3, session.get("extra_lorebooks"))
    lore_eval = ctx.lore_engine.evaluate_and_splice(
        entries=entries,
        chat_messages=messages,
        latest_user_input=cleaned_user or "",
        timed_state=header.get("chat_metadata", {}).get("timed_effects"),
        quarantine_backstage=(epistemic_level < 100 and not is_backstage),
    )

    expr_list = list_character_expressions(ctx.st_data_root, avatar_stem) or list_character_expressions(
        ctx.st_data_root, char_name
    )
    detected_expr = detect_message_emotion(cleaned_char, expr_list)
    if detected_expr:
        extra_meta["expression"] = detected_expr

    commit_res = append_turn_to_chat(
        chat_path=chat_path,
        character_name=char_name,
        user_name=persona["name"],
        user_mes=cleaned_user,
        char_mes=cleaned_char,
        swipe_mode=bool(args.get("swipe_mode", False)),
        variables_delta={**(args.get("variables_delta") or {}), **macro_setvar_delta},
        next_timed_state=lore_eval["next_timed_state"],
        extra_meta=extra_meta,
    )
    commit_res["expression"] = detected_expr

    ctx.sessions.update({
        "active": True,
        "character_name": char_name,
        "card_file": card_path.name,
        "avatar_stem": avatar_stem,
        "card_path": str(card_path),
        "chat_path": str(chat_path),
        "last_committed_at": time.time(),
        "last_turn_hash": commit_res["turn_hash"],
        "pending_uncommitted_turn": False,
        "user_floor_already_appended": False,
        "stage_busy": False,
    })
    return commit_res


def st_manage_chat_branch(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    sess = ctx.sessions.load()
    char_query = str(args["character_name"]).strip()
    card_path, card_bundle, char_name, avatar_stem = resolve_card_and_identity(ctx, char_query, sess)
    card_v3 = card_bundle["card"]
    action = str(args["action"])
    target_id = int(args.get("target_id", 0))

    if action == "new":
        persona = get_active_persona(ctx.st_data_root)
        created = create_new_chat_session(
            ctx.st_data_root,
            character_name=char_name,
            user_name=persona["name"],
            greeting_text=resolve_st_macros(card_v3["data"].get("first_mes", ""), char_name, persona["name"]),
            alternate_greetings=[
                resolve_st_macros(str(g), char_name, persona["name"])
                for g in (card_v3["data"].get("alternate_greetings") or [])
            ],
            greeting_index=target_id,
            avatar_stem=avatar_stem,
        )
        def _sync_new_chat_if_active(cur: dict[str, Any]) -> dict[str, Any]:
            if cur.get("character_name") == char_name and cur.get("avatar_stem", avatar_stem) == avatar_stem:
                cur["chat_path"] = created["chat_path"]
            return cur

        ctx.sessions.update(_sync_new_chat_if_active)
        return created

    if (
        sess.get("character_name") == char_name
        and sess.get("avatar_stem", avatar_stem) == avatar_stem
        and sess.get("chat_path")
        and Path(sess["chat_path"]).exists()
    ):
        chat_target = Path(sess["chat_path"])
    else:
        chats = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=char_name)
        if not chats:
            raise FileNotFoundError(f"No existing chats found for {char_name}.")
        chat_target = chats[0]
    return manage_chat_branch(ctx.st_data_root, char_name, chat_target, action, target_id)  # type: ignore[arg-type]


def st_stage_floor_op(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Executes in-place Stage floor operations (edit, delete, switch_swipe, prune_swipes, fork_from_floor)."""
    sess = ctx.sessions.load()
    char_q = str(args.get("character_name") or sess.get("card_file") or sess.get("character_name") or "").strip()
    if not char_q:
        raise ValueError("character_name is required for floor operations.")
    card_path, _, char_name, avatar_stem = resolve_card_and_identity(ctx, char_q, sess)

    chat_file = str(args.get("chat_file") or "").strip()
    if chat_file:
        if ".." in Path(chat_file).parts or Path(chat_file).name != chat_file:
            raise ValueError("Invalid chat_file name.")
        primary_dir = get_character_chat_dir(ctx.st_data_root, avatar_stem)
        cand_primary = ensure_under_dir(primary_dir / chat_file, primary_dir)
        if cand_primary.exists():
            chat_path = cand_primary
        else:
            legacy_dir = get_character_chat_dir(ctx.st_data_root, char_name)
            cand_legacy = ensure_under_dir(legacy_dir / chat_file, legacy_dir)
            chat_path = cand_legacy if cand_legacy.exists() else cand_primary
    elif (
        sess.get("character_name") == char_name
        and sess.get("avatar_stem", avatar_stem) == avatar_stem
        and sess.get("chat_path")
        and Path(sess["chat_path"]).exists()
    ):
        chat_path = Path(sess["chat_path"])
    else:
        chats = list_chat_files(ctx.st_data_root, avatar_stem, legacy_character_name=char_name)
        if not chats:
            raise FileNotFoundError(f"No chat log found for {char_name}.")
        chat_path = chats[0]

    persona = get_active_persona(ctx.st_data_root, sess.get("persona_name"))
    res = stage_floor_operation(
        chat_path=chat_path,
        character_name=char_name,
        operation=args["operation"],
        floor_index=int(args.get("floor_index", -1)),
        swipe_id=int(args.get("swipe_id", 0)),
        new_text=str(args.get("new_text", "")),
        user_name=persona["name"],
        extra_meta=args.get("extra_meta") if isinstance(args.get("extra_meta"), dict) else None,
    )
    if args["operation"] == "fork_from_floor" and args.get("activate_fork", True):
        ctx.sessions.update({
            "chat_path": res["branch_path"],
            "character_name": char_name,
            "card_file": card_path.name,
            "avatar_stem": avatar_stem,
        })
    return res


def st_manage_variables(ctx: RuntimeContext, args: dict[str, Any]) -> dict[str, Any]:
    scope = str(args.get("scope", "chat"))
    op = str(args.get("op", "list"))
    key = str(args.get("key", "")).strip()
    val = args.get("value")

    if scope == "global":
        settings_path = ctx.st_data_root / "settings.json"
        # Serialize the full read-modify-write inside the lock so a concurrently running
        # SillyTavern server (or a parallel hook/tool process) never clobbers updates with
        # a stale pre-lock snapshot.
        with exclusive_file_lock(settings_path):
            settings = load_st_settings(ctx.st_data_root)
            if op in ("set", "inc", "del") and settings_path.exists() and not settings:
                # `load_st_settings` returns {} on a parse error; writing back would wipe the user's settings.json.
                raise ValueError("settings.json exists but could not be parsed; refusing to overwrite it.")
            # SillyTavern keeps global variables at `extension_settings.variables.global`.
            ext_settings = settings.setdefault("extension_settings", {})
            g_vars = ext_settings.setdefault("variables", {}).setdefault("global", {})
            legacy = settings.get("variables")
            if isinstance(legacy, dict) and isinstance(legacy.get("global"), dict):
                for lk, lv in legacy["global"].items():
                    g_vars.setdefault(lk, lv)
            if op == "get":
                return {"scope": "global", "key": key, "value": g_vars.get(key)}
            if op == "set":
                g_vars[key] = val
            elif op == "inc":
                # `val` of 0 must increment by 0 (the old `val or 1` turned it into +1)
                inc_step = float(val) if val not in (None, "") else 1.0
                g_vars[key] = float(g_vars.get(key, 0)) + inc_step
            elif op == "del":
                g_vars.pop(key, None)
            if op in ("set", "inc", "del"):
                atomic_write_json(settings_path, settings, indent=2)
            return {"scope": "global", "variables": g_vars}

    session = ctx.sessions.load()
    chat_path = session.get("chat_path")
    if not chat_path or not Path(chat_path).exists():
        raise ValueError("No active chat session to read/write chat-scoped variables.")
    c_path_obj = Path(chat_path)
    with exclusive_file_lock(c_path_obj):
        header, messages = read_jsonl_chat(c_path_obj)
        c_vars = header.setdefault("chat_metadata", {}).setdefault("variables", {})
        if op == "get":
            return {"scope": "chat", "key": key, "value": c_vars.get(key)}
        if op == "set":
            c_vars[key] = val
        elif op == "inc":
            inc_step = float(val) if val not in (None, "") else 1.0
            num = float(c_vars.get(key, 0)) + inc_step
            c_vars[key] = int(num) if num.is_integer() else num
        elif op == "del":
            c_vars.pop(key, None)
        if op in ("set", "inc", "del"):
            write_jsonl_chat_atomic(c_path_obj, header, messages)
        return {"scope": "chat", "variables": dict(c_vars)}
