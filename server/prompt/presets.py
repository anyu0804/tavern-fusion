"""`OpenAI Settings/*.json` preset loader (`prompts[]` + `prompt_order[]`).

Moved verbatim from `server/st_runtime_compat.py`, which re-exports these names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RP_MAIN_PROMPT = (
    "Write {{char}}'s next reply in a fictional adventure and collaborative story with {{user}}. "
    "Stay strictly in character, portray {{char}}'s authentic emotions, sensory perceptions, and agency, "
    "wrap spoken dialogue in quotation marks \"...\" and actions/inner thoughts in asterisks *...*, "
    "and never speak or decide actions on behalf of {{user}}."
)


def list_openai_presets(st_data_root: str | Path) -> list[str]:
    preset_dir = Path(st_data_root) / "OpenAI Settings"
    if not preset_dir.exists():
        return ["BuiltIn-Carbon-RP"]
    names = sorted((p.stem for p in preset_dir.glob("*.json") if p.is_file()), key=lambda s: (s.lower(), s))
    return names or ["BuiltIn-Carbon-RP"]


def load_openai_preset(st_data_root: str | Path, preset_name: str | None = None) -> dict[str, Any]:
    """Parses full SillyTavern `OpenAI Settings/*.json` including `prompts[]` and `prompt_order[]`."""
    preset_dir = Path(st_data_root) / "OpenAI Settings"
    chosen_file: Path | None = None
    if preset_dir.exists():
        if preset_name and preset_name != "BuiltIn-Carbon-RP":
            candidate = preset_dir / (preset_name if preset_name.endswith(".json") else f"{preset_name}.json")
            if candidate.exists():
                chosen_file = candidate
        if chosen_file is None:
            default_cand = preset_dir / "Default.json"
            if default_cand.exists():
                chosen_file = default_cand
            else:
                json_files = sorted(preset_dir.glob("*.json"), key=lambda item: (item.name.lower(), item.name))
                if json_files:
                    chosen_file = json_files[0]

    default_order = [
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

    if chosen_file is None:
        return {
            "preset_name": "BuiltIn-Carbon-RP",
            "main_prompt": DEFAULT_RP_MAIN_PROMPT,
            "auxiliary_prompt": "",
            "post_history_instructions": "",
            "impersonation_prompt": "[Write your next reply from the point of view of {{user}}.]",
            "continue_nudge_prompt": "[Continue your last message without repeating its original content.]",
            "group_nudge_prompt": "[Write the next reply only as {{char}}.]",
            "wi_format": "{0}",
            "scenario_format": "{{scenario}}",
            "personality_format": "{{personality}}",
            "temperature": 0.85,
            "top_p": 1.0,
            "top_k": 0,
            "min_p": 0.0,
            "ordered_identifiers": default_order,
            "custom_prompts": {},
        }

    raw = json.loads(chosen_file.read_text(encoding="utf-8"))
    prompts_list = raw.get("prompts", [])
    prompt_defs: dict[str, dict[str, Any]] = {}
    if isinstance(prompts_list, list):
        for p in prompts_list:
            if isinstance(p, dict) and p.get("identifier"):
                prompt_defs[str(p["identifier"])] = p

    # Resolve prompt_order (prefer character_id 100001 which includes personaDescription in ST v1.19, else 100000)
    order_entries: list[dict[str, Any]] = []
    raw_order = raw.get("prompt_order", [])
    if isinstance(raw_order, list):
        by_cid = {int(b.get("character_id", 0)): b.get("order", []) for b in raw_order if isinstance(b, dict)}
        chosen_ord = by_cid.get(100001) or by_cid.get(100000) or (raw_order[0].get("order", []) if raw_order and isinstance(raw_order[0], dict) else [])
        if isinstance(chosen_ord, list):
            order_entries = [x for x in chosen_ord if isinstance(x, dict)]

    if order_entries:
        enabled_identifiers = [
            str(x["identifier"])
            for x in order_entries
            if x.get("enabled", True) is not False and x.get("identifier")
        ]
    else:
        enabled_identifiers = [
            ident for ident, d in prompt_defs.items() if d.get("enabled", True) is not False
        ] or default_order

    enabled_set = set(enabled_identifiers)

    def _get_prompt_text(ident: str, fallback: str = "") -> str:
        if ident not in enabled_set:
            return ""
        d = prompt_defs.get(ident, {})
        val = str(d.get("content", "")).strip()
        return val if val else fallback

    main_prompt = _get_prompt_text("main", DEFAULT_RP_MAIN_PROMPT)
    # In ST Default.json, main is very terse ("Write {{char}}'s next reply..."); enrich with RP typography contract if missing
    if "quotation marks" not in main_prompt and '"..."' not in main_prompt:
        main_prompt = (
            f"{main_prompt} Stay strictly in character, wrap spoken dialogue in \"...\" "
            "and physical actions/inner thoughts in *...*, and never speak for {{user}}."
        )

    builtin_markers = {
        "main",
        "nsfw",
        "jailbreak",
        "postHistoryInstructions",
        "worldInfoBefore",
        "worldInfoAfter",
        "personaDescription",
        "charDescription",
        "charPersonality",
        "scenario",
        "dialogueExamples",
        "chatHistory",
    }
    custom_prompts: dict[str, dict[str, Any]] = {}
    for ident in enabled_identifiers:
        if ident not in builtin_markers and ident in prompt_defs:
            pdef = prompt_defs[ident]
            if not pdef.get("marker") and str(pdef.get("content", "")).strip():
                custom_prompts[ident] = {
                    "name": str(pdef.get("name", ident)),
                    "role": str(pdef.get("role", "system")),
                    "content": str(pdef["content"]),
                    "injection_position": int(pdef.get("injection_position", 0)),
                    "injection_depth": int(pdef.get("injection_depth", 4)),
                }

    post_hist = _get_prompt_text("jailbreak") or _get_prompt_text("postHistoryInstructions")
    aux_prompt = _get_prompt_text("nsfw")

    return {
        "preset_name": chosen_file.stem,
        "main_prompt": main_prompt,
        "auxiliary_prompt": aux_prompt,
        "post_history_instructions": post_hist,
        "impersonation_prompt": str(raw.get("impersonation_prompt", "")),
        "continue_nudge_prompt": str(raw.get("continue_nudge_prompt", "")),
        "group_nudge_prompt": str(raw.get("group_nudge_prompt", "")),
        "wi_format": str(raw.get("wi_format", "{0}")),
        "scenario_format": str(raw.get("scenario_format", "{{scenario}}")),
        "personality_format": str(raw.get("personality_format", "{{personality}}")),
        "temperature": float(raw.get("temperature", 0.85)),
        "top_p": float(raw.get("top_p", 1.0)),
        "top_k": int(raw.get("top_k", 0)),
        "min_p": float(raw.get("min_p", 0.0)),
        "ordered_identifiers": enabled_identifiers,
        "custom_prompts": custom_prompts,
    }
