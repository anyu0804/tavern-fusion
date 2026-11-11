"""SillyTavern global `settings.json` and the active user Persona.

Moved verbatim from `server/st_runtime_compat.py`, which re-exports these names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
