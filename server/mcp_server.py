#!/usr/bin/env python3
"""Zero-Dependency Python 3.12 MCP Server & Sidecar Control Plane (`mcp_server.py`).

Speaks standard Model Context Protocol (MCP `2024-11-05` / `2025-03-26`) JSON-RPC 2.0
over `stdin`/`stdout` (supporting both newline-delimited JSON and Content-Length framing),
plus `--cli <tool_name>` and `--sidecar-op <op_name>` modes used by Antigravity Lifecycle
Hooks (`hooks/*.py`) and the permanent `Tavern Stage Sidecar` (`sidecars/tavern_stage/`).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from server.card_codec import (
    detect_message_emotion,
    lint_character_card,
    list_character_expressions,
    read_character_card,
    write_character_card_png,
)
from server.chat_sync import (
    append_turn_to_chat,
    create_new_chat_session,
    exclusive_file_lock,
    get_character_chat_dir,
    list_chat_files,
    list_groups,
    manage_chat_branch,
    read_jsonl_chat,
    stage_floor_operation,
    write_jsonl_chat_atomic,
)
from server.fs_safety import atomic_write_json, ensure_under_dir, sanitize_filename_stem
from server.lore_engine import (
    HybridLoreEngine,
    NormalizedLoreEntry,
    evolve_lorebook_file,
    lint_lorebook_file,
    load_lorebook_file,
    parse_lorebook_dict,
)
from server.st_runtime_compat import (
    compile_st_prompt_frame,
    evaluate_policy_compliance,
    get_active_persona,
    list_openai_presets,
    load_openai_preset,
    load_st_settings,
    normalize_epistemic_level,
    resolve_policy_mode,
    resolve_st_macros,
    run_regex_pipeline,
)
from server.widget_compiler import compile_stage_widget_html


DEFAULT_STATE_DIR = Path.home() / ".gemini" / "antigravity" / "tavern-fusion" / "state"


class TavernFusionRuntime:
    """Stateful coordinator bridging Antigravity, MCP tools, Hooks, Sidecar, and SillyTavern disk data."""

    def __init__(self, st_data_root: str | Path | None = None, state_dir: str | Path | None = None) -> None:
        self._explicit_data_root = st_data_root
        raw_state = state_dir or os.environ.get("PLUGIN_STATE_DIR") or ""
        if str(raw_state) in ("${PLUGIN_DATA}", ""):
            raw_state = DEFAULT_STATE_DIR
        self.state_dir = Path(raw_state).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.st_data_root = self._resolve_data_root(st_data_root, self.state_dir)
        self.lore_engine = HybridLoreEngine(self.state_dir / "lore_vectors.sqlite")

    def close(self) -> None:
        self.lore_engine.close()

    @staticmethod
    def _resolve_data_root(explicit: str | Path | None, state_dir: Path | None = None) -> Path:
        remembered: str | None = None
        if state_dir is not None:
            settings_file = state_dir / "plugin_settings.json"
            if settings_file.exists():
                try:
                    s_obj = json.loads(settings_file.read_text(encoding="utf-8"))
                    if isinstance(s_obj, dict) and s_obj.get("last_st_data_root"):
                        remembered = str(s_obj["last_st_data_root"])
                except Exception:
                    remembered = None

        candidates = [
            explicit,
            os.environ.get("ST_DATA_ROOT"),
            os.environ.get("ST_USER_DATA_DIR"),
            remembered,
            Path.cwd() / "data" / "default-user",
            Path.cwd() / "SillyTavern" / "data" / "default-user",
            ROOT_DIR.parent / "SillyTavern" / "data" / "default-user",
            ROOT_DIR.parent / "data" / "default-user",
            ROOT_DIR / "examples" / "mock_st_data" / "default-user",
        ]
        for c in candidates:
            if c and not str(c).startswith("${"):
                p = Path(c)
                if p.exists() and p.is_dir():
                    return p.resolve()
        if explicit and not str(explicit).startswith("${"):
            p = Path(explicit)
            p.mkdir(parents=True, exist_ok=True)
            return p.resolve()
        fallback = ROOT_DIR / "examples" / "mock_st_data" / "default-user"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback.resolve()

    @property
    def active_session_file(self) -> Path:
        return self.state_dir / "active_session.json"

    @property
    def plugin_settings_file(self) -> Path:
        return self.state_dir / "plugin_settings.json"

    @property
    def stage_suggestions_file(self) -> Path:
        return self.state_dir / "stage_suggestions.json"

    def load_active_session(self) -> dict[str, Any]:
        if not self.active_session_file.exists():
            return {}
        try:
            return json.loads(self.active_session_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_active_session(self, session: dict[str, Any]) -> None:
        atomic_write_json(self.active_session_file, session, indent=2)

    def load_plugin_settings(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "policy_mode": resolve_policy_mode(state_dir=self.state_dir),
            "inference_channel": "antigravity_builtin",
        }
        if not self.plugin_settings_file.exists():
            return defaults
        try:
            loaded = json.loads(self.plugin_settings_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except Exception:
            pass
        defaults["policy_mode"] = resolve_policy_mode(str(defaults.get("policy_mode", "")))
        return defaults

    def save_plugin_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        cur = self.load_plugin_settings()
        cur.update(updates)
        cur["policy_mode"] = resolve_policy_mode(str(cur.get("policy_mode", "")))
        atomic_write_json(self.plugin_settings_file, cur, indent=2)
        return cur

    def find_character_card_path(self, character_query: str) -> Path:
        char_dir = self.st_data_root / "characters"
        char_dir.mkdir(parents=True, exist_ok=True)
        q_clean = character_query.strip()
        if ".." in q_clean or "/" in q_clean or "\\" in q_clean:
            # Allow full path ONLY if strictly inside char_dir
            cand_abs = Path(q_clean)
            if cand_abs.is_absolute() and cand_abs.exists():
                return ensure_under_dir(cand_abs, char_dir)
            raise ValueError("Invalid character query containing path separators.")

        direct = char_dir / q_clean
        if direct.exists() and direct.is_file():
            return ensure_under_dir(direct, char_dir)
        for ext in (".png", ".json"):
            cand = char_dir / f"{q_clean}{ext}"
            if cand.exists():
                return ensure_under_dir(cand, char_dir)
            prefixed = char_dir / f"default_{q_clean}{ext}"
            if prefixed.exists():
                return ensure_under_dir(prefixed, char_dir)

        q_low = q_clean.lower()
        for p in sorted(char_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in (".png", ".json"):
                if q_low in p.stem.lower():
                    return p
                try:
                    c_name = read_character_card(p, include_avatar_data_uri=False)["card"]["data"]["name"]
                    if q_low == c_name.lower() or q_low in c_name.lower():
                        return p
                except Exception:
                    continue
        raise FileNotFoundError(f"Character card matching '{character_query}' not found in {char_dir}.")

    def collect_merged_lore_entries(
        self,
        card_v3: dict[str, Any],
        extra_books: list[str] | None = None,
    ) -> list[NormalizedLoreEntry]:
        entries: list[NormalizedLoreEntry] = []
        data = card_v3.get("data", {})
        if isinstance(data.get("character_book"), dict):
            entries.extend(parse_lorebook_dict(data["character_book"], book_name="character_book", source_tier=2))

        ext = data.get("extensions", {}) if isinstance(data.get("extensions"), dict) else {}
        linked_world = str(ext.get("world", "")).strip()
        book_names: list[str] = []
        if linked_world:
            book_names.append(linked_world)
        if extra_books:
            for b in extra_books:
                if b and b not in book_names:
                    book_names.append(b)

        worlds_dir = self.st_data_root / "worlds"
        if not book_names and worlds_dir.exists():
            settings = load_st_settings(self.st_data_root)
            # SillyTavern stores this at `world_info_settings.world_info.globalSelect`;
            # top-level `world_info.globalSelect` is kept as a fallback for older mock data.
            wi_settings = settings.get("world_info_settings")
            wi_block = wi_settings.get("world_info") if isinstance(wi_settings, dict) else None
            if not isinstance(wi_block, dict):
                wi_block = settings.get("world_info") if isinstance(settings.get("world_info"), dict) else {}
            selected_global = wi_block.get("globalSelect", []) or []
            if selected_global:
                book_names.extend(selected_global)
            else:
                book_names.extend(p.stem for p in sorted(worlds_dir.glob("*.json"))[:5])

        for b_name in book_names:
            fname = b_name if b_name.endswith(".json") else f"{b_name}.json"
            b_path = worlds_dir / fname
            if b_path.exists():
                tier = 2 if b_name == linked_world else 3
                entries.extend(load_lorebook_file(b_path, source_tier=tier))
        return entries

    def _resolve_card_and_identity(
        self,
        char_query: str,
        session: dict[str, Any] | None = None,
        *,
        include_avatar_data_uri: bool = False,
    ) -> tuple[Path, dict[str, Any], str, str]:
        """Resolves `(card_path, card_bundle, char_name, avatar_stem)`.

        When `char_query` refers to the currently bound character in `session` and
        `session["card_file"]` exists on disk, prefers that exact PNG file so cards
        whose filename differs from `data.name` stay deterministically paired.
        """
        q_clean = str(char_query).strip()
        card_path: Path | None = None
        if session:
            sess_file = str(session.get("card_file") or "").strip()
            sess_char = str(session.get("character_name") or "").strip()
            sess_stem = str(session.get("avatar_stem") or "").strip()
            if sess_file and q_clean in (sess_char, sess_file, sess_stem):
                cand = (self.st_data_root / "characters" / sess_file).resolve()
                if cand.exists() and cand.is_file():
                    card_path = ensure_under_dir(cand, self.st_data_root / "characters")
        if card_path is None:
            card_path = self.find_character_card_path(q_clean)
        bundle = read_character_card(card_path, include_avatar_data_uri=include_avatar_data_uri)
        char_name = str(bundle["card"]["data"]["name"])
        avatar_stem = card_path.stem
        return card_path, bundle, char_name, avatar_stem

    def _validate_contained_path(
        self,
        raw_path: str | Path,
        *,
        default_subdir: str | None = None,
        allowed_suffixes: tuple[str, ...] = (),
        label: str = "path",
    ) -> Path:
        raw_str = str(raw_path).strip()
        if not raw_str:
            raise ValueError(f"{label} cannot be empty.")
        cand = Path(raw_str)
        if ".." in cand.parts:
            raise ValueError(f"Path traversal ('..') is forbidden in {label}.")
        if allowed_suffixes and cand.suffix.lower() not in allowed_suffixes:
            raise ValueError(f"{label} must have one of suffixes {allowed_suffixes}, got '{cand.suffix}'.")

        allowed_roots = [
            self.state_dir.resolve(),
            self.st_data_root.resolve(),
            Path.cwd().resolve(),
        ]
        if not cand.is_absolute():
            base = (self.state_dir / default_subdir).resolve() if default_subdir else self.state_dir.resolve()
            base.mkdir(parents=True, exist_ok=True)
            return ensure_under_dir(base / cand, base)

        resolved = cand.resolve()
        for root in allowed_roots:
            if resolved.is_relative_to(root):
                return resolved
        raise ValueError(
            f"Path containment violation: {label} '{resolved.name}' must reside inside "
            "plugin state_dir, SillyTavern data root, or the current workspace."
        )

    # ------------------------------------------------------------------
    # 14 Core MCP Tool Implementations
    # ------------------------------------------------------------------
    def tool_st_session_bootstrap(self, args: dict[str, Any]) -> dict[str, Any]:
        char_query = str(args["character_name"]).strip()
        mode = str(args.get("mode", "play")).lstrip("-")
        force_new = bool(args.get("new_chat", False))
        greeting_idx = int(args.get("greeting_index", 0))

        prev = self.load_active_session()
        card_path, card_bundle, char_name, avatar_stem = self._resolve_card_and_identity(char_query, prev)
        card_v3 = card_bundle["card"]

        persona = get_active_persona(self.st_data_root, args.get("persona_name"))
        preset = load_openai_preset(self.st_data_root, args.get("preset_name"))

        existing_chats = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=char_name)
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
                self.st_data_root,
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
        self.save_active_session(session_state)
        if (self.st_data_root / "characters").exists() and "mock_st_data" not in str(self.st_data_root):
            self.save_plugin_settings({"last_st_data_root": str(self.st_data_root)})

        expressions = list_character_expressions(self.st_data_root, avatar_stem) or list_character_expressions(
            self.st_data_root, char_name
        )
        return {
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

    def tool_st_dump_prompt_frame(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self.load_active_session()
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

        card_path, card_bundle, char_name, avatar_stem = self._resolve_card_and_identity(char_query, session)
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
            chats = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=char_name)
            if chats:
                header, messages = read_jsonl_chat(chats[0])
            else:
                header, messages = {"chat_metadata": {"variables": {}, "timed_effects": {}}}, []

        entries = self.collect_merged_lore_entries(card_v3, session.get("extra_lorebooks"))
        lore_res = self.lore_engine.evaluate_and_splice(
            entries=entries,
            chat_messages=messages,
            latest_user_input=user_input,
            timed_state=header.get("chat_metadata", {}).get("timed_effects"),
            token_budget=budget,
            quarantine_backstage=quarantine_backstage,
        )
        persona = get_active_persona(self.st_data_root, session.get("persona_name"))
        preset = load_openai_preset(self.st_data_root, session.get("preset_name"))
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
        )
        return {
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

    def tool_st_commit_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self.load_active_session()
        char_query = str(args.get("character_name") or session.get("card_file") or session.get("character_name") or "").strip()
        if not char_query:
            raise ValueError("character_name is required to commit a turn.")

        card_path, card_bundle, char_name, avatar_stem = self._resolve_card_and_identity(char_query, session)
        card_v3 = card_bundle["card"]
        persona = get_active_persona(self.st_data_root, session.get("persona_name"))

        chat_path_str = session.get("chat_path")
        if (
            chat_path_str
            and Path(chat_path_str).exists()
            and session.get("character_name") == char_name
            and session.get("avatar_stem", avatar_stem) == avatar_stem
        ):
            chat_path = Path(chat_path_str)
        else:
            existing = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=char_name)
            if existing:
                chat_path = existing[0]
            else:
                created = create_new_chat_session(
                    self.st_data_root,
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

        ok, reason = evaluate_policy_compliance(raw_char, state_dir=self.state_dir)
        if not ok:
            raise ValueError(f"Turn commit blocked by policy gate: {reason}")

        header, messages = read_jsonl_chat(chat_path)
        vars_dict = header.get("chat_metadata", {}).get("variables", {})
        if isinstance(args.get("variables_delta"), dict):
            vars_dict.update(args["variables_delta"])

        extra_meta = dict(args.get("extra_meta") or {})
        is_backstage = bool(extra_meta.get("is_backstage", False) or session.get("director_mode", False))
        if is_backstage:
            extra_meta["is_backstage"] = True
        epistemic_level = normalize_epistemic_level(session.get("epistemic_level", 0))

        cleaned_char = run_regex_pipeline(
            raw_char,
            st_data_root=self.st_data_root,
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

        entries = self.collect_merged_lore_entries(card_v3, session.get("extra_lorebooks"))
        lore_eval = self.lore_engine.evaluate_and_splice(
            entries=entries,
            chat_messages=messages,
            latest_user_input=cleaned_user or "",
            timed_state=header.get("chat_metadata", {}).get("timed_effects"),
            quarantine_backstage=(epistemic_level < 100 and not is_backstage),
        )

        expr_list = list_character_expressions(self.st_data_root, avatar_stem) or list_character_expressions(
            self.st_data_root, char_name
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
            variables_delta=args.get("variables_delta"),
            next_timed_state=lore_eval["next_timed_state"],
            extra_meta=extra_meta,
        )
        commit_res["expression"] = detected_expr

        session["active"] = True
        session["character_name"] = char_name
        session["card_file"] = card_path.name
        session["avatar_stem"] = avatar_stem
        session["card_path"] = str(card_path)
        session["chat_path"] = str(chat_path)
        session["last_committed_at"] = time.time()
        session["last_turn_hash"] = commit_res["turn_hash"]
        session["pending_uncommitted_turn"] = False
        session["user_floor_already_appended"] = False
        session["stage_busy"] = False
        self.save_active_session(session)
        return commit_res

    def tool_st_publish_stage_suggestions(self, args: dict[str, Any]) -> dict[str, Any]:
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
        atomic_write_json(self.stage_suggestions_file, payload, indent=2)
        session = self.load_active_session()
        if session:
            session["pending_uncommitted_turn"] = False
            session["stage_busy"] = False
            self.save_active_session(session)
        return {"published": len(suggestions), "kind": kind, "suggestions": suggestions}

    def tool_st_read_card(self, args: dict[str, Any]) -> dict[str, Any]:
        card_path = self.find_character_card_path(str(args["card_filename"]))
        bundle = read_character_card(
            card_path,
            include_avatar_data_uri=bool(args.get("include_avatar_base64", False)),
        )
        bundle.pop("raw_card", None)
        return bundle

    def tool_st_build_export_card(self, args: dict[str, Any]) -> dict[str, Any]:
        card_json = args["card_v3_json"]
        if isinstance(card_json, str):
            card_json = json.loads(card_json)
        out_name = sanitize_filename_stem(Path(str(args["output_filename"]).strip()).stem) + ".png"
        char_dir = (self.st_data_root / "characters").resolve()
        char_dir.mkdir(parents=True, exist_ok=True)
        out_path = ensure_under_dir(char_dir / out_name, char_dir)
        raw_portrait = args.get("portrait_image_path")
        portrait_path = (
            self._validate_contained_path(raw_portrait, label="portrait_image_path")
            if raw_portrait
            else None
        )
        return write_character_card_png(
            card_payload=card_json,
            output_png_path=out_path,
            base_png_path=portrait_path,
        )

    def tool_st_lint_card_and_lore(self, args: dict[str, Any]) -> dict[str, Any]:
        """Card Doctor & World Info Doctor static audit tool."""
        card_issues: list[dict[str, Any]] = []
        lore_issues: list[dict[str, Any]] = []
        inspected_card = None
        inspected_lore = None

        char_q = str(args.get("character_name") or "").strip()
        lore_q = str(args.get("lorebook_name") or "").strip()
        if not char_q and not lore_q:
            sess = self.load_active_session()
            char_q = str(sess.get("character_name") or "")

        if char_q:
            c_path = self.find_character_card_path(char_q)
            bundle = read_character_card(c_path, include_avatar_data_uri=False)
            inspected_card = c_path.name
            card_issues = lint_character_card(bundle)
            linked = str(bundle["card"]["data"].get("extensions", {}).get("world", "")).strip()
            if linked and not lore_q:
                lore_q = linked

        if lore_q:
            fname = lore_q if lore_q.endswith(".json") else f"{lore_q}.json"
            l_path = ensure_under_dir(self.st_data_root / "worlds" / fname, self.st_data_root / "worlds")
            inspected_lore = l_path.name
            lore_issues = lint_lorebook_file(l_path)

        total = len(card_issues) + len(lore_issues)
        return {
            "healthy": total == 0,
            "total_issues": total,
            "inspected_card": inspected_card,
            "inspected_lorebook": inspected_lore,
            "card_issues": card_issues,
            "lorebook_issues": lore_issues,
        }

    def tool_st_query_lorebook(self, args: dict[str, Any]) -> dict[str, Any]:
        book_names = args.get("lorebook_names") or []
        if isinstance(book_names, str):
            book_names = [book_names]
        query_text = str(args.get("query_text", ""))
        budget = int(args.get("token_budget", 2048))

        entries: list[NormalizedLoreEntry] = []
        worlds_dir = (self.st_data_root / "worlds").resolve()
        worlds_dir.mkdir(parents=True, exist_ok=True)
        target_files = (
            [ensure_under_dir(worlds_dir / (b if b.endswith(".json") else f"{b}.json"), worlds_dir) for b in book_names]
            if book_names
            else sorted(worlds_dir.glob("*.json"))
        )
        for p in target_files:
            if p.exists():
                entries.extend(load_lorebook_file(p))

        res = self.lore_engine.evaluate_and_splice(
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

    def tool_st_evolve_lore_entry(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_name = sanitize_filename_stem(Path(str(args["lorebook_name"]).strip()).stem)
        worlds_dir = (self.st_data_root / "worlds").resolve()
        worlds_dir.mkdir(parents=True, exist_ok=True)
        lore_path = ensure_under_dir(worlds_dir / f"{raw_name}.json", worlds_dir)
        op = str(args.get("operation", "upsert"))
        payload = args["entry_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return evolve_lorebook_file(lore_path, op, payload)  # type: ignore[arg-type]

    def tool_st_manage_chat_branch(self, args: dict[str, Any]) -> dict[str, Any]:
        sess = self.load_active_session()
        char_query = str(args["character_name"]).strip()
        card_path, card_bundle, char_name, avatar_stem = self._resolve_card_and_identity(char_query, sess)
        card_v3 = card_bundle["card"]
        action = str(args["action"])
        target_id = int(args.get("target_id", 0))

        if action == "new":
            persona = get_active_persona(self.st_data_root)
            created = create_new_chat_session(
                self.st_data_root,
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
            if sess.get("character_name") == char_name and sess.get("avatar_stem", avatar_stem) == avatar_stem:
                sess["chat_path"] = created["chat_path"]
                self.save_active_session(sess)
            return created

        if (
            sess.get("character_name") == char_name
            and sess.get("avatar_stem", avatar_stem) == avatar_stem
            and sess.get("chat_path")
            and Path(sess["chat_path"]).exists()
        ):
            chat_target = Path(sess["chat_path"])
        else:
            chats = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=char_name)
            if not chats:
                raise FileNotFoundError(f"No existing chats found for {char_name}.")
            chat_target = chats[0]
        return manage_chat_branch(self.st_data_root, char_name, chat_target, action, target_id)  # type: ignore[arg-type]

    def tool_st_stage_floor_op(self, args: dict[str, Any]) -> dict[str, Any]:
        """Executes in-place Stage floor operations (edit, delete, switch_swipe, prune_swipes, fork_from_floor)."""
        sess = self.load_active_session()
        char_q = str(args.get("character_name") or sess.get("card_file") or sess.get("character_name") or "").strip()
        if not char_q:
            raise ValueError("character_name is required for floor operations.")
        card_path, _, char_name, avatar_stem = self._resolve_card_and_identity(char_q, sess)

        chat_file = str(args.get("chat_file") or "").strip()
        if chat_file:
            if ".." in Path(chat_file).parts or Path(chat_file).name != chat_file:
                raise ValueError("Invalid chat_file name.")
            primary_dir = get_character_chat_dir(self.st_data_root, avatar_stem)
            cand_primary = ensure_under_dir(primary_dir / chat_file, primary_dir)
            if cand_primary.exists():
                chat_path = cand_primary
            else:
                legacy_dir = get_character_chat_dir(self.st_data_root, char_name)
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
            chats = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=char_name)
            if not chats:
                raise FileNotFoundError(f"No chat log found for {char_name}.")
            chat_path = chats[0]

        persona = get_active_persona(self.st_data_root, sess.get("persona_name"))
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
            sess["chat_path"] = res["branch_path"]
            sess["character_name"] = char_name
            sess["card_file"] = card_path.name
            sess["avatar_stem"] = avatar_stem
            self.save_active_session(sess)
        return res

    def tool_st_switch_profile(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self.load_active_session()
        if args.get("preset_name"):
            preset = load_openai_preset(self.st_data_root, str(args["preset_name"]))
            session["preset_name"] = preset["preset_name"]
            session["bootstrapped_conv_id"] = ""
        if args.get("persona_name"):
            persona = get_active_persona(self.st_data_root, str(args["persona_name"]))
            session["persona_name"] = persona["name"]
            session["bootstrapped_conv_id"] = ""
        if args.get("mode"):
            session["mode"] = str(args["mode"]).lstrip("-")
        if "epistemic_level" in args and args["epistemic_level"] is not None:
            session["epistemic_level"] = normalize_epistemic_level(args["epistemic_level"])
            session["bootstrapped_conv_id"] = ""
        if "agentic_hands" in args and args["agentic_hands"] is not None:
            session["agentic_hands"] = bool(args["agentic_hands"])
        if "director_mode" in args and args["director_mode"] is not None:
            session["director_mode"] = bool(args["director_mode"])
            session["bootstrapped_conv_id"] = ""
        if args.get("policy_mode"):
            self.save_plugin_settings({"policy_mode": str(args["policy_mode"])})

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

        self.save_active_session(session)
        return {"updated_session": session, "plugin_settings": self.load_plugin_settings()}

    def tool_st_manage_variables(self, args: dict[str, Any]) -> dict[str, Any]:
        scope = str(args.get("scope", "chat"))
        op = str(args.get("op", "list"))
        key = str(args.get("key", "")).strip()
        val = args.get("value")

        if scope == "global":
            settings_path = self.st_data_root / "settings.json"
            settings = load_st_settings(self.st_data_root)
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
                g_vars[key] = float(g_vars.get(key, 0)) + float(val or 1)
            elif op == "del":
                g_vars.pop(key, None)
            if op in ("set", "inc", "del"):
                atomic_write_json(settings_path, settings, indent=2)
            return {"scope": "global", "variables": g_vars}

        session = self.load_active_session()
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
                num = float(c_vars.get(key, 0)) + float(val or 1)
                c_vars[key] = int(num) if num.is_integer() else num
            elif op == "del":
                c_vars.pop(key, None)
            if op in ("set", "inc", "del"):
                write_jsonl_chat_atomic(c_path_obj, header, messages)
            return {"scope": "chat", "variables": dict(c_vars)}

    def tool_st_compile_stage_widget(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self.load_active_session()
        char_query = str(args.get("character_name") or session.get("card_file") or session.get("character_name") or "").strip()
        if not char_query:
            raise ValueError("character_name is required to compile the Stage widget.")
        placement = str(args.get("placement", "standalone"))
        raw_out_html = args.get("output_html_path")
        safe_out_html = (
            self._validate_contained_path(
                raw_out_html,
                default_subdir="widgets",
                allowed_suffixes=(".html", ".htm"),
                label="output_html_path",
            )
            if raw_out_html is not None and str(raw_out_html).strip() != ""
            else None
        )

        card_path, bundle, char_name, avatar_stem = self._resolve_card_and_identity(
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
            chats = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=char_name)
            if chats:
                chat_file_name = chats[0].name
                header, messages = read_jsonl_chat(chats[0])
            else:
                chat_file_name = "unsaved_preview.jsonl"
                header, messages = {"chat_metadata": {"variables": {}}}, []

        dump_info = self.tool_st_dump_prompt_frame({"character_name": card_path.name})
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

    # ------------------------------------------------------------------
    # Sidecar High-Speed Batch Operations (`--sidecar-op`)
    # ------------------------------------------------------------------
    def handle_sidecar_op(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "snapshot":
            return self._sidecar_snapshot()
        if op == "bootstrap":
            return self.tool_st_session_bootstrap(args)
        if op == "bind_rp_conv":
            sess = self.load_active_session()
            sess["rp_conv_id"] = str(args.get("rp_conv_id") or "").strip()
            if sess["rp_conv_id"]:
                sess["active"] = True
            self.save_active_session(sess)
            return {"rp_conv_id": sess["rp_conv_id"]}
        if op == "floor_op":
            return self.tool_st_stage_floor_op(args)
        if op == "prepare_agent_turn":
            return self._sidecar_prepare_agent_turn(args)
        if op == "read_lorebook":
            return self._sidecar_read_lorebook(args)
        if op == "evolve_lore":
            return self.tool_st_evolve_lore_entry(args)
        if op == "query_lore":
            return self.tool_st_query_lorebook(args)
        if op == "lint":
            return self.tool_st_lint_card_and_lore(args)
        if op == "switch_profile":
            return self.tool_st_switch_profile(args)
        if op == "manage_variables":
            return self.tool_st_manage_variables(args)
        if op == "import_card":
            return self._sidecar_import_card(args)
        if op == "clear_suggestions":
            self.stage_suggestions_file.unlink(missing_ok=True)
            return {"cleared": True}
        raise ValueError(f"Unknown sidecar operation: {op}")

    def _sidecar_snapshot(self) -> dict[str, Any]:
        char_dir = self.st_data_root / "characters"
        worlds_dir = self.st_data_root / "worlds"
        cards_summary: list[dict[str, Any]] = []
        if char_dir.exists():
            for p in sorted(char_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in (".png", ".json"):
                    try:
                        b = read_character_card(p, include_avatar_data_uri=False)
                        d = b["card"]["data"]
                        c_name = d["name"]
                        cards_summary.append({
                            "file": p.name,
                            "name": c_name,
                            "scenario": str(d.get("scenario", ""))[:120],
                            "tags": list(d.get("tags") or [])[:6],
                            "world": str(d.get("extensions", {}).get("world", "")),
                            "spec": b["detected_chunk"],
                            "has_dual_chunks": b["has_dual_chunks"],
                            "expressions_count": len(list_character_expressions(self.st_data_root, c_name)),
                        })
                    except Exception:
                        continue

        session = self.load_active_session()
        if not session.get("character_name") and cards_summary:
            # Auto-select first available card for Sidecar display without claiming an active global RP chat
            try:
                self.tool_st_session_bootstrap({
                    "character_name": cards_summary[0]["file"],
                    "mode": "play",
                    "activate_rp": False,
                })
                session = self.load_active_session()
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

        if active_char:
            try:
                c_query = str(session.get("card_file") or active_char)
                c_path, c_bundle, resolved_char, avatar_stem = self._resolve_card_and_identity(c_query, session)
                active_char = resolved_char
                active_card_file = c_path.name
                active_card_data = c_bundle["card"]["data"]
                expressions = list_character_expressions(self.st_data_root, avatar_stem) or list_character_expressions(
                    self.st_data_root, active_char
                )
                all_chats = list_chat_files(self.st_data_root, avatar_stem, legacy_character_name=active_char)
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

                dump_data = self.tool_st_dump_prompt_frame({"character_name": active_card_file})
                doctor_data = self.tool_st_lint_card_and_lore({"character_name": active_card_file})
            except Exception as err:
                dump_data = {"dump_report": f"Inspector error: {err}", "active_lore": [], "prefix_cache_ratio_pct": 0}

        lorebooks = sorted(p.stem for p in worlds_dir.glob("*.json")) if worlds_dir.exists() else []
        suggestions_obj: dict[str, Any] = {}
        if self.stage_suggestions_file.exists():
            try:
                suggestions_obj = json.loads(self.stage_suggestions_file.read_text(encoding="utf-8"))
            except Exception:
                suggestions_obj = {}

        meta = header.get("chat_metadata", {})
        return {
            "data_root_name": self.st_data_root.name,
            "plugin_settings": self.load_plugin_settings(),
            "presets": list_openai_presets(self.st_data_root),
            "lorebooks": lorebooks,
            "groups": list_groups(self.st_data_root),
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
                "persona_name": str(session.get("persona_name") or get_active_persona(self.st_data_root)["name"]),
                "preset_name": str(session.get("preset_name") or "Default"),
                "stage_busy": bool(session.get("stage_busy", False)),
                "pending_uncommitted_turn": bool(session.get("pending_uncommitted_turn", False)),
                "pending_turn_mode": str(session.get("pending_turn_mode", "normal")),
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

    def _sidecar_prepare_agent_turn(self, args: dict[str, Any]) -> dict[str, Any]:
        """Prepares session state & returns the formatted prompt to send to Antigravity built-in model."""
        session = self.load_active_session()
        char_name = str(args.get("character_name") or session.get("character_name") or "").strip()
        if not char_name:
            raise ValueError("Select a character before dispatching to the Antigravity model.")
        if session.get("character_name") != char_name or not session.get("chat_path"):
            self.tool_st_session_bootstrap({"character_name": char_name, "mode": args.get("mode", "play")})
            session = self.load_active_session()

        action_kind = str(args.get("action_kind", "send_turn"))
        user_text = str(args.get("user_text", "")).strip()
        mode = str(session.get("mode", "play"))
        rp_conv_id = str(args.get("rp_conv_id") or session.get("rp_conv_id") or "").strip()

        ok, detail = evaluate_policy_compliance(user_text, state_dir=self.state_dir)
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
            self.tool_st_stage_floor_op({
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
            self.tool_st_stage_floor_op({
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

        session["user_floor_already_appended"] = action_kind in ("send_turn", "director_intervention") or is_director_turn
        session["pending_sidecar_launch"] = {
            "prompt": prompt_to_agent,
            "action_kind": "director_intervention" if is_director_turn else action_kind,
            "user_text": user_text,
            "user_floor_already_appended": action_kind in ("send_turn", "director_intervention") or is_director_turn,
            "ts": time.time(),
        }
        self.save_active_session(session)

        return {
            "ready": True,
            "character_name": char_name,
            "action_kind": action_kind,
            "prompt": prompt_to_agent,
            "gentle_notice": detail.removeprefix("gentle_notice:") if detail.startswith("gentle_notice:") else "",
        }

    def _sidecar_read_lorebook(self, args: dict[str, Any]) -> dict[str, Any]:
        worlds_dir = (self.st_data_root / "worlds").resolve()
        worlds_dir.mkdir(parents=True, exist_ok=True)
        b_name = str(args.get("lorebook_name") or "").strip()
        if not b_name:
            files = sorted(worlds_dir.glob("*.json"))
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

    def _sidecar_import_card(self, args: dict[str, Any]) -> dict[str, Any]:
        filename = str(args.get("filename") or "imported_card.png").strip()
        b64_data = str(args.get("base64_data") or "").strip()
        if not b64_data:
            raise ValueError("Missing base64_data for card import.")
        if "," in b64_data and b64_data.startswith("data:"):
            b64_data = b64_data.split(",", 1)[1]
        raw_bytes = base64.b64decode(b64_data)

        char_dir = (self.st_data_root / "characters").resolve()
        char_dir.mkdir(parents=True, exist_ok=True)
        stem = sanitize_filename_stem(Path(filename).stem)
        out_png = ensure_under_dir(char_dir / f"{stem}.png", char_dir)

        if filename.lower().endswith(".json") or raw_bytes.lstrip().startswith(b"{"):
            card_obj = json.loads(raw_bytes.decode("utf-8"))
            res = write_character_card_png(card_obj, out_png)
        else:
            # Parse & re-export through Seppä codec so dual chunks & Card-is-Data sanitization apply!
            tmp_in = self.state_dir / f".import_{stem}.png"
            try:
                tmp_in.write_bytes(raw_bytes)
                parsed = read_character_card(tmp_in, include_avatar_data_uri=False)
                res = write_character_card_png(parsed["card"], out_png, base_png_bytes=raw_bytes)
            finally:
                tmp_in.unlink(missing_ok=True)

        self.tool_st_session_bootstrap({"character_name": out_png.name, "mode": "play"})
        return res


MCP_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
        "name": "st_lint_card_and_lore",
        "description": "Card Doctor & World Info Doctor static linter: detects missing ccv3 chunks, system_prompt wiping presets without {{original}}, selective:false secondary-key traps, leaked creator_notes, and broken macros.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "character_name": {"type": "string"},
                "lorebook_name": {"type": "string"},
            },
        },
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
]


class StdioMCPServer:
    """Pure-Python MCP JSON-RPC 2.0 Server supporting newline & Content-Length framing."""

    def __init__(self, runtime: TavernFusionRuntime) -> None:
        self.runtime = runtime
        self._dispatch_map = {
            "st_session_bootstrap": runtime.tool_st_session_bootstrap,
            "st_commit_turn": runtime.tool_st_commit_turn,
            "st_publish_stage_suggestions": runtime.tool_st_publish_stage_suggestions,
            "st_compile_stage_widget": runtime.tool_st_compile_stage_widget,
            "st_dump_prompt_frame": runtime.tool_st_dump_prompt_frame,
            "st_read_card": runtime.tool_st_read_card,
            "st_build_export_card": runtime.tool_st_build_export_card,
            "st_lint_card_and_lore": runtime.tool_st_lint_card_and_lore,
            "st_query_lorebook": runtime.tool_st_query_lorebook,
            "st_evolve_lore_entry": runtime.tool_st_evolve_lore_entry,
            "st_manage_chat_branch": runtime.tool_st_manage_chat_branch,
            "st_stage_floor_op": runtime.tool_st_stage_floor_op,
            "st_switch_profile": runtime.tool_st_switch_profile,
            "st_manage_variables": runtime.tool_st_manage_variables,
        }

    def handle_rpc(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if req_id is None:
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion", "2024-11-05")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "st-antigravity-mcp", "version": "3.0.1"},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            # Always return the full tool catalog so parallel engineering conversations
            # never lose tool visibility; Switch B tool blocking is enforced per-conversation
            # via `rp_conv_id == conversationId` in `hooks/pre_tool_guard.py`.
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOL_SCHEMAS}}

        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

        if method == "tools/call":
            if self.runtime._explicit_data_root is None:
                self.runtime.st_data_root = self.runtime._resolve_data_root(None, self.runtime.state_dir)
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            fn = self._dispatch_map.get(tool_name)
            if fn is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            try:
                out = fn(tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error executing {tool_name}: {exc}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def serve_forever(self) -> None:
        stdin_bin = sys.stdin.buffer
        stdout_bin = sys.stdout.buffer

        while True:
            first_line = stdin_bin.readline()
            if not first_line:
                break
            stripped = first_line.strip()
            if not stripped:
                continue

            use_content_length = False
            if stripped.lower().startswith(b"content-length:"):
                use_content_length = True
                length = int(stripped.split(b":", 1)[1].strip())
                while True:
                    hdr = stdin_bin.readline()
                    if not hdr or hdr in (b"\r\n", b"\n"):
                        break
                raw_body = stdin_bin.read(length)
            else:
                raw_body = stripped

            try:
                req_obj = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            resp = self.handle_rpc(req_obj)
            if resp is not None:
                encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                if use_content_length:
                    stdout_bin.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded)
                else:
                    stdout_bin.write(encoded + b"\n")
                stdout_bin.flush()


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
