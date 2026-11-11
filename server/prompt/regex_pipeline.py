"""Regex script pipeline (`beforeRegex` / `afterRegex`, JS replacement tokens) and the built-in immersion guard.

Moved verbatim from `server/st_runtime_compat.py`, which re-exports these names.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .epistemic import normalize_epistemic_level
from .st_settings import load_st_settings

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


_JS_REPL_TOKEN_RE = re.compile(r"\$(?:\$|&|`|'|(\d{1,2})|<([^>]+)>)")


def _apply_js_regex_replacement(match: re.Match[str], repl: str) -> str:
    """Applies JavaScript `String.prototype.replace` substitution tokens (`$1`, `$&`, `$$`, `$<name>`)."""
    if "$" not in repl:
        return repl
    num_groups = match.re.groups

    def _sub_token(tok: re.Match[str]) -> str:
        raw = tok.group(0)
        if raw == "$$":
            return "$"
        if raw == "$&":
            return match.group(0)
        if raw == "$`":
            return match.string[: match.start()]
        if raw == "$'":
            return match.string[match.end() :]
        digits = tok.group(1)
        if digits is not None:
            idx = int(digits)
            if 1 <= idx <= num_groups:
                val = match.group(idx)
                return "" if val is None else val
            if len(digits) == 2:
                idx1 = int(digits[0])
                if 1 <= idx1 <= num_groups:
                    val1 = match.group(idx1)
                    return ("" if val1 is None else val1) + digits[1]
            return raw
        gname = tok.group(2)
        if gname is not None:
            if not match.re.groupindex:
                return ""
            try:
                val_n = match.group(gname)
                return "" if val_n is None else val_n
            except IndexError:
                return ""
        return raw

    return _JS_REPL_TOKEN_RE.sub(_sub_token, repl)


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
        # ST semantics: a field absent means `false`. Only skip scripts explicitly scoped
        # away from storage: `markdownOnly` (display-only) and `promptOnly` / `is_user_regex`
        # (never alter the character's stored text). The old `get("markdownOnly", True)`
        # default silently skipped every script that did not set the field explicitly.
        if is_storage_pass and (
            rule.get("markdownOnly") or rule.get("promptOnly") or rule.get("is_user_regex")
        ):
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
            cleaned = re.sub(expr, lambda m, r=repl: _apply_js_regex_replacement(m, r), cleaned, flags=flags)
        except re.error:
            continue

    return cleaned.strip()
