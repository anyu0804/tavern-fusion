#!/usr/bin/env python3
"""Characterization (golden snapshot) tests for the 14 MCP tools, sidecar ops and hooks.

Drives every public entry point against a temp copy of `examples/mock_st_data` and compares
the normalized JSON output with goldens in `tests/snapshots/`. The goldens were generated from
the pre-refactor code (v3.0.3 / PR #2 head), so any diff means observable behavior changed.

Run:      python tests/test_tool_snapshots.py
Refresh:  UPDATE_SNAPSHOTS=1 python tests/test_tool_snapshots.py   (only when a change is intended)

Normalization: temp paths -> <TMP>, chat timestamps / uuids / random hex -> placeholders,
wall-clock numeric fields (ts, mtime, *_at, ...) -> "<NUM>", data URIs -> length marker.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

SNAP_DIR = PLUGIN_ROOT / "tests" / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

# Keep the environment hermetic: no proxy, no policy override, no BYOK key, and a dead
# ST web URL so chat_sync's best-effort notify never reaches a real SillyTavern.
_HERMETIC_ENV = {
    "ST_WEB_URL": "http://127.0.0.1:9",
    "NO_PROXY": "127.0.0.1,localhost",
    "no_proxy": "127.0.0.1,localhost",
}
_DROP_ENV = (
    "TAVERN_POLICY_MODE",
    "TAVERN_BYOK_API_KEY",
    "ST_DATA_ROOT",
    "ST_USER_DATA_DIR",
    "PLUGIN_STATE_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)

VOLATILE_KEYS = frozenset({
    "ts",
    "mtime",
    "size",
    "updated_at",
    "timestamp",
    "last_bootstrapped_at",
    "last_committed_at",
    "first_run_ack_at",
    "create_date",
    "send_date",
    "gen_started",
    "gen_finished",
    "creation_date",
    "modification_date",
    "integrity",
    # Length of the widget HTML embeds the avatar data URI, whose PNG bytes depend on the
    # interpreter's zlib build (differs between CPython builds); the HTML itself is compared.
    "html_length",
})

_STR_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+"), "<DATA_URI>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}@\d{2}h\d{2}m\d{2}s(?:\d+ms)?"), "<STAMP>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"), "<DATETIME>"),
    (re.compile(r"\b\d{8}_\d{6}\b"), "<STAMP>"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<UUID>"),
    (re.compile(r"\b[0-9a-f]{32}\b"), "<HEX32>"),
    # create_new_chat_session only appends `_<hex4>` when a chat with the same second-resolution
    # stamp already exists, so the suffix depends on timing: drop it.
    (re.compile(r"(<STAMP>)_[0-9a-f]{4}(?![0-9A-Za-z])"), r"\1"),
    (re.compile(r"_[0-9a-f]{4}(?=\.jsonl\b)"), "_<HEX4>"),
    (re.compile(r"_[0-9a-f]{4}(?=[\"'\s`)]|$)"), "_<HEX4>"),
    (re.compile(r"\b1[7-9]\d{8}(?:\.\d+)?\b"), "<EPOCH>"),
]


_TMP_PATH_TAIL_RE = re.compile(r"<TMP>[^\r\n\"'`<>]*")


class Normalizer:
    def __init__(self, tmp_root: Path) -> None:
        roots = {str(tmp_root), str(tmp_root.resolve())}
        self._roots = sorted(roots, key=len, reverse=True)

    def text(self, s: str) -> str:
        for r in self._roots:
            s = s.replace(r, "<TMP>")
            s = s.replace(r.replace("\\", "\\\\"), "<TMP>")
        if os.name == "nt" and "<TMP>" in s:
            s = _TMP_PATH_TAIL_RE.sub(lambda m: re.sub(r"\\+(?![\"'])", "/", m.group(0)), s)
        for pat, repl in _STR_RULES:
            s = pat.sub(repl, s)
        return s

    def value(self, v: Any, key: str | None = None) -> Any:
        if key in VOLATILE_KEYS and v not in (None, "", 0, [], {}):
            return "<VOLATILE>"
        if isinstance(v, dict):
            return {k: self.value(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if isinstance(v, str):
            stripped = v.strip()
            if stripped[:1] in "{[" and stripped[-1:] in "}]":
                try:
                    inner = json.loads(v)
                except (ValueError, TypeError):
                    inner = None
                if isinstance(inner, (dict, list)):
                    return {"<json>": self.value(inner)}
            return self.text(v)
        return v


def _rpc(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return server.handle_rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })


def _sidecar(rt: Any, op: str, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": rt.handle_sidecar_op(op, args)}
    except Exception as exc:  # mirror `--sidecar-op` error envelope
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _read_jsonl(p: Path) -> list[Any]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


class SnapshotRecorder:
    def __init__(self, norm: Normalizer) -> None:
        self.norm = norm
        self.groups: dict[str, list[dict[str, Any]]] = {}

    def add(self, group: str, label: str, args: Any, result: Any) -> None:
        self.groups.setdefault(group, []).append({
            "call": label,
            "args": self.norm.value(args),
            "result": self.norm.value(result),
        })


class ToolSnapshotTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved_env = dict(os.environ)
        for k in _DROP_ENV:
            os.environ.pop(k, None)
        os.environ.update(_HERMETIC_ENV)

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.clear()
        os.environ.update(cls._saved_env)

    def _compare(self, name: str, payload: Any) -> None:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        path = SNAP_DIR / f"{name}.json"
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if UPDATE or not path.exists():
            if not UPDATE:
                self.fail(f"Missing golden {path.name}; run with UPDATE_SNAPSHOTS=1 to create it.")
            path.write_text(rendered, encoding="utf-8", newline="\n")
            return
        expected = path.read_text(encoding="utf-8")
        if expected != rendered:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(True), rendered.splitlines(True), f"golden/{path.name}", "actual", n=2
                )
            )
            self.fail(f"Snapshot mismatch for {path.name}:\n{diff[:12000]}")

    # ------------------------------------------------------------------
    def test_tools_and_sidecar_ops(self) -> None:
        from server.mcp_server import StdioMCPServer, TavernFusionRuntime

        with tempfile.TemporaryDirectory(prefix="tf_snap_") as td:
            tmp = Path(td)
            st_root = tmp / "data" / "default-user"
            shutil.copytree(PLUGIN_ROOT / "examples" / "mock_st_data" / "default-user", st_root)
            state_dir = tmp / "state"
            norm = Normalizer(tmp)
            rec = SnapshotRecorder(norm)
            old_cwd = Path.cwd()
            os.chdir(tmp)
            rt = TavernFusionRuntime(st_data_root=st_root, state_dir=state_dir)
            try:
                server = StdioMCPServer(rt)

                def call(name: str, args: dict[str, Any], label: str = "") -> dict[str, Any]:
                    resp = _rpc(server, name, args)
                    rec.add(name, label or name, args, resp)
                    return resp

                # protocol surface
                proto = {}
                for method, params in (
                    ("initialize", {"protocolVersion": "2025-03-26"}),
                    ("ping", {}),
                    ("tools/list", {}),
                    ("resources/list", {}),
                    ("prompts/list", {}),
                    ("no/such_method", {}),
                ):
                    proto[method] = server.handle_rpc({"jsonrpc": "2.0", "id": 7, "method": method, "params": params})
                proto["notification_returns"] = server.handle_rpc({"jsonrpc": "2.0", "method": "ping"})
                proto["unknown_tool"] = _rpc(server, "st_no_such_tool", {})
                self._compare("protocol", norm.value(proto))

                # cards & doctor
                call("st_read_card", {"card_filename": "Seraphina"})
                call("st_read_card", {"card_filename": "Seraphina.png", "include_avatar_base64": True}, "with_avatar")
                call("st_read_card", {"card_filename": "Nobody"}, "missing")
                call("st_read_card", {"card_filename": "../etc/passwd"}, "traversal")
                call("st_lint_card_and_lore", {"character_name": "Seraphina"})
                call("st_lint_card_and_lore", {"lorebook_name": "Eldoria"}, "lore_only")

                # session
                call("st_session_bootstrap", {"character_name": "Seraphina", "mode": "play"}, "first")
                call("st_session_bootstrap", {"character_name": "Seraphina", "mode": "--chat"}, "resume")
                call("st_session_bootstrap", {"character_name": "Seraphina", "new_chat": True, "greeting_index": 1}, "new_alt_greeting")
                call("st_session_bootstrap", {}, "missing_required")
                call("st_lint_card_and_lore", {}, "from_session")

                # prompt frame
                call("st_dump_prompt_frame", {"user_input": "星界裂隙还好吗？mana 是否稳定"}, "keyword_hit")
                call("st_dump_prompt_frame", {"user_input": "hello", "compact_agent_delta": True, "token_budget": 64}, "compact_small_budget")

                # commit turns
                call("st_commit_turn", {
                    "user_mes": "*走近星盘* 你好，{{char}}。",
                    "char_mes": "<think>internal</think>*她微微一笑* \"欢迎，{{user}}。\"{{setvar::mood::warm}}",
                    "variables_delta": {"affection": 1},
                }, "normal")
                call("st_commit_turn", {"char_mes": "*她转过身* \"雨还没停。\"", "swipe_mode": True}, "swipe")
                call("st_commit_turn", {"char_mes": "（场外：作为AI我不能）*她点头* \"好。\"", "extra_meta": {"is_backstage": True}}, "backstage_ooc")
                call("st_commit_turn", {"char_mes": "这里有强奸情节"}, "policy_block")
                call("st_commit_turn", {}, "missing_char_mes")

                # variables
                for a in (
                    {"op": "list"},
                    {"op": "get", "key": "affection"},
                    {"op": "set", "key": "hp", "value": 10},
                    {"op": "inc", "key": "hp", "value": 2.5},
                    {"op": "inc", "key": "hp"},
                    {"op": "inc", "key": "hp", "value": 0},
                    {"op": "del", "key": "hp"},
                    {"scope": "global", "op": "set", "key": "g1", "value": "x"},
                    {"scope": "global", "op": "inc", "key": "g2"},
                    {"scope": "global", "op": "get", "key": "g1"},
                    {"scope": "global", "op": "list"},
                ):
                    call("st_manage_variables", a, f"{a.get('scope', 'chat')}:{a['op']}")

                # profile
                call("st_switch_profile", {"mode": "chat", "epistemic_level": 55, "authors_note": "Keep it rainy.", "an_depth": 2}, "mode_epistemic_note")
                call("st_switch_profile", {"policy_mode": "strict_block", "agentic_hands": True}, "strict")
                call("st_commit_turn", {"char_mes": "这是 nsfw 段落"}, "strict_mature_block")
                call("st_switch_profile", {"policy_mode": "host_gentle", "agentic_hands": False, "epistemic_level": 0, "mode": "play"}, "back_to_gentle")
                call("st_commit_turn", {"char_mes": "这是 nsfw 段落"}, "gentle_mature_allowed")
                call("st_switch_profile", {"generation_backend": "byok", "byok_base_url": " https://api.example.com/v1 ", "byok_model": "m1"}, "byok_cfg")
                call("st_switch_profile", {"generation_backend": "host", "persona_name": "旅行者", "preset_name": "Default"}, "host_persona_preset")

                # lore
                call("st_query_lorebook", {"query_text": "星界裂隙 mana", "lorebook_names": ["Eldoria"]}, "named")
                call("st_query_lorebook", {"query_text": "钟塔", "lorebook_names": "Eldoria.json", "token_budget": 16}, "string_name_small_budget")
                call("st_query_lorebook", {"query_text": "nothing"}, "all_books")
                call("st_evolve_lore_entry", {"lorebook_name": "Eldoria", "entry_payload": {"comment": "新条目", "key": ["琥珀茶"], "content": "琥珀茶能缓和霜蓝结晶。"}}, "upsert_new")
                call("st_evolve_lore_entry", {"lorebook_name": "Eldoria", "operation": "toggle", "entry_payload": json.dumps({"uid": 1})}, "toggle_str_payload")
                call("st_evolve_lore_entry", {"lorebook_name": "Eldoria", "operation": "archive", "entry_payload": {"uid": 0}}, "archive")
                call("st_evolve_lore_entry", {"lorebook_name": "Fresh Book", "entry_payload": {"comment": "c", "key": ["k"], "content": "v"}}, "new_book")

                # stage floor ops
                call("st_stage_floor_op", {"operation": "edit_floor", "floor_index": -1, "new_text": "*编辑后的回复*"}, "edit_last")
                call("st_stage_floor_op", {"operation": "switch_swipe", "floor_index": -1, "swipe_id": 0}, "switch_swipe")
                call("st_stage_floor_op", {"operation": "append_user_floor", "new_text": "我先四处看看。"}, "append_user")
                call("st_stage_floor_op", {"operation": "toggle_backstage", "floor_index": -1}, "toggle_backstage")
                call("st_stage_floor_op", {"operation": "delete_floor", "floor_index": -1}, "delete_last")
                call("st_stage_floor_op", {"operation": "prune_swipes", "floor_index": -1}, "prune")
                call("st_stage_floor_op", {"operation": "edit_floor", "chat_file": "../x.jsonl", "new_text": "x"}, "bad_chat_file")
                call("st_stage_floor_op", {"operation": "fork_from_floor", "floor_index": 1, "activate_fork": False}, "fork_inactive")

                # chat branches
                call("st_manage_chat_branch", {"character_name": "Seraphina", "action": "switch_swipe", "target_id": 0}, "switch_swipe")
                call("st_manage_chat_branch", {"character_name": "Seraphina", "action": "fork_checkpoint"}, "fork_checkpoint")
                call("st_manage_chat_branch", {"character_name": "Seraphina", "action": "rollback", "target_id": 1}, "rollback")
                call("st_manage_chat_branch", {"character_name": "Seraphina", "action": "new", "target_id": 0}, "new")
                call("st_manage_chat_branch", {"character_name": "Seraphina", "action": "bogus"}, "bogus_action")

                # suggestions
                call("st_publish_stage_suggestions", {"suggestions": [" a ", "", "b", "c", "d", "e", "f"]}, "list_trim")
                call("st_publish_stage_suggestions", {"suggestions": "only one", "kind": "expanded_intent"}, "string")

                # widget
                call("st_compile_stage_widget", {"placement": "inline"}, "inline")
                call("st_compile_stage_widget", {"placement": "standalone", "output_html_path": "snap.html"}, "to_file")
                call("st_compile_stage_widget", {"output_html_path": "bad.txt"}, "bad_suffix")

                # card export
                card = {
                    "spec": "chara_card_v3",
                    "spec_version": "3.0",
                    "data": {
                        "name": "Snapshot Bard",
                        "description": "A wandering adult bard, 29 years old.",
                        "personality": "cheerful",
                        "scenario": "A tavern.",
                        "first_mes": "\"Hello, {{user}}!\"",
                        "mes_example": "",
                        "tags": ["music"],
                        "extensions": {"world": "", "regex_scripts": []},
                    },
                }
                call("st_build_export_card", {"card_v3_json": card, "output_filename": "Snapshot Bard.png"}, "export")
                call("st_build_export_card", {"card_v3_json": json.dumps(card), "output_filename": "../../evil"}, "export_str_sanitized")
                minor = json.loads(json.dumps(card))
                minor["data"]["name"] = "Kid"
                minor["data"]["description"] = "A 12-year-old child, explicit sexual content."
                minor["data"]["tags"] = ["loli", "nsfw"]
                call("st_build_export_card", {"card_v3_json": minor, "output_filename": "kid"}, "minor_blocked")
                call("st_build_export_card", {"card_v3_json": card, "output_filename": "p", "portrait_image_path": "/etc/hosts.png"}, "portrait_outside")

                # sidecar ops (Tavern Stage control plane)
                sidecar: dict[str, list[Any]] = {}

                def op(name: str, args: dict[str, Any], label: str = "") -> dict[str, Any]:
                    res = _sidecar(rt, name, args)
                    sidecar.setdefault(name, []).append({
                        "call": label or name,
                        "args": norm.value(args),
                        "result": norm.value(res),
                    })
                    return res

                op("snapshot", {})
                op("read_lorebook", {})
                op("read_lorebook", {"lorebook_name": "Eldoria"}, "named")
                op("bind_rp_conv", {"rp_conv_id": "conv-snap"})
                op("byok_status", {})
                op("prepare_agent_turn", {"action_kind": "send_turn", "user_text": "你好"}, "send_turn")
                op("prepare_agent_turn", {"action_kind": "suggest_replies"}, "suggest_replies")
                op("prepare_agent_turn", {"action_kind": "swipe_reroll", "user_text": "更温柔"}, "swipe_reroll")
                op("prepare_agent_turn", {"action_kind": "kartoittaja_branches"}, "kartoittaja")
                op("prepare_agent_turn", {"action_kind": "expand_intent", "user_text": "拥抱"}, "expand_intent")
                op("prepare_agent_turn", {"action_kind": "send_turn", "user_text": "强奸"}, "policy_block")
                op("prepare_agent_turn", {"action_kind": "nope", "user_text": "x"}, "unknown_kind")
                op("byok_turn", {"action_kind": "send_turn", "user_text": "hi"}, "byok_unconfigured")
                op("byok_turn", {"action_kind": "kartoittaja_branches"}, "byok_unsupported")
                op("query_lore", {"query_text": "钟塔"})
                op("lint", {"character_name": "Seraphina"})
                op("manage_variables", {"op": "list"})
                op("switch_profile", {"director_mode": True})
                op("prepare_agent_turn", {"action_kind": "send_turn", "user_text": "导演：加快节奏"}, "director_send")
                op("switch_profile", {"director_mode": False}, "director_off")
                op("floor_op", {"operation": "edit_floor", "floor_index": 0, "new_text": "*改写开场*"})
                op("evolve_lore", {"lorebook_name": "Eldoria", "operation": "delete", "entry_payload": {"uid": 0}})
                op("clear_suggestions", {})
                op("ack_first_run_notice", {})
                op("ack_minor_risk_warn", {"card_file": "Seraphina.png"})
                op("ack_minor_risk_warn", {}, "missing_card")
                imported = dict(card)
                imported["data"] = dict(card["data"], name="Imported Sage")
                b64 = base64.b64encode(json.dumps(imported).encode("utf-8")).decode("ascii")
                op("import_card", {"filename": "Imported Sage.json", "base64_data": "data:application/json;base64," + b64}, "json")
                op("import_card", {}, "missing_data")
                op("bootstrap", {"character_name": "Seraphina", "activate_rp": False}, "bootstrap_inactive")
                op("snapshot", {}, "after_ops")
                op("no_such_op", {})

                # final on-disk state
                def tree(root: Path) -> list[str]:
                    return sorted(norm.text(str(p.relative_to(tmp)).replace("\\", "/")) for p in root.rglob("*") if p.is_file())

                chats_dir = st_root / "chats"
                chat_contents = {}
                if chats_dir.exists():
                    for p in sorted(chats_dir.rglob("*.jsonl")):
                        chat_contents.setdefault(norm.text(p.name), []).append(norm.value(_read_jsonl(p)))
                final_state = {
                    "state_files": tree(state_dir),
                    "data_files": tree(st_root),
                    "active_session": norm.value(json.loads((state_dir / "active_session.json").read_text(encoding="utf-8"))),
                    "plugin_settings": norm.value(json.loads((state_dir / "plugin_settings.json").read_text(encoding="utf-8"))),
                    "settings_json_ext": norm.value(
                        json.loads((st_root / "settings.json").read_text(encoding="utf-8")).get("extension_settings")
                    ),
                    "eldoria": norm.value(json.loads((st_root / "worlds" / "Eldoria.json").read_text(encoding="utf-8"))),
                    "chat_count": sum(len(v) for v in chat_contents.values()),
                    "chat_floor_counts": sorted(len(c) for v in chat_contents.values() for c in v),
                }
            finally:
                rt.close()
                os.chdir(old_cwd)

            for group, entries in rec.groups.items():
                self._compare(f"tool_{group}", entries)
            self._compare("sidecar_ops", sidecar)
            self._compare("final_state", final_state)

    # ------------------------------------------------------------------
    def test_cli_entry_points(self) -> None:
        """`--cli` and `--sidecar-op` modes of server/mcp_server.py as hooks/sidecar invoke them."""
        with tempfile.TemporaryDirectory(prefix="tf_snap_cli_") as td:
            tmp = Path(td)
            st_root = tmp / "data" / "default-user"
            shutil.copytree(PLUGIN_ROOT / "examples" / "mock_st_data" / "default-user", st_root)
            state_dir = tmp / "state"
            norm = Normalizer(tmp)
            env = dict(os.environ)
            out: dict[str, Any] = {}
            for label, extra in (
                ("cli_read_card", ["--cli", "st_read_card", "--args", json.dumps({"card_filename": "Seraphina"})]),
                ("cli_unknown", ["--cli", "st_nope", "--args", "{}"]),
                ("sidecar_bootstrap", ["--sidecar-op", "bootstrap", "--args", json.dumps({"character_name": "Seraphina"})]),
                ("sidecar_bad", ["--sidecar-op", "zzz", "--args", "{}"]),
            ):
                proc = subprocess.run(
                    [sys.executable, str(PLUGIN_ROOT / "server" / "mcp_server.py"), "--data-root", str(st_root), "--state-dir", str(state_dir), *extra],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=env,
                    timeout=60,
                )
                out[label] = {"rc": proc.returncode, "stdout": norm.value(json.loads(proc.stdout))}
            # stdin args variant
            proc = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "server" / "mcp_server.py"), "--data-root", str(st_root), "--state-dir", str(state_dir), "--sidecar-op", "byok_status", "--args", "-"],
                input="{}",
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=60,
            )
            out["sidecar_stdin_args"] = {"rc": proc.returncode, "stdout": norm.value(json.loads(proc.stdout))}
            self._compare("cli_entry_points", out)

    # ------------------------------------------------------------------
    def test_hooks(self) -> None:
        """Feeds each hook the same kind of stdin payloads the host sends and snapshots stdout + session state."""
        with tempfile.TemporaryDirectory(prefix="tf_snap_hooks_") as td:
            tmp = Path(td)
            ws = tmp / "ws"
            st_root = ws / "data" / "default-user"
            shutil.copytree(PLUGIN_ROOT / "examples" / "mock_st_data" / "default-user", st_root)
            state_dir = tmp / "state"
            norm = Normalizer(tmp)
            env = dict(os.environ)
            env["PLUGIN_STATE_DIR"] = str(state_dir)
            env["HOME"] = str(tmp / "home")
            env["USERPROFILE"] = str(tmp / "home")
            transcript = tmp / "transcript.jsonl"
            results: list[dict[str, Any]] = []

            def hook(script: str, payload: dict[str, Any], label: str) -> None:
                proc = subprocess.run(
                    [sys.executable, str(PLUGIN_ROOT / "hooks" / script)],
                    input=json.dumps(payload, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=env,
                    cwd=str(tmp),
                    timeout=60,
                )
                sess_file = state_dir / "active_session.json"
                sess = json.loads(sess_file.read_text(encoding="utf-8")) if sess_file.exists() else None
                try:
                    stdout: Any = json.loads(proc.stdout)
                except ValueError:
                    stdout = proc.stdout
                results.append({
                    "hook": script,
                    "call": label,
                    "rc": proc.returncode,
                    "stdout": norm.value(stdout),
                    "stderr_tail": norm.text(proc.stderr.strip().splitlines()[-1]) if proc.returncode else "",
                    "session_after": norm.value(sess),
                })

            base = {"workspacePaths": [str(ws)]}
            hook("session_init.py", {"workspacePaths": [str(tmp / "nowhere")]}, "non_st_workspace")
            hook("session_init.py", {**base, "conversationId": "c1"}, "st_workspace")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "fix my code", "invocationNum": 0}, "non_rp")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "/rp Seraphina --play", "invocationNum": 0}, "rp_bootstrap")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "x", "invocationNum": 3}, "continuation_skip")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "*点头* 我们去钟塔吧", "invocationNum": 0}, "rp_turn")
            hook("pre_tool_guard.py", {**base, "conversationId": "c1", "toolCall": {"name": "mcp_st-mcp_st_manage_variables", "args": {}}}, "pure_theater_deny")
            hook("pre_tool_guard.py", {**base, "conversationId": "c2", "toolCall": {"name": "write_file", "args": {"Path": "C:\\st\\chats\\a\\b.jsonl"}}}, "jsonl_write_deny")
            hook("pre_tool_guard.py", {**base, "conversationId": "c2", "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/x/characters/a.png"}}}, "png_write_deny")
            hook("pre_tool_guard.py", {**base, "conversationId": "c2", "toolCall": {"name": "st_read_card", "args": {"card_filename": "../a"}}}, "traversal_deny")
            hook("pre_tool_guard.py", {**base, "conversationId": "c2", "toolCall": {"name": "st_commit_turn", "args": {"char_mes": "强奸"}}}, "policy_deny")
            hook("pre_tool_guard.py", {**base, "conversationId": "c2", "toolCall": {"name": "st_read_card", "args": {"card_filename": "Seraphina"}}}, "st_allow")
            hook("pre_tool_guard.py", {**base, "toolCall": {"name": "list_dir", "args": {}}}, "other_allow")
            transcript.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in (
                    {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "*点头* 我们去钟塔吧"},
                    {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "I'll call a tool", "tool_calls": [{"name": "x"}]},
                    {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "*她提起灯* \"走吧。\""},
                )) + "\n",
                encoding="utf-8",
            )
            hook("stop_persistence_check.py", {**base, "conversationId": "c2", "terminationReason": "NO_TOOL_CALL"}, "other_conv_allow")
            hook("stop_persistence_check.py", {**base, "conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(transcript)}, "persist_turn")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "这段要 nsfw", "invocationNum": 0}, "gentle_notice_turn")
            hook("stop_persistence_check.py", {**base, "conversationId": "c1", "terminationReason": "CANCELLED"}, "abnormal_stop")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "强奸她", "invocationNum": 0}, "policy_block")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "我们继续", "invocationNum": 0}, "turn_no_prose")
            hook("stop_persistence_check.py", {**base, "conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tmp / "missing.jsonl")}, "missing_prose")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "/dump-prompt", "invocationNum": 0}, "utility_in_rp")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c9", "lastUserInput": "/card-forge", "invocationNum": 0}, "utility_outside_rp")
            hook("pre_invocation_gate.py", {**base, "conversationId": "c1", "lastUserInput": "/rp off", "invocationNum": 0}, "rp_off")

            chats = sorted((st_root / "chats").rglob("*.jsonl"))
            results.append({
                "final_chats": [
                    {"file": norm.text(p.name), "floors": norm.value(_read_jsonl(p))} for p in chats
                ]
            })
            self._compare("hooks", results)


if __name__ == "__main__":
    unittest.main(verbosity=2)
