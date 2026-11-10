#!/usr/bin/env python3
"""Antigravity `SessionStart` Lifecycle Hook (`hooks/session_init.py`).

Strictly isolated by workspace & `rp_conv_id`:
- Emits ZERO output in non-SillyTavern workspaces (e.g. general coding repositories),
  keeping unrelated engineering sessions 100% clean.
- When inside a SillyTavern workspace, summarizes available Character Cards & Lorebooks
  without claiming an active RP session unless `conversationId == rp_conv_id`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from server.mcp_server import TavernFusionRuntime


def discover_st_data_from_workspaces(workspace_paths: list[str]) -> str | None:
    for wp in workspace_paths:
        base = Path(wp)
        for rel in ("data/default-user", "SillyTavern/data/default-user", "../SillyTavern/data/default-user"):
            cand = (base / rel).resolve()
            if cand.exists() and cand.is_dir():
                return str(cand)
    return None


def check_st_web_alive(url: str = "http://127.0.0.1:8000") -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=0.3) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main() -> None:
    raw_in = sys.stdin.read().strip()
    payload = json.loads(raw_in) if raw_in else {}
    ws_paths = payload.get("workspacePaths") or []
    conv_id = str(payload.get("conversationId") or "")

    discovered = discover_st_data_from_workspaces(ws_paths)
    if not discovered:
        # Non-SillyTavern workspace -> stay 100% silent, zero context pollution
        sys.stdout.write(json.dumps({"injectSteps": []}) + "\n")
        return

    runtime = TavernFusionRuntime(st_data_root=discovered)
    try:
        runtime.save_plugin_settings({"last_st_data_root": discovered})
        cards_dir = runtime.st_data_root / "characters"
        worlds_dir = runtime.st_data_root / "worlds"
        card_count = len([p for p in cards_dir.glob("*.png") if p.is_file()]) if cards_dir.exists() else 0
        world_names = [p.stem for p in sorted(worlds_dir.glob("*.json"))[:8]] if worlds_dir.exists() else []
        web_online = check_st_web_alive(os.environ.get("ST_WEB_URL", "http://127.0.0.1:8000"))

        active_sess = runtime.load_active_session()
        is_bound_rp = (
            bool(conv_id)
            and bool(active_sess.get("active"))
            and active_sess.get("rp_conv_id") == conv_id
            and bool(active_sess.get("character_name"))
        )
        mode_note = (
            f"Active RP session bound to this chat: {active_sess.get('character_name')} ({Path(active_sess.get('chat_path', '')).name})"
            if is_bound_rp
            else "Standard engineering mode (RP isolated; type `/rp <CharacterName>` or use `[Tavern Stage](sidecar://tavern-fusion/tavern_stage/)` to open a dedicated RP chat)."
        )

        msg = (
            f"[Tavern Fusion v3.0 Ready] Data profile: `{runtime.st_data_root.name}` "
            f"(cards={card_count}, lorebooks={world_names or ['none']}, st_web_8000={'online' if web_online else 'offline'}). "
            f"{mode_note}"
        )
        out = {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
