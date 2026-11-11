#!/usr/bin/env python3
"""Cross-Platform Local Installer & Global Sync Registrar (`scripts/install_local.py`).

Supports clean installation targets with unified state at `~/.gemini/antigravity/tavern-fusion/state`
and ZERO hardcoded OS user paths:

1. `--global-plugin`:
   Deploys `tavern-fusion` into `~/.gemini/config/plugins/tavern-fusion` (including the
   `Tavern Stage` Aux Pane Sidecar bundle), while removing any duplicate workspace
   `.agents/plugins.json` entry to prevent double Hook execution.
2. `--workspace <dir>`:
   Opt-in workspace-only registration in `<workspace>/.agents/plugins.json` (when not using `--global-plugin`).
3. `--st-root <dir>`:
   Installs the optional client-side `st-ag-sync` browser extension into
   `<st_root>/data/<user-handle>/extensions/third-party/st-ag-sync` and records `last_st_data_root`
   inside `~/.gemini/antigravity/tavern-fusion/state/plugin_settings.json`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def unified_state_dir() -> Path:
    d = Path.home() / ".gemini" / "antigravity" / "tavern-fusion" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_workspace_plugin(workspace_dir: Path) -> Path:
    agents_dir = workspace_dir.resolve() / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plugins_json = agents_dir / "plugins.json"

    try:
        rel_parent = PLUGIN_ROOT.parent.resolve().relative_to(workspace_dir.resolve())
        entry_path = "." if str(rel_parent) == "." else rel_parent.as_posix()
    except ValueError:
        entry_path = PLUGIN_ROOT.parent.resolve().as_posix()

    config_obj: dict = {"entries": []}
    if plugins_json.exists():
        try:
            loaded = json.loads(plugins_json.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config_obj = loaded
        except Exception:
            config_obj = {"entries": []}

    entries = config_obj.setdefault("entries", [])
    already = any(
        isinstance(e, dict)
        and e.get("path") == entry_path
        and ("tavern-fusion" in (e.get("include_only") or ["tavern-fusion"]))
        for e in entries
    )
    if not already:
        entries.append({
            "path": entry_path,
            "include_only": [PLUGIN_ROOT.name],
        })

    plugins_json.write_text(json.dumps(config_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plugins_json


def remove_duplicate_workspace_entry(workspace_dir: Path) -> bool:
    plugins_json = workspace_dir.resolve() / ".agents" / "plugins.json"
    if not plugins_json.exists():
        return False
    try:
        loaded = json.loads(plugins_json.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), list):
            return False
        orig_len = len(loaded["entries"])
        loaded["entries"] = [
            e
            for e in loaded["entries"]
            if not (isinstance(e, dict) and "tavern-fusion" in (e.get("include_only") or []))
        ]
        if len(loaded["entries"]) != orig_len:
            plugins_json.write_text(json.dumps(loaded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def install_st_web_extension(st_root: Path, user_handle: str = "default-user") -> Path:
    src_ext = PLUGIN_ROOT / "st_web_extension" / "st-ag-sync"
    dest_ext = st_root.resolve() / "data" / user_handle / "extensions" / "third-party" / "st-ag-sync"
    dest_ext.parent.mkdir(parents=True, exist_ok=True)
    if dest_ext.exists():
        shutil.rmtree(dest_ext)
    shutil.copytree(src_ext, dest_ext)
    return dest_ext


def install_global_plugin_and_sidecar() -> Path:
    ignore = shutil.ignore_patterns(
        ".runtime_state",
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        "node_modules",
        ".git",
        "tests",
    )
    global_plugin_dir = Path.home() / ".gemini" / "config" / "plugins" / "tavern-fusion"
    global_plugin_dir.mkdir(parents=True, exist_ok=True)
    legacy_link = global_plugin_dir / ".runtime_state"
    if legacy_link.is_symlink():
        legacy_link.unlink(missing_ok=True)
    shutil.copytree(PLUGIN_ROOT, global_plugin_dir, ignore=ignore, dirs_exist_ok=True)
    # Only deploy under plugins/tavern-fusion so Antigravity mounts `tavern-fusion/tavern_stage`
    # once (with plugin.json's custom logo) instead of duplicating a puzzle-icon sidecar.
    return global_plugin_dir


def seed_mock_card_if_missing() -> Path:
    from server.card_codec import write_character_card_png

    mock_char_dir = PLUGIN_ROOT / "examples" / "mock_st_data" / "default-user" / "characters"
    mock_char_dir.mkdir(parents=True, exist_ok=True)
    card_png = mock_char_dir / "Seraphina.png"
    if not card_png.exists():
        payload = {
            "spec": "chara_card_v3",
            "spec_version": "3.0",
            "data": {
                "name": "Seraphina",
                "description": "驻守艾尔多利亚星陨钟塔的苍银守望者，擅长观测魔力潮汐与修复古星象仪。",
                "personality": "温雅克制、观察入微，谈及古代铭文时流露罕见的热忱。",
                "scenario": "{{user}}在星界裂隙微光浮现的雨夜登上了星陨钟塔顶层。",
                "first_mes": "*雨丝敲打着星陨钟塔的高窗，Seraphina 轻轻合上铜制星盘，回眸望向推门而入的{{user}}。*\n\n\"今夜的星界潮汐比往常提前了半个时辰，你也是循着钟声而来的吗？\"",
                "mes_example": (
                    "<START>\n"
                    "{{user}}: *走近星盘观察刻度* \"裂隙还在扩大吗？\"\n"
                    "{{char}}: *指尖掠过微凉的银环，烛火在她鸦青色的睫羽下投出浅影，语气平静却透着凝重。* \"仍在星轨闭环之内，但我们必须在黎明钟声敲响前，用月见草浸液重新校准第三道铭文。\"\n"
                    "<START>\n"
                    "{{user}}: \"如果用现代计算机跑一遍轨道模型会快很多吧？\"\n"
                    "{{char}}: *她微微歪过头，苍银色的眼眸里浮起一丝困惑而纯粹的探究，指节轻轻叩了叩黄铜星盘。* \"计……算机？那是北境炼金学派新铸的差分齿轮匣吗？钟塔的星图只认星髓与银针的共鸣。\"\n"
                    "<START>\n"
                    "{{user}}: *将冻僵的手拢在炉火旁* \"今夜有劳你守塔了。\"\n"
                    "{{char}}: *她不动声色地将温在火炉边的琥珀草药茶推到{{user}}手边，唇角漾开一抹极淡的暖意。* \"守望者本就与长夜为伴。趁热饮下吧，山风里的寒气重。\""
                ),
                "system_prompt": "{{original}}\nMaintain atmospheric sensory details and subtle emotional progression.",
                "alternate_greetings": [
                    "*炉火映亮琥珀茶盏，Seraphina 将一方干爽披风递给刚踏入塔楼的{{user}}。*\n\n\"银雾山脉的夜风很凉，先喝口热茶暖一暖吧。\""
                ],
                "tags": ["fantasy", "scholar", "eldoria"],
                "creator": "安钰 (AnYu)",
                "character_version": "3.0.0",
                "extensions": {"world": "Eldoria"},
            },
        }
        write_character_card_png(payload, card_png)
    return card_png


def remember_st_data_root(st_root: Path | None, user_handle: str = "default-user") -> Path | None:
    candidates: list[Path] = []
    if st_root is not None:
        candidates.append(st_root.resolve() / "data" / user_handle)
        candidates.append(st_root.resolve())
    candidates.append(PLUGIN_ROOT.parent / "SillyTavern" / "data" / user_handle)
    candidates.append(PLUGIN_ROOT.parent / "data" / user_handle)

    chosen: Path | None = None
    for c in candidates:
        if (c / "characters").exists() and c.is_dir():
            chosen = c.resolve()
            break
    if chosen is None:
        return None

    from server.fs_safety import atomic_write_json

    st_dir = unified_state_dir()
    cfg_file = st_dir / "plugin_settings.json"
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["last_st_data_root"] = str(chosen)
    atomic_write_json(cfg_file, cfg, indent=2)
    return chosen


def emit_one_time_cli_notice() -> None:
    from server.fs_safety import atomic_write_json
    from server.mcp_server import FIRST_RUN_NOTICE_V1

    st_dir = unified_state_dir()
    cfg_file = st_dir / "plugin_settings.json"
    cfg: dict = {}
    if cfg_file.exists():
        try:
            loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg = loaded
        except Exception:
            cfg = {}
    if cfg.get("first_run_notice_ack_v1") or cfg.get("first_run_cli_shown_v1"):
        return

    print("\n" + "=" * 68)
    print(f"[NOTICE] {FIRST_RUN_NOTICE_V1['title']}")
    for idx, item in enumerate(FIRST_RUN_NOTICE_V1["items"], start=1):
        print(f"  {idx}. {item}")
    print("=" * 68 + "\n")

    cfg["first_run_cli_shown_v1"] = True
    atomic_write_json(cfg_file, cfg, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install or register Tavern Fusion 3.0 locally")
    parser.add_argument("--workspace", default=None, help="Workspace directory to register .agents/plugins.json")
    parser.add_argument("--st-root", default=None, help="SillyTavern installation directory")
    parser.add_argument("--user-handle", default="default-user", help="SillyTavern user profile folder name")
    parser.add_argument(
        "--global-plugin",
        action="store_true",
        help="Deploy into ~/.gemini/config/plugins/tavern-fusion (with Tavern Stage Sidecar)",
    )
    args = parser.parse_args()

    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))

    mock_card = seed_mock_card_if_missing()
    print(f"[OK] Verified sample dual-chunk CCv3 card: {mock_card.name}")

    rem = remember_st_data_root(Path(args.st_root) if args.st_root else None, args.user_handle)
    if rem is not None:
        print(f"[OK] Recorded SillyTavern user profile in ~/.gemini/antigravity/tavern-fusion/state: {rem.name}")

    if args.st_root:
        ext_dir = install_st_web_extension(Path(args.st_root), args.user_handle)
        print(f"[OK] Installed pure client-side ST Web Sync extension: {ext_dir.name}")

    if args.global_plugin:
        g_plugin = install_global_plugin_and_sidecar()
        deduped = remove_duplicate_workspace_entry(PLUGIN_ROOT.parent)
        print(f"[OK] Deployed global plugin (with Tavern Stage sidecar): ~/.gemini/config/plugins/{g_plugin.name}")
        if deduped:
            print("[OK] Removed duplicate workspace .agents/plugins.json entry to prevent double Hook execution")
    else:
        ws_target = Path(args.workspace) if args.workspace else PLUGIN_ROOT.parent
        p_json = register_workspace_plugin(ws_target)
        print(f"[OK] Registered workspace plugin config: {p_json.name} (workspace: {ws_target.name})")

    emit_one_time_cli_notice()


if __name__ == "__main__":
    main()
