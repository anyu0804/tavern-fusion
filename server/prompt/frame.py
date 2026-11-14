"""Prefix-cache-aligned prompt frame compiler (full prefix & agent-mode incremental frame) and lexical dialogue examples.

Moved verbatim from `server/st_runtime_compat.py`, which re-exports these names.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from ..lore_engine import estimate_tokens
from .epistemic import SYSTEM_INSTRUCTION_BY_EPISTEMIC, normalize_epistemic_level
from .macros import resolve_st_macros
from .presets import DEFAULT_RP_MAIN_PROMPT

HISTORY_WINDOW = 12
"""Minimum number of recent chat items rendered into the frame."""

HISTORY_CHUNK = 12
"""Prefix-cache mode: the history window start only moves in steps of this many items, so the
rendered history stays append-only (byte-identical prefix) for up to HISTORY_CHUNK turns."""

CACHE_MODES = ("off", "prefix_pinned")


def history_window_start(total: int, cache_mode: str = "off") -> int:
    """Index of the first chat item rendered into the frame.

    `off` keeps SillyTavern-like behaviour (always the last HISTORY_WINDOW items, so the start slides
    every turn). `prefix_pinned` drops old items in whole chunks instead, rendering between
    HISTORY_WINDOW and HISTORY_WINDOW + HISTORY_CHUNK - 1 items.
    """
    if total <= HISTORY_WINDOW:
        return 0
    if cache_mode != "prefix_pinned":
        return total - HISTORY_WINDOW
    return ((total - HISTORY_WINDOW) // HISTORY_CHUNK) * HISTORY_CHUNK


def format_lexical_dialogue_examples(
    raw_examples: str,
    char_name: str,
    user_name: str,
    variables: dict[str, Any] | None = None,
    outlets: dict[str, str] | None = None,
    defer_volatile: bool = False,
) -> tuple[str, int]:
    """Lock #2: Preserves `<START>` boundaries in `mes_example` as structured Few-Shot Lexical Blocks
    to dye the model's next-token probability distribution in `{{char}}`'s exact cadence and vocabulary.
    """
    if not raw_examples or not raw_examples.strip():
        return "", 0
    raw_chunks = re.split(r"(?i)<START>", raw_examples)
    formatted_blocks: list[str] = []
    for chunk in raw_chunks:
        cleaned = resolve_st_macros(chunk, char_name, user_name, variables, outlets, defer_volatile=defer_volatile).strip()
        if cleaned:
            idx = len(formatted_blocks) + 1
            formatted_blocks.append(
                f"<START_LEXICAL_SAMPLE index=\"{idx}\" purpose=\"vocabulary_and_cadence_dye_only\">\n"
                f"{cleaned}\n"
                f"</START_LEXICAL_SAMPLE>"
            )
    if not formatted_blocks:
        return "", 0
    header = (
        f"[Dialogue Examples — Lock #2 Lexical Convergence ({len(formatted_blocks)} <START> blocks: "
        f"Lock token probability distribution to {char_name}'s diction, punctuation, and sensory style; "
        f"do NOT treat these samples as events that already happened in the current chat)]"
    )
    return header + "\n" + "\n".join(formatted_blocks), len(formatted_blocks)


def compile_st_prompt_frame(
    card_v3: dict[str, Any],
    persona: dict[str, str],
    preset: dict[str, Any],
    lore_result: dict[str, Any],
    chat_header: dict[str, Any],
    mode: str = "play",
    compact_agent_delta: bool = False,
    boundary_notice: str = "",
    epistemic_level: int = 0,
    agentic_hands: bool = False,
    director_mode: bool = False,
    cache_mode: str = "off",
) -> dict[str, Any]:
    """Full Dynamic Prompt Re-assembler implementing the Four Dimensional Locks & Dimension Mixer:

    1. Lock #1 (Top-Level `system_instruction` Hot-Swap): Welds 0% Deep Immersion, 50% Tacit Resonance,
       or 100% Dual-Track Avatar cognitive directives directly at the top of the prompt stack.
    2. Lock #2 (`<START>` Lexical Convergence): Preserves structured `<START_LEXICAL_SAMPLE>` dialogue
       blocks on every turn so token probabilities stay dyed in `{{char}}`'s vocabulary.
    3. Lock #3 (Golden `@Depth 1~4` Splicing): Inserts dynamic World Info & Author's Note directly at
       their exact `@Depth` positions right above the latest turns in `chatHistory` instead of shoving
       them into a tail patch.
    4. Backstage Quarantine (`extra.is_backstage: true`): Automatically strips cross-dimensional
       director/coding discussions from `chatHistory` when `epistemic_level < 100` and `not director_mode`.

    `cache_mode="prefix_pinned"` (opt-in, used by the BYOK backend) trades exact SillyTavern placement
    for provider prefix-cache hits: only constant (blue) lore stays in the prefix; keyword-triggered
    lore, @Depth lore and the Author's Note move to the dynamic tail; `{{time}}`/`{{date}}`/`{{getvar}}`
    are deferred to the tail; and history is truncated in whole chunks so it is append-only.
    """
    if cache_mode not in CACHE_MODES:
        cache_mode = "off"
    pinned = cache_mode == "prefix_pinned"

    def _r(text: str, **kw: Any) -> str:
        return resolve_st_macros(text, char_name, user_name, variables, outlets, defer_volatile=pinned, **kw)

    data = card_v3.get("data", {})
    char_name = str(data.get("name", "Character"))
    user_name = str(persona.get("name") or chat_header.get("user_name") or "旅行者")
    meta = chat_header.get("chat_metadata", {})
    variables = meta.get("variables", {})
    outlets = lore_result.get("outlets", {})
    slots = lore_result.get("slots", {})
    norm_epistemic = normalize_epistemic_level(epistemic_level)
    quarantine_active = (norm_epistemic < 100) and (not director_mode)
    quarantined_count = int(lore_result.get("quarantined_backstage_count", 0))

    ordered_ids: list[str] = preset.get("ordered_identifiers") or [
        "main",
        "worldInfoBefore",
        "personaDescription",
        "charDescription",
        "charPersonality",
        "scenario",
        "nsfw",
        "worldInfoAfter",
        "dialogueExamples",
        "chatHistory",
        "jailbreak",
    ]
    custom_prompts: dict[str, dict[str, Any]] = preset.get("custom_prompts") or {}

    # Top-level System Instruction hot-swapped by Dimension Mixer Switch A
    top_sys_raw = SYSTEM_INSTRUCTION_BY_EPISTEMIC.get(norm_epistemic, SYSTEM_INSTRUCTION_BY_EPISTEMIC[0])
    top_system_instruction = _r(top_sys_raw)

    base_main = _r(preset.get("main_prompt", DEFAULT_RP_MAIN_PROMPT))
    card_sys = str(data.get("system_prompt", "")).strip()
    slot1_sys = _r(card_sys, original_prompt=base_main) if card_sys else base_main

    # All `before_char` and `after_char` lore (both constant and keyword/semantic triggered) placed in proper slots
    # In prefix-cache mode only constant (blue) entries stay in the prefix; triggered (green)
    # entries are re-stated in the dynamic tail so a new trigger never invalidates the cache.
    tail_lore_items: list[str] = []
    wi_before_items: list[str] = []
    wi_after_items: list[str] = []
    for item in lore_result.get("active_trace", []):
        pos = item.get("position")
        if pos not in ("before_char", "after_char"):
            continue
        if pinned and not item.get("constant"):
            tail_lore_items.append(str(item["content"]))
        elif pos == "before_char":
            wi_before_items.append(item["content"])
        else:
            wi_after_items.append(item["content"])
    at_depth_trace = [
        item
        for item in lore_result.get("active_trace", [])
        if str(item.get("position", "")).startswith("@D")
    ]

    prefix_sections: list[str] = [top_system_instruction]
    slot3_parts: list[str] = []
    slot4_parts: list[str] = []
    lexical_block_count = 0

    for ident in ordered_ids:
        if ident == "chatHistory":
            break
        if ident == "main":
            if slot1_sys:
                prefix_sections.append(slot1_sys)
        elif ident == "worldInfoBefore":
            if wi_before_items:
                blk = _r("\n".join(wi_before_items))
                prefix_sections.append(f"[World Info (Before)]\n{blk}")
        elif ident == "personaDescription" and persona.get("description"):
            p_txt = _r(f"[User Persona ({user_name})]\n{persona['description']}")
            slot3_parts.append(p_txt)
            prefix_sections.append(p_txt)
        elif ident == "charDescription" and data.get("description"):
            c_txt = _r(f"[Character Description]\n{data['description']}")
            slot3_parts.append(c_txt)
            prefix_sections.append(c_txt)
        elif ident == "charPersonality" and data.get("personality"):
            pers_txt = _r(f"[Personality]\n{data['personality']}")
            slot3_parts.append(pers_txt)
            prefix_sections.append(pers_txt)
        elif ident == "scenario" and data.get("scenario"):
            scen_txt = _r(f"[Scenario]\n{data['scenario']}")
            slot3_parts.append(scen_txt)
            prefix_sections.append(scen_txt)
        elif ident == "nsfw" and preset.get("auxiliary_prompt"):
            aux_txt = _r(str(preset["auxiliary_prompt"]))
            if aux_txt:
                prefix_sections.append(aux_txt)
        elif ident == "worldInfoAfter" and wi_after_items:
            wa_txt = _r("\n".join(wi_after_items))
            slot4_parts.append(wa_txt)
            prefix_sections.append(f"[World Info (After)]\n{wa_txt}")
        elif ident == "dialogueExamples" and str(data.get("mes_example", "")).strip():
            ex_txt, lexical_block_count = format_lexical_dialogue_examples(
                str(data["mes_example"]), char_name, user_name, variables, outlets, defer_volatile=pinned
            )
            if ex_txt:
                slot4_parts.append(ex_txt)
                prefix_sections.append(ex_txt)
        elif ident in custom_prompts:
            cp_txt = _r(custom_prompts[ident]["content"])
            if cp_txt:
                prefix_sections.append(f"[{custom_prompts[ident]['name']}]\n{cp_txt}")

    static_prefix_block = "\n\n".join(s for s in prefix_sections if s.strip())

    # Lock #3: Golden @Depth 1~4 Splicing directly inside `spliced_chat` (with Backstage Quarantine)
    raw_spliced = list(lore_result.get("spliced_chat", []))
    spliced_msgs: list[dict[str, Any]] = []
    for m in raw_spliced:
        if quarantine_active and isinstance(m.get("extra"), dict) and m["extra"].get("is_backstage"):
            continue
        if pinned and m.get("name") == "WorldInfo" and "lore_uid" in m:
            # @Depth lore moves to the tail instead of shifting through the history every turn.
            tail_lore_items.append(str(m.get("mes", "")))
            continue
        spliced_msgs.append(dict(m))

    note_prompt = str(meta.get("note_prompt", "")).strip()
    an_depth = int(meta.get("note_depth", 4))
    tail_author_note = ""
    if note_prompt or slots.get("an_top") or slots.get("an_bottom"):
        an_block = "\n".join(slots.get("an_top", []) + ([note_prompt] if note_prompt else []) + slots.get("an_bottom", []))
        an_resolved = _r(an_block)
        if pinned and an_resolved.strip():
            tail_author_note = an_resolved
        elif an_resolved.strip():
            idx = max(0, len(spliced_msgs) - an_depth)
            spliced_msgs.insert(
                idx,
                {
                    "role": "system",
                    "name": f"AuthorsNote@D{an_depth}",
                    "mes": an_resolved,
                },
            )

    hist_slice = spliced_msgs[history_window_start(len(spliced_msgs), cache_mode):]
    rendered_history_lines: list[str] = []
    history_messages: list[dict[str, str]] = []
    for m in hist_slice:
        m_name = str(m.get("name", "Msg"))
        m_mes = _r(str(m.get("mes", "")))
        if m.get("is_user") or m.get("role") == "user":
            h_role = "user"
        elif m.get("is_system") or m.get("role") == "system":
            h_role = "system"
        else:
            h_role = "assistant"
        history_messages.append({"role": h_role, "name": m_name, "content": m_mes})
        if m_name == "WorldInfo":
            d_uid = m.get("lore_uid", "")
            rendered_history_lines.append(f"[World Info @Depth Injection #{d_uid}]: {m_mes}")
        elif m_name.startswith("AuthorsNote"):
            rendered_history_lines.append(f"[{m_name}]: {m_mes}")
        else:
            rendered_history_lines.append(f"{m_name}: {m_mes}")
    hist_text = "\n".join(rendered_history_lines)

    live_card_lines: list[str] = []
    mixer_summary = (
        f"[Dimension Mixer State | Epistemic={norm_epistemic}% | "
        f"AgenticHands={'ON' if agentic_hands else 'OFF (Tools Unplugged)'} | "
        f"DirectorMode={'ON (Backstage)' if director_mode else 'OFF'} | "
        f"QuarantinedBackstageFloors={quarantined_count}]"
    )
    live_card_lines.append(mixer_summary)

    if mode == "play":
        vars_summary = ", ".join(f"{k}={v}" for k, v in variables.items()) if variables else "affection=initial"
        live_card_lines.append(f"[Live Context Card | mode=--play] Active Variables: {vars_summary}")
    else:
        live_card_lines.append("[Pure Dialogue Mode | mode=--chat] Focus on natural character voice and emotional nuance.")

    if pinned:
        if tail_lore_items:
            lore_txt = _r("\n".join(tail_lore_items))
            if lore_txt:
                live_card_lines.append(f"[World Info (Triggered This Turn)]\n{lore_txt}")
        if tail_author_note:
            live_card_lines.append(f"[Author's Note]\n{tail_author_note}")
        now = datetime.now()
        live_vars = ", ".join(f"{k}={v}" for k, v in variables.items()) if variables else "(none)"
        live_card_lines.append(
            f"[Live Context] 当前日期 {now.strftime('%Y-%m-%d')} 时间 {now.strftime('%H:%M')} | Variables: {live_vars}"
        )

    post_hist = str(data.get("post_history_instructions") or preset.get("post_history_instructions") or "").strip()
    if post_hist:
        live_card_lines.append(resolve_st_macros(post_hist, char_name, user_name, variables, outlets))
    elif norm_epistemic == 0 and not director_mode:
        live_card_lines.append(
            f"[Post-History @D0 Lock]: Respond strictly within {char_name}'s world and era using {char_name}'s lexical style (*...* and \"...\"). "
            f"Never break the fourth wall, never use modern/coding terminology, and never speak for {user_name}."
        )
    if boundary_notice:
        live_card_lines.append(boundary_notice)

    dynamic_tail_block = "\n\n".join(live_card_lines)

    static_toks = estimate_tokens(static_prefix_block)
    dynamic_toks = estimate_tokens(hist_text) + estimate_tokens(dynamic_tail_block)
    total_toks = max(1, static_toks + dynamic_toks)
    cache_ratio = round((static_toks / total_toks) * 100, 1)

    frame_kind = "incremental" if compact_agent_delta else "bootstrap"
    if pinned:
        history_header = "## 2. SCENE HISTORY (append-only)"
    else:
        history_header = (
            f"## 2. GOLDEN @DEPTH SPLICED SCENE HISTORY ({len(hist_slice)} items | Quarantined Backstage: {quarantined_count})"
        )
    ephemeral_frame = (
        f"<ST_RUNTIME_FRAME character=\"{char_name}\" user=\"{user_name}\" mode=\"--{mode}\" "
        f"epistemic=\"{norm_epistemic}%\" agentic=\"{'on' if agentic_hands else 'off'}\" "
        f"director=\"{'on' if director_mode else 'off'}\" frame=\"{frame_kind}\">\n"
        f"## 1. TOP-LEVEL SYSTEM INSTRUCTION & CHARACTER IDENTITY ({static_toks} tok | Preset: {preset.get('preset_name')})\n"
        f"{static_prefix_block}\n\n"
        f"{history_header}\n"
        f"{hist_text}\n\n"
        f"## 3. IMMEDIATE @D0 POST-HISTORY DIRECTIVES\n"
        f"{dynamic_tail_block}\n"
        f"</ST_RUNTIME_FRAME>"
    )

    dump_lines = [
        f"=== TAVERN FUSION 3.0 --dump-prompt INSPECTOR ({char_name} | mode=--{mode}) ===",
        f"Dimension Mixer: Epistemic={norm_epistemic}% | AgenticHands={'ON' if agentic_hands else 'OFF'} | Director={'ON' if director_mode else 'OFF'}",
        f"Four Locks Status: Lock#1(System)={norm_epistemic}% | Lock#2(<START> Samples)={lexical_block_count} | Lock#3(@Depth Spliced)={len(at_depth_trace)} | Lock#4(Regex Guard)={'ON' if norm_epistemic < 100 and not director_mode else 'BYPASSED'}",
        f"Backstage Quarantine: {quarantined_count} floor(s) isolated from context" if quarantine_active else "Backstage Quarantine: INACTIVE (100% / Director Mode)",
        f"Preset Stack: {preset.get('preset_name')} ({len(ordered_ids)} ordered slots) | Temp={preset.get('temperature')}",
        f"Total Frame Tokens: ~{total_toks} tok | Identity Weight Ratio: {cache_ratio}%",
        f"Lorebook Budget: {lore_result['used_tokens']} / {lore_result['token_budget']} tok",
        "",
        "[DYNAMICALLY ASSEMBLED IDENTITY & LEXICAL STACK (prompt_order aligned)]",
        f"  Slot 0 (Top-Level System Instruction): {norm_epistemic}% Epistemic Lock",
        f"  Slot 1 (Preset '{preset.get('preset_name')}' + Card System): {estimate_tokens(slot1_sys)} tok",
        f"  Slot 2 (Before-Char Lore): {len(wi_before_items)} entries",
        f"  Slot 3 (Character + Persona '{user_name}'): {estimate_tokens(chr(10).join(slot3_parts))} tok",
        f"  Slot 4 (After-Char Lore + {lexical_block_count} <START> Lexical Samples): {estimate_tokens(chr(10).join(slot4_parts))} tok",
        f"  Enabled Prompt Order: {' -> '.join(ordered_ids)}",
        "",
        "[GOLDEN @DEPTH SPLICED LORE IN CHAT HISTORY]",
    ]
    for tr in lore_result.get("active_trace", []):
        dump_lines.append(
            f"  + [#{tr['uid']} {tr['comment']}] via={tr['trigger']} | pos={tr['position']} | order={tr['order']} | {tr['tokens']} tok"
        )
    if lore_result.get("evicted_trace"):
        dump_lines.append("[BUDGET EVICTED ENTRIES]")
        for ev in lore_result["evicted_trace"]:
            dump_lines.append(f"  - [#{ev['uid']} {ev['comment']}] evicted ({ev['tokens']} tok > remaining budget)")

    return {
        "character_name": char_name,
        "user_name": user_name,
        "mode": mode,
        "epistemic_level": norm_epistemic,
        "agentic_hands": bool(agentic_hands),
        "director_mode": bool(director_mode),
        "quarantined_backstage_count": quarantined_count,
        "lexical_sample_count": lexical_block_count,
        "preset_name": preset.get("preset_name", "BuiltIn-Carbon-RP"),
        "ordered_identifiers": ordered_ids,
        "static_tokens": static_toks,
        "dynamic_tokens": dynamic_toks,
        "prefix_cache_ratio_pct": cache_ratio,
        "ephemeral_frame": ephemeral_frame,
        "cache_mode": cache_mode,
        "static_prefix_block": static_prefix_block,
        "history_messages": history_messages,
        "dynamic_tail_block": dynamic_tail_block,
        "dump_report": "\n".join(dump_lines),
    }
