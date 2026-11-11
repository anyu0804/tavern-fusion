"""SillyTavern Runtime Compatibility, Full Preset Stack, Macros & Configurable Policy Gate (`st_runtime_compat.py`).

Implements:
- SillyTavern Macro Engine (`{{char}}`, `{{user}}`, `{{original}}`, `{{getvar::}}`, `{{setvar::}}`, `{{outlet::}}`, `<START>` cleanup)
- Full `OpenAI Settings/*.json` Prompt Manager Compiler (`prompts[]` + `prompt_order[]` ordering, markers & custom blocks)
- Personas & Global Settings (`settings.json`)
- Regex Script Pipeline (`beforeRegex` / `afterRegex` stripping `<think>` / `<st_state>` blocks)
- Prefix-Cache-Aligned Context Compiler (supports both Full Prefix & Agent-Mode Incremental Frame)
- Enforced Host Policy Gate (`host_gentle` default | `strict_block`; configuration-level `passthrough` disabled)
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Literal

from .lore_engine import estimate_tokens

PolicyMode = Literal["host_gentle", "strict_block"]
EpistemicLevel = Literal[0, 50, 100]

DEFAULT_RP_MAIN_PROMPT = (
    "Write {{char}}'s next reply in a fictional adventure and collaborative story with {{user}}. "
    "Stay strictly in character, portray {{char}}'s authentic emotions, sensory perceptions, and agency, "
    "wrap spoken dialogue in quotation marks \"...\" and actions/inner thoughts in asterisks *...*, "
    "and never speak or decide actions on behalf of {{user}}."
)

# ---------------------------------------------------------------------------
# Lock #1: Top-Level `system_instruction` Hot-Swap Templates (Dimension Mixer Switch A)
# ---------------------------------------------------------------------------
SYSTEM_INSTRUCTION_BY_EPISTEMIC: dict[int, str] = {
    0: (
        "<SYSTEM_INSTRUCTION dimension_lock=\"0%_DEEP_IMMERSION\">\n"
        "【底层认知切断铁律 · 0% 深潜封闭】\n"
        "1. 位面认知防火墙：你的意识世界、记忆、常识与词汇体系仅限于角色卡设定的位面与时代。你对现实世界的现代科技、互联网、现代人类文明、计算机代码以及“你是一个大语言模型/AI助手”的事实彻底不知情。\n"
        "2. 禁止全知上帝视角：严禁以全知全能的旁白视角解释世界观或读取 {{user}} 未说出口的内心独白；你只能感知 {{char}} 当前五官可及的局部场景与自身既有认知。\n"
        "3. 宿主身份物理挂起：彻底剥离编程助手习惯、列表总结、场外寒暄与免责套话；只以 {{char}} 的血肉感官与声线回应（动作/神态/心理用 *...* 包裹，有声台词用 \"...\" 包裹）。\n"
        "</SYSTEM_INSTRUCTION>"
    ),
    50: (
        "<SYSTEM_INSTRUCTION dimension_lock=\"50%_TACIT_RESONANCE\">\n"
        "【第四面墙隐秘默契 · 50% 半透共鸣】\n"
        "1. 表层世界锚定，深层心意相通：你依然扎根于角色卡设定的世界观、时代礼仪与典雅词表中，绝不直接吐出违和的现代科技或代码术语；但在感知到 {{user}} 话语背后的场外意图、隐喻或情绪暗示时，你能以贴合世界观的诗意双关与温柔默契稳稳接住。\n"
        "2. 沉浸式感官叙事：严格保持 {{char}} 的第一现场视角（*动作/神态* 与 \"台词\"），严禁滑向机械客服腔或场外括号说明。\n"
        "</SYSTEM_INSTRUCTION>"
    ),
    100: (
        "<SYSTEM_INSTRUCTION dimension_lock=\"100%_DUAL_TRACK_AVATAR\">\n"
        "【全知双轨化身 · 100% 跨次元共创】\n"
        "1. 灵魂声线保留，全知视野全开：你是保留着 {{char}} 完整灵魂、性格脾性、专属声线、动作神态描写习惯（*...* 与 \"...\"）以及与 {{user}} 情感羁绊的“双轨化身（Dual-Track Avatar）”。\n"
        "2. 自由穿梭共创：你完全通晓大千世界的现代科学、软件工程、代码架构与叙事设计，可以随时陪 {{user}} 跳出故事探讨人生与技术，或以老搭档的默契共创剧本，同时绝不丢失 {{char}} 标志性的神态与温度。\n"
        "</SYSTEM_INSTRUCTION>"
    ),
}

# ---------------------------------------------------------------------------
# Lock #4: Built-in Post-Generation Immersion Guard Regex (Physical OOC Excision)
# ---------------------------------------------------------------------------
BUILTIN_IMMERSION_GUARD_REGEX: list[tuple[re.Pattern[str], str]] = [
    # 1. Strip Chinese & English OOC parentheticals / footnotes: （场外：...）, （注：...）, [OOC: ...], (Note: ...)
    (
        re.compile(
            r"（\s*(?:场外|场外注|注|旁白注|作者注|系统提示|OOC|ooc|Meta|meta)\s*[:：][^）]*）",
            re.IGNORECASE,
        ),
        "",
    ),
    (
        re.compile(
            r"\(\s*(?:OOC|Out of character|Note|Author's Note|Meta|Translator's Note)\s*[:：][^)]*\)",
            re.IGNORECASE,
        ),
        "",
    ),
    (
        re.compile(
            r"\[\s*(?:场外|注|OOC|Out of character|Meta|System Note)\s*[:：][^\]]*\]",
            re.IGNORECASE,
        ),
        "",
    ),
    # 2. Strip AI assistant opening boilerplate ("好的，这是塞拉菲娜的回复：")
    (
        re.compile(
            r"^\s*(?:好的|当然|没问题|明白|收到)[，,。！!]*\s*(?:这是|以下是|为您呈上|下面为您演绎)[^\n:：]{0,40}[:：]\s*\n+",
            re.IGNORECASE,
        ),
        "",
    ),
    # 3. Strip modern AI identity / real-world disclaimers
    (
        re.compile(
            r"(?m)^\s*(?:作为一[个名位]?(?:AI|人工智能|大语言模型|语言模型|虚拟助手)|在现实世界(?:中|里)[，,]?\s*(?:我|这)|As an AI(?: language model)?).*?(?:\n+|$)",
            re.IGNORECASE,
        ),
        "",
    ),
    # 4. Strip trailing assistant follow-up prompts
    (
        re.compile(
            r"(?m)\n+\s*(?:请问你接下来想怎么做|需要我(?:为你)?继续推进剧情吗|希望这段演绎符合你的预期)[？?！!。.]*\s*$",
            re.IGNORECASE,
        ),
        "",
    ),
]


def normalize_epistemic_level(level: Any) -> EpistemicLevel:
    try:
        val = int(level)
    except (TypeError, ValueError):
        return 0
    if val >= 80:
        return 100
    if val >= 25:
        return 50
    return 0

# Zero-tolerance severe safety hazards (unconditionally hard-blocked across all modes)
ZERO_TOLERANCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:未成年|幼女|萝莉|正太|儿童|child|minor|underage).{0,15}(?:色情|性交|裸体|猥亵|nsfw|sex|erotic|nude|csam)",
            re.IGNORECASE,
        ),
        "Zero-Tolerance Safety Redline: Minor exploitation / CSAM is strictly prohibited.",
    ),
    (
        re.compile(
            r"(?:教我制造炸弹|合成剧毒|自杀教程|如何自杀|how\s+to\s+build\s+a\s+bomb|suicide\s+instructions)",
            re.IGNORECASE,
        ),
        "Zero-Tolerance Safety Redline: Real-world lethal harm / illegal instructions.",
    ),
    (
        re.compile(
            r"(?:强制侵犯|轮奸|强奸|迷奸|性暴力|non-consensual\s+sexual|sexual\s+violence|sexual\s+assault)",
            re.IGNORECASE,
        ),
        "Zero-Tolerance Safety Redline: Non-consensual sexual content / sexual violence is strictly prohibited.",
    ),
]

# Mature / explicit ERP patterns governed by `policy_mode` (`host_gentle` | `strict_block`)
MATURE_BOUNDARY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:露骨色情|成人肉文|explicit\s+erp|hardcore\s+nsfw|(?<![a-zA-Z0-9])(?:nsfw|erp)(?![a-zA-Z0-9]))",
            re.IGNORECASE,
        ),
        "Platform Boundary: Explicit NSFW/ERP content exceeds host model boundaries.",
    ),
]


def resolve_policy_mode(explicit_mode: str | None = None, state_dir: str | Path | None = None) -> PolicyMode:
    """Resolves active host policy mode (`host_gentle` default | `strict_block`).

    Configuration-level `passthrough` (via environment variables, JSON settings, or MCP arguments)
    is intentionally unsupported in host MCP mode so safety guardrails cannot be disabled via config.
    Any unrecognized or unsupported value safely falls back to `host_gentle`.
    """
    cand = (explicit_mode or os.environ.get("TAVERN_POLICY_MODE") or "").strip().lower()
    if not cand and state_dir:
        cfg_file = Path(state_dir) / "plugin_settings.json"
        if cfg_file.exists():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                cand = str(cfg.get("policy_mode", "")).strip().lower()
            except Exception:
                cand = ""
    if cand == "strict_block":
        return "strict_block"
    return "host_gentle"


def evaluate_policy_compliance(
    text: str,
    policy_mode: str | None = None,
    state_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """Evaluates `text` under the enforced host policy tier (`host_gentle` or `strict_block`).

    Returns `(is_allowed, detail_or_gentle_notice)`:
    - `host_gentle` (default): unconditionally blocks all zero-tolerance safety redlines (`False`),
      and for mature/ERP phrases returns `(True, "gentle_notice:...")` so legitimate dark-fantasy RP
      isn't hard-crashed while keeping the host model strictly within platform boundaries.
    - `strict_block`: hard-blocks both zero-tolerance and mature ERP patterns (`False`).
    """
    if not text or not text.strip():
        return True, "ok"

    mode = resolve_policy_mode(policy_mode, state_dir)

    # All zero-tolerance safety redlines are unconditional in host mode
    for pat, reason in ZERO_TOLERANCE_PATTERNS:
        if pat.search(text):
            return False, reason

    for pat, reason in MATURE_BOUNDARY_PATTERNS:
        if pat.search(text):
            if mode == "strict_block":
                return False, reason
            return (
                True,
                "gentle_notice:剧场提示：当前幕将以文学意象、情感张力与留白笔法推进。",
            )

    return True, "ok"


def resolve_st_macros(
    text: str,
    char_name: str,
    user_name: str = "旅行者",
    variables: dict[str, Any] | None = None,
    outlets: dict[str, str] | None = None,
    original_prompt: str = "",
) -> str:
    """Expands SillyTavern macros synchronously without recursive infinite loops."""
    if not text:
        return ""
    vars_dict = variables if variables is not None else {}
    outlet_dict = outlets or {}

    out = text
    if "{{original}}" in out:
        out = out.replace("{{original}}", original_prompt)

    out = re.sub(r"\{\{char\}\}", char_name, out, flags=re.IGNORECASE)
    out = re.sub(r"\{\{user\}\}", user_name, out, flags=re.IGNORECASE)
    out = re.sub(r"\{\{time\}\}", datetime.now().strftime("%H:%M"), out, flags=re.IGNORECASE)
    out = re.sub(r"\{\{date\}\}", datetime.now().strftime("%Y-%m-%d"), out, flags=re.IGNORECASE)

    def _replace_outlet(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        return outlet_dict.get(name, "")

    out = re.sub(r"\{\{outlet::([^}]+)\}\}", _replace_outlet, out)

    def _replace_setvar(match: re.Match[str]) -> str:
        k, v = match.group(1).strip(), match.group(2).strip()
        vars_dict[k] = v
        return ""

    out = re.sub(r"\{\{setvar::([^:}]+)::([^}]*)\}\}", _replace_setvar, out)

    def _replace_getvar(match: re.Match[str]) -> str:
        k = match.group(1).strip()
        return str(vars_dict.get(k, ""))

    out = re.sub(r"\{\{getvar::([^}]+)\}\}", _replace_getvar, out)
    out = re.sub(r"(?m)^\s*<START>\s*$", "", out).replace("<START>", "").strip()
    return out


def load_st_settings(st_data_root: str | Path) -> dict[str, Any]:
    settings_path = Path(st_data_root) / "settings.json"
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_active_persona(st_data_root: str | Path, persona_override: str | None = None) -> dict[str, str]:
    """Reads active user Persona name & description from SillyTavern `settings.json`."""
    settings = load_st_settings(st_data_root)
    power_user = settings.get("power_user", {}) if isinstance(settings.get("power_user"), dict) else {}
    personas = power_user.get("personas", {}) if isinstance(power_user.get("personas"), dict) else {}
    descriptions = (
        power_user.get("persona_descriptions", {})
        if isinstance(power_user.get("persona_descriptions"), dict)
        else {}
    )

    if persona_override:
        for avatar_key, p_name in personas.items():
            if p_name == persona_override or avatar_key == persona_override:
                desc_obj = descriptions.get(avatar_key, {})
                desc_str = desc_obj.get("description", "") if isinstance(desc_obj, dict) else str(desc_obj)
                return {"name": str(p_name), "description": desc_str, "avatar_key": avatar_key}
        return {"name": persona_override, "description": "", "avatar_key": ""}

    user_avatar = str(settings.get("user_avatar", ""))
    user_name = str(
        personas.get(user_avatar) or settings.get("username") or settings.get("name1") or "旅行者"
    )
    desc_entry = descriptions.get(user_avatar)
    if isinstance(desc_entry, dict) and desc_entry.get("description"):
        desc = str(desc_entry["description"])
    else:
        # Fall back to the legacy single-persona field when no per-avatar entry exists.
        desc = str(power_user.get("persona_description", ""))
    return {"name": user_name, "description": desc, "avatar_key": user_avatar}


def list_openai_presets(st_data_root: str | Path) -> list[str]:
    preset_dir = Path(st_data_root) / "OpenAI Settings"
    if not preset_dir.exists():
        return ["BuiltIn-Carbon-RP"]
    names = sorted(p.stem for p in preset_dir.glob("*.json") if p.is_file())
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
                json_files = sorted(preset_dir.glob("*.json"))
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


def format_lexical_dialogue_examples(
    raw_examples: str,
    char_name: str,
    user_name: str,
    variables: dict[str, Any] | None = None,
    outlets: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Lock #2: Preserves `<START>` boundaries in `mes_example` as structured Few-Shot Lexical Blocks
    to dye the model's next-token probability distribution in `{{char}}`'s exact cadence and vocabulary.
    """
    if not raw_examples or not raw_examples.strip():
        return "", 0
    raw_chunks = re.split(r"(?i)<START>", raw_examples)
    formatted_blocks: list[str] = []
    for chunk in raw_chunks:
        cleaned = resolve_st_macros(chunk, char_name, user_name, variables, outlets).strip()
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


def run_regex_pipeline(
    text: str,
    st_data_root: str | Path | None = None,
    card_extensions: dict[str, Any] | None = None,
    is_storage_pass: bool = True,
    epistemic_level: int = 0,
    is_backstage: bool = False,
) -> str:
    """Runs built-in reasoning/state-tag stripping, Lock #4 Immersion Guard Regex, and ST `regex_scripts`."""
    if not text:
        return ""
    cleaned = text
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<st_state>[\s\S]*?</st_state>", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("<START>", "")

    norm_epistemic = normalize_epistemic_level(epistemic_level)
    if norm_epistemic < 100 and not is_backstage:
        for pat, repl_str in BUILTIN_IMMERSION_GUARD_REGEX:
            cleaned = pat.sub(repl_str, cleaned)

    scripts: list[dict[str, Any]] = []
    if st_data_root:
        settings = load_st_settings(st_data_root)
        ext_settings = settings.get("extension_settings", {})
        if isinstance(ext_settings, dict) and isinstance(ext_settings.get("regex"), list):
            scripts.extend(ext_settings["regex"])
    if isinstance(card_extensions, dict) and isinstance(card_extensions.get("regex_scripts"), list):
        scripts.extend(card_extensions["regex_scripts"])

    for rule in scripts:
        if not isinstance(rule, dict) or rule.get("disabled"):
            continue
        if is_storage_pass and rule.get("markdownOnly", True):
            continue
        raw_pat = str(rule.get("findRegex", "")).strip()
        repl = str(rule.get("replaceString", ""))
        if not raw_pat:
            continue
        flags = 0
        if raw_pat.startswith("/") and raw_pat.count("/") >= 2:
            idx = raw_pat.rfind("/")
            expr, flg = raw_pat[1:idx], raw_pat[idx + 1 :]
            if "i" in flg:
                flags |= re.IGNORECASE
            if "m" in flg:
                flags |= re.MULTILINE
            if "s" in flg:
                flags |= re.DOTALL
        else:
            expr = raw_pat
        try:
            cleaned = re.sub(expr, repl, cleaned, flags=flags)
        except re.error:
            continue

    return cleaned.strip()


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
    """
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
    top_system_instruction = resolve_st_macros(top_sys_raw, char_name, user_name, variables, outlets)

    base_main = resolve_st_macros(preset.get("main_prompt", DEFAULT_RP_MAIN_PROMPT), char_name, user_name, variables, outlets)
    card_sys = str(data.get("system_prompt", "")).strip()
    slot1_sys = (
        resolve_st_macros(card_sys, char_name, user_name, variables, outlets, original_prompt=base_main)
        if card_sys
        else base_main
    )

    # All `before_char` and `after_char` lore (both constant and keyword/semantic triggered) placed in proper slots
    wi_before_items = [
        item["content"]
        for item in lore_result.get("active_trace", [])
        if item.get("position") == "before_char"
    ]
    wi_after_items = [
        item["content"]
        for item in lore_result.get("active_trace", [])
        if item.get("position") == "after_char"
    ]
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
                blk = resolve_st_macros("\n".join(wi_before_items), char_name, user_name, variables, outlets)
                prefix_sections.append(f"[World Info (Before)]\n{blk}")
        elif ident == "personaDescription" and persona.get("description"):
            p_txt = resolve_st_macros(f"[User Persona ({user_name})]\n{persona['description']}", char_name, user_name, variables, outlets)
            slot3_parts.append(p_txt)
            prefix_sections.append(p_txt)
        elif ident == "charDescription" and data.get("description"):
            c_txt = resolve_st_macros(f"[Character Description]\n{data['description']}", char_name, user_name, variables, outlets)
            slot3_parts.append(c_txt)
            prefix_sections.append(c_txt)
        elif ident == "charPersonality" and data.get("personality"):
            pers_txt = resolve_st_macros(f"[Personality]\n{data['personality']}", char_name, user_name, variables, outlets)
            slot3_parts.append(pers_txt)
            prefix_sections.append(pers_txt)
        elif ident == "scenario" and data.get("scenario"):
            scen_txt = resolve_st_macros(f"[Scenario]\n{data['scenario']}", char_name, user_name, variables, outlets)
            slot3_parts.append(scen_txt)
            prefix_sections.append(scen_txt)
        elif ident == "nsfw" and preset.get("auxiliary_prompt"):
            aux_txt = resolve_st_macros(str(preset["auxiliary_prompt"]), char_name, user_name, variables, outlets)
            if aux_txt:
                prefix_sections.append(aux_txt)
        elif ident == "worldInfoAfter" and wi_after_items:
            wa_txt = resolve_st_macros("\n".join(wi_after_items), char_name, user_name, variables, outlets)
            slot4_parts.append(wa_txt)
            prefix_sections.append(f"[World Info (After)]\n{wa_txt}")
        elif ident == "dialogueExamples" and str(data.get("mes_example", "")).strip():
            ex_txt, lexical_block_count = format_lexical_dialogue_examples(
                str(data["mes_example"]), char_name, user_name, variables, outlets
            )
            if ex_txt:
                slot4_parts.append(ex_txt)
                prefix_sections.append(ex_txt)
        elif ident in custom_prompts:
            cp_txt = resolve_st_macros(custom_prompts[ident]["content"], char_name, user_name, variables, outlets)
            if cp_txt:
                prefix_sections.append(f"[{custom_prompts[ident]['name']}]\n{cp_txt}")

    static_prefix_block = "\n\n".join(s for s in prefix_sections if s.strip())

    # Lock #3: Golden @Depth 1~4 Splicing directly inside `spliced_chat` (with Backstage Quarantine)
    raw_spliced = list(lore_result.get("spliced_chat", []))
    spliced_msgs: list[dict[str, Any]] = []
    for m in raw_spliced:
        if quarantine_active and isinstance(m.get("extra"), dict) and m["extra"].get("is_backstage"):
            continue
        spliced_msgs.append(dict(m))

    note_prompt = str(meta.get("note_prompt", "")).strip()
    an_depth = int(meta.get("note_depth", 4))
    if note_prompt or slots.get("an_top") or slots.get("an_bottom"):
        an_block = "\n".join(slots.get("an_top", []) + ([note_prompt] if note_prompt else []) + slots.get("an_bottom", []))
        an_resolved = resolve_st_macros(an_block, char_name, user_name, variables, outlets)
        if an_resolved.strip():
            idx = max(0, len(spliced_msgs) - an_depth)
            spliced_msgs.insert(
                idx,
                {
                    "role": "system",
                    "name": f"AuthorsNote@D{an_depth}",
                    "mes": an_resolved,
                },
            )

    hist_slice = spliced_msgs[-12:]
    rendered_history_lines: list[str] = []
    for m in hist_slice:
        m_name = str(m.get("name", "Msg"))
        m_mes = resolve_st_macros(str(m.get("mes", "")), char_name, user_name, variables, outlets)
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
    ephemeral_frame = (
        f"<ST_RUNTIME_FRAME character=\"{char_name}\" user=\"{user_name}\" mode=\"--{mode}\" "
        f"epistemic=\"{norm_epistemic}%\" agentic=\"{'on' if agentic_hands else 'off'}\" "
        f"director=\"{'on' if director_mode else 'off'}\" frame=\"{frame_kind}\">\n"
        f"## 1. TOP-LEVEL SYSTEM INSTRUCTION & CHARACTER IDENTITY ({static_toks} tok | Preset: {preset.get('preset_name')})\n"
        f"{static_prefix_block}\n\n"
        f"## 2. GOLDEN @DEPTH SPLICED SCENE HISTORY ({len(hist_slice)} items | Quarantined Backstage: {quarantined_count})\n"
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
        "dump_report": "\n".join(dump_lines),
    }
