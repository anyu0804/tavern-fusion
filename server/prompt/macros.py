"""SillyTavern macro engine (`{{char}}`, `{{user}}`, `{{original}}`, `{{getvar::}}`, `{{setvar::}}`, `{{outlet::}}`, `<START>`).

Moved verbatim from `server/st_runtime_compat.py`, which re-exports these names.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any


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
