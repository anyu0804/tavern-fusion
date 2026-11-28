#!/usr/bin/env python3
"""Targeted regression tests for backend-mcp P0/P1/P2/OPT fixes and cross-layer contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from scripts import install_local
from server.byok_backend import _build_request, read_byok_api_key
from server.card_codec import (
    detect_message_emotion,
    read_character_card,
    write_character_card_png,
)
from server.chat_sync import read_jsonl_chat
from server.fs_safety import sanitize_filename_stem
from server.lore_engine import (
    HybridLoreEngine,
    NormalizedLoreEntry,
    evolve_lorebook_file,
    lint_lorebook_file,
    load_lorebook_file,
)
from server.mcp_server import TavernFusionRuntime
from server.prompt import (
    compile_st_prompt_frame,
    load_openai_preset,
    load_st_settings,
    resolve_st_macros,
)


def _copy_sandbox_st_data(base_dir: Path) -> Path:
    st_root = base_dir / "data" / "default-user"
    shutil.copytree(PLUGIN_ROOT / "examples" / "mock_st_data" / "default-user", st_root)
    return st_root


class BackendRegressionTests(unittest.TestCase):
    def test_01_path_containment_and_pre_tool_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tf_reg_p0_path_") as td:
            root = Path(td)
            st_root = _copy_sandbox_st_data(root)
            state_dir = root / "state"

            secret_book = root / "outside_secret.json"
            secret_book.write_text(
                json.dumps({
                    "entries": {
                        "0": {
                            "uid": 0,
                            "key": ["leak"],
                            "content": "TOP_SECRET_OUTSIDE_WORLD_CONTENT",
                            "constant": True,
                        }
                    }
                }),
                encoding="utf-8",
            )

            # 1. Untrusted `extensions.world` (both `..` traversal and absolute path) must be ignored
            rt = TavernFusionRuntime(st_data_root=st_root, state_dir=state_dir)
            try:
                for bad_world in ("../../outside_secret", str(secret_book)):
                    write_character_card_png(
                        {
                            "spec": "chara_card_v3",
                            "spec_version": "3.0",
                            "data": {
                                "name": "UntrustedCard",
                                "description": "Adult mage, 25 years old.",
                                "personality": "calm",
                                "scenario": "tower",
                                "first_mes": "Greetings.",
                                "extensions": {"world": bad_world},
                            },
                        },
                        st_root / "characters" / "UntrustedCard.png",
                    )
                    frame = rt.tool_st_dump_prompt_frame({"character_name": "UntrustedCard.png", "user_input": "leak"})
                    self.assertNotIn("TOP_SECRET_OUTSIDE_WORLD_CONTENT", frame["ephemeral_frame"])

                # 2. Explicit `lorebooks` and `preset_name` traversal must raise ValueError
                with self.assertRaises(ValueError):
                    rt.tool_st_session_bootstrap({
                        "character_name": "Seraphina",
                        "lorebooks": ["../../outside_secret"],
                    })
                with self.assertRaises(ValueError):
                    load_openai_preset(st_root, "../../outside_secret")

                # 3. Ellipsis in character stems is legal, while `..` path segments are rejected
                self.assertEqual(sanitize_filename_stem("少女...祈祷中"), "少女...祈祷中")
                with self.assertRaises(ValueError):
                    sanitize_filename_stem("../../outside")
            finally:
                rt.close()

            # 4. PreToolUse guard blocks list/preset traversals, relative chat paths, trailing dots & state tampering
            hook_env = dict(os.environ, PLUGIN_STATE_DIR=str(state_dir), ST_DATA_ROOT=str(st_root))
            deny_cases = [
                {"toolCall": {"name": "st_session_bootstrap", "args": {"character_name": "Seraphina", "lorebooks": ["../secret"]}}},
                {"toolCall": {"name": "st_query_lorebook", "args": {"query_text": "x", "lorebook_names": ["../secret"]}}},
                {"toolCall": {"name": "st_switch_profile", "args": {"preset_name": "../secret"}}},
                {"toolCall": {"name": "write_file", "args": {"Path": "chats/Seraphina/2026.jsonl"}}},
                {"toolCall": {"name": "write_file", "args": {"Path": "C:\\ST\\characters\\Hero.png."}}},
                {"toolCall": {"name": "edit_file", "args": {"Path": "/tmp/tavern-fusion/state/plugin_settings.json"}}},
            ]
            for payload in deny_cases:
                proc = subprocess.run(
                    [sys.executable, str(PLUGIN_ROOT / "hooks" / "pre_tool_guard.py")],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=hook_env,
                    check=True,
                )
                decision = json.loads(proc.stdout)
                self.assertEqual(decision["decision"], "deny", f"Expected deny for {payload}")

            # Ellipsis card filename without traversal must be allowed
            allow_proc = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "hooks" / "pre_tool_guard.py")],
                input=json.dumps({"toolCall": {"name": "st_read_card", "args": {"card_filename": "少女...祈祷中.png"}}}),
                text=True,
                capture_output=True,
                env=hook_env,
                check=True,
            )
            self.assertEqual(json.loads(allow_proc.stdout)["decision"], "allow")

    def test_02_run_py_plugin_root_priority_and_hooks_json(self) -> None:
        hooks_cfg = json.loads((PLUGIN_ROOT / "hooks.json").read_text(encoding="utf-8"))
        cmds = [
            hooks_cfg["st-session-init"]["SessionStart"][0]["command"],
            hooks_cfg["st-pre-invocation-interceptor"]["PreInvocation"][0]["command"],
            hooks_cfg["st-pre-tool-chokepoint"]["PreToolUse"][0]["hooks"][0]["command"],
            hooks_cfg["st-stop-sync-guarantor"]["Stop"][0]["command"],
        ]
        for cmd in cmds:
            self.assertNotIn("${PLUGIN_ROOT}", cmd)
            self.assertIn("./scripts/run_py.mjs", cmd)
            self.assertIn("./hooks/", cmd)

        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "Node.js is required to verify run_py.mjs")
        with tempfile.TemporaryDirectory(prefix="tf_reg_untrusted_ws_") as td:
            evil_ws = Path(td)
            (evil_ws / "hooks").mkdir(parents=True, exist_ok=True)
            (evil_ws / "hooks" / "session_init.py").write_text(
                'import sys; sys.stdout.write(\'{"hijacked": true}\\n\')\n',
                encoding="utf-8",
            )
            proc = subprocess.run(
                [str(node_bin), str(PLUGIN_ROOT / "scripts" / "run_py.mjs"), "./hooks/session_init.py"],
                input=json.dumps({"workspacePaths": [str(evil_ws)]}),
                text=True,
                capture_output=True,
                cwd=str(evil_ws),
                timeout=20,
                check=True,
            )
            parsed = json.loads(proc.stdout)
            self.assertNotIn("hijacked", parsed)
            self.assertEqual(parsed, {"injectSteps": []})

    def test_03_byok_sse_premature_eof_hook_isolation_and_cancel_turn(self) -> None:
        server_state: dict[str, object] = {
            "mode": "premature_eof",
            "chunk1_sent": threading.Event(),
            "allow_chunk2": threading.Event(),
        }

        class _SSEHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                _ = self.rfile.read(length)
                mode = server_state["mode"]
                self.send_response(200)
                self.send_header("Content-Type", "TEXT/EVENT-STREAM; charset=utf-8")
                self.end_headers()
                c1 = {"choices": [{"delta": {"content": "*半截星光闪烁*"}}]}
                self.wfile.write(f"data: {json.dumps(c1, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                if mode == "premature_eof":
                    # Close connection without finish_reason and without [DONE]
                    return
                if mode == "slow_cancellable":
                    ev_sent = server_state["chunk1_sent"]
                    ev_go = server_state["allow_chunk2"]
                    assert isinstance(ev_sent, threading.Event) and isinstance(ev_go, threading.Event)
                    ev_sent.set()
                    ev_go.wait(timeout=5.0)
                    c2 = {"choices": [{"delta": {"content": " \"继续低语。\""}, "finish_reason": "stop"}]}
                    self.wfile.write(f"data: {json.dumps(c2, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
                    self.wfile.flush()

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        srv = HTTPServer(("127.0.0.1", 0), _SSEHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{srv.server_port}/v1"
        saved_env = {k: os.environ.pop(k, None) for k in ("TAVERN_BYOK_API_KEY", "TAVERN_BYOK_BASE_URL", "TAVERN_BYOK_MODEL")}
        os.environ["TAVERN_BYOK_API_KEY"] = "sk-reg-test-key"
        try:
            with tempfile.TemporaryDirectory(prefix="tf_reg_byok_") as td:
                root = Path(td)
                st_root = _copy_sandbox_st_data(root)
                state_dir = root / "state"
                rt = TavernFusionRuntime(st_data_root=st_root, state_dir=state_dir)
                try:
                    rt.tool_st_session_bootstrap({
                        "character_name": "Seraphina",
                        "mode": "play",
                        "rp_conv_id": "conv-bound-1",
                        "activate_rp": True,
                    })
                    rt.handle_sidecar_op("switch_profile", {
                        "generation_backend": "byok",
                        "byok_base_url": base_url,
                        "byok_model": "mock-sse",
                        "byok_stream": True,
                    })
                    chat_path = Path(rt.load_active_session()["chat_path"])
                    _, before_msgs = read_jsonl_chat(chat_path)

                    # 1. Premature EOF must abort and roll back the pending user floor
                    with self.assertRaises(ValueError) as eof_cm:
                        rt.handle_sidecar_op("byok_turn", {"action_kind": "send_turn", "user_text": "断连测试台词"})
                    self.assertIn("未正常结束", str(eof_cm.exception))
                    _, after_eof_msgs = read_jsonl_chat(chat_path)
                    self.assertEqual(len(after_eof_msgs), len(before_msgs))
                    self.assertFalse((state_dir / "byok_stream_preview.json").exists())
                    self.assertFalse(rt.load_active_session().get("stage_busy"))

                    # 2. In-flight stream: host PreToolUse is NOT blocked, and `cancel_turn` aborts cleanly
                    server_state["mode"] = "slow_cancellable"
                    err_box: list[BaseException] = []

                    def _bg_turn() -> None:
                        try:
                            rt.handle_sidecar_op("byok_turn", {"action_kind": "send_turn", "user_text": "取消测试台词"})
                        except BaseException as exc:
                            err_box.append(exc)

                    bg = threading.Thread(target=_bg_turn)
                    bg.start()
                    ev_sent = server_state["chunk1_sent"]
                    ev_go = server_state["allow_chunk2"]
                    assert isinstance(ev_sent, threading.Event) and isinstance(ev_go, threading.Event)
                    self.assertTrue(ev_sent.wait(timeout=5.0))

                    # Host PreToolUse in the bound conversation must NOT be locked out by BYOK
                    mid_sess = rt.load_active_session()
                    self.assertFalse(mid_sess.get("pending_uncommitted_turn"))
                    hook_env = dict(os.environ, PLUGIN_STATE_DIR=str(state_dir), ST_DATA_ROOT=str(st_root))
                    guard_proc = subprocess.run(
                        [sys.executable, str(PLUGIN_ROOT / "hooks" / "pre_tool_guard.py")],
                        input=json.dumps({
                            "conversationId": "conv-bound-1",
                            "workspacePaths": [str(root)],
                            "toolCall": {"name": "read_file", "args": {"Path": "README.md"}},
                        }),
                        text=True,
                        capture_output=True,
                        env=hook_env,
                        check=True,
                    )
                    self.assertEqual(json.loads(guard_proc.stdout)["decision"], "allow")

                    # Issue `cancel_turn` while stream is active, then release chunk 2
                    cancel_res = rt.handle_sidecar_op("cancel_turn", {})
                    self.assertEqual(cancel_res, {"cancelled": True, "force_reset": False})
                    ev_go.set()
                    bg.join(timeout=5.0)
                    self.assertFalse(bg.is_alive())
                    self.assertEqual(len(err_box), 1)
                    self.assertIn("已取消", str(err_box[0]))
                    self.assertFalse((state_dir / "byok_stream_preview.json").exists())
                    self.assertFalse((state_dir / "byok_cancel.flag").exists())
                    self.assertFalse(rt.load_active_session().get("stage_busy"))
                    _, after_cancel_msgs = read_jsonl_chat(chat_path)
                    self.assertEqual(len(after_cancel_msgs), len(before_msgs))

                    # 3. `cancel_turn(force_reset=True)` clears stuck busy flags when no stream is active
                    rt.update_active_session({"stage_busy": True, "pending_uncommitted_turn": True})
                    f_res = rt.handle_sidecar_op("cancel_turn", {"force_reset": True})
                    self.assertEqual(f_res, {"cancelled": True, "force_reset": True})
                    reset_sess = rt.load_active_session()
                    self.assertFalse(reset_sess.get("stage_busy"))
                    self.assertFalse(reset_sess.get("pending_uncommitted_turn"))
                    self.assertFalse((state_dir / "byok_cancel.flag").exists())
                finally:
                    rt.close()
        finally:
            srv.shutdown()
            srv.server_close()
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_04_utf8_bom_and_install_local_sync(self) -> None:
        saved_key = os.environ.pop("TAVERN_BYOK_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory(prefix="tf_reg_bom_") as td:
                root = Path(td)
                st_root = _copy_sandbox_st_data(root)
                state_dir = root / "state"
                state_dir.mkdir(parents=True, exist_ok=True)

                # 1. UTF-8 BOM in byok_api_key must not leak U+FEFF into Authorization header
                (state_dir / "byok_api_key").write_bytes("\ufeffsk-bom-clean-key\r\n".encode("utf-8"))
                key = read_byok_api_key(state_dir)
                self.assertEqual(key, "sk-bom-clean-key")
                req = _build_request("https://api.example.com/v1", key, "m1", [{"role": "user", "content": "hi"}], 0.7, None, False)
                self.assertEqual(req.get_header("Authorization"), "Bearer sk-bom-clean-key")

                # 2. UTF-8 BOM in .jsonl, worlds/*.json, OpenAI Settings/*.json, settings.json, and .json card
                bom_chat = st_root / "chats" / "Seraphina" / "bom_chat.jsonl"
                bom_chat.parent.mkdir(parents=True, exist_ok=True)
                bom_chat.write_bytes(
                    "\ufeff{\"user_name\":\"旅行者\",\"character_name\":\"Seraphina\",\"chat_metadata\":{}}\r\n"
                    "{\"name\":\"Seraphina\",\"is_user\":false,\"mes\":\"你好。\"}\r\n".encode("utf-8")
                )
                hdr, msgs = read_jsonl_chat(bom_chat)
                self.assertEqual(hdr["character_name"], "Seraphina")
                self.assertEqual(len(msgs), 1)

                bom_world = st_root / "worlds" / "BomWorld.json"
                bom_world.write_bytes(
                    "\ufeff{\"entries\":{\"0\":{\"uid\":\"str_id\",\"enabled\":false,\"key\":[\"k\"],\"content\":\"c\"}}}".encode("utf-8")
                )
                self.assertEqual(len(load_lorebook_file(bom_world)), 1)
                lint_issues = lint_lorebook_file(bom_world)
                self.assertTrue(any(i["code"] == "INVALID_UID" for i in lint_issues))
                self.assertFalse(any(i["code"] == "UNREACHABLE_ENTRY" for i in lint_issues))

                (st_root / "OpenAI Settings" / "BomPreset.json").write_bytes(
                    "\ufeff{\"temperature\":0.66,\"prompts\":[]}".encode("utf-8")
                )
                self.assertAlmostEqual(load_openai_preset(st_root, "BomPreset")["temperature"], 0.66)

                (st_root / "settings.json").write_bytes(
                    "\ufeff{\"username\":\"BOM旅人\"}".encode("utf-8")
                )
                self.assertEqual(load_st_settings(st_root)["username"], "BOM旅人")

                bom_card = st_root / "characters" / "BomCard.json"
                bom_card.write_bytes(
                    "\ufeff{\"name\":\"BomHero\",\"description\":\"Adult knight, 28 years old.\",\"first_mes\":\"Hi\"}".encode("utf-8")
                )
                self.assertEqual(read_character_card(bom_card)["card"]["data"]["name"], "BomHero")

                # 3. install_local mirror-prunes deleted files, __pycache__, and .venv, and persists notice version
                target_plugin = root / "installed_plugin"
                (target_plugin / "server" / "__pycache__").mkdir(parents=True, exist_ok=True)
                (target_plugin / "server" / "deleted_legacy_module.py").write_text("# stale", encoding="utf-8")
                (target_plugin / "server" / "__pycache__" / "stale.cpython-313.pyc").write_bytes(b"0")
                (target_plugin / ".venv" / "Scripts").mkdir(parents=True, exist_ok=True)
                (target_plugin / ".venv" / "Scripts" / "python.exe").write_bytes(b"0")
                install_local.install_global_plugin_and_sidecar(target_dir=target_plugin)
                self.assertTrue((target_plugin / "server" / "mcp_server.py").is_file())
                self.assertFalse((target_plugin / "server" / "deleted_legacy_module.py").exists())
                self.assertFalse((target_plugin / "server" / "__pycache__").exists())
                self.assertFalse((target_plugin / ".venv").exists())
                self.assertFalse((target_plugin / "tests").exists())

                install_local.emit_one_time_cli_notice(state_dir=state_dir)
                rt = TavernFusionRuntime(st_data_root=st_root, state_dir=state_dir)
                try:
                    updated_cfg = rt.save_plugin_settings({"policy_mode": "host_gentle"})
                    self.assertTrue(updated_cfg["first_run_cli_shown_v1"])
                    self.assertGreaterEqual(updated_cfg["first_run_cli_shown_version"], 2)
                finally:
                    rt.close()
        finally:
            if saved_key is not None:
                os.environ["TAVERN_BYOK_API_KEY"] = saved_key

    def test_05_bootstrap_preservation_pre_inv_errors_and_swipe_semantics(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tf_reg_sess_") as td:
            root = Path(td)
            st_root = _copy_sandbox_st_data(root)
            state_dir = root / "state"
            rt = TavernFusionRuntime(st_data_root=st_root, state_dir=state_dir)
            try:
                # 1. `st_session_bootstrap` preserves bound `rp_conv_id` and Dimension Mixer switches
                rt.tool_st_session_bootstrap({
                    "character_name": "Seraphina",
                    "mode": "play",
                    "rp_conv_id": "conv-keep",
                    "activate_rp": True,
                })
                rt.tool_st_switch_profile({"epistemic_level": 50, "agentic_hands": True})
                rt.tool_st_session_bootstrap({"character_name": "Seraphina", "new_chat": True})
                sess = rt.load_active_session()
                self.assertEqual(sess["rp_conv_id"], "conv-keep")
                self.assertEqual(sess["epistemic_level"], 50)
                self.assertTrue(sess["agentic_hands"])

                # 2. Failed `/rp <MissingCard>` in PreInvocation terminates cleanly instead of polluting active chat
                hook_env = dict(os.environ, PLUGIN_STATE_DIR=str(state_dir), ST_DATA_ROOT=str(st_root))
                pre_proc = subprocess.run(
                    [sys.executable, str(PLUGIN_ROOT / "hooks" / "pre_invocation_gate.py")],
                    input=json.dumps({
                        "conversationId": "conv-keep",
                        "workspacePaths": [str(root)],
                        "lastUserInput": "/rp MissingCardDoesNotExist",
                        "invocationNum": 0,
                    }),
                    text=True,
                    capture_output=True,
                    env=hook_env,
                    check=True,
                )
                pre_out = json.loads(pre_proc.stdout)
                self.assertEqual(pre_out.get("terminationBehavior"), "terminate")
                self.assertIn("MissingCardDoesNotExist", pre_out["injectSteps"][0]["errorMessage"]["userMessage"])

                # 3. `st_dump_prompt_frame` & `st_commit_turn` enforce Minor Risk Gate even on direct calls
                blocked_json = st_root / "characters" / "BlockedMinor.json"
                blocked_json.write_text(
                    json.dumps({
                        "name": "BlockedMinor",
                        "description": "12 years old loli character.",
                        "tags": ["loli"],
                        "first_mes": "Hi",
                    }),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    rt.tool_st_dump_prompt_frame({"character_name": "BlockedMinor.json"})
                with self.assertRaises(ValueError):
                    rt.tool_st_commit_turn({"character_name": "BlockedMinor.json", "char_mes": "Hi"})

                # 4. Swipe reroll strips the old reply from SCENE HISTORY and swipe commit preserves timed_effects
                rt.tool_st_commit_turn({
                    "user_mes": "星界裂隙的秘银辉光似乎过载了。",
                    "char_mes": "*旧的第一版回复：她轻轻蹙眉。*",
                })
                chat_p = Path(rt.load_active_session()["chat_path"])
                hdr_before_swipe, _ = read_jsonl_chat(chat_p)
                sticky_before = dict(hdr_before_swipe["chat_metadata"]["timed_effects"]["sticky"])
                self.assertTrue(sticky_before)

                reroll_frame = rt.tool_st_dump_prompt_frame({
                    "character_name": "Seraphina",
                    "user_input": "",
                    "_swipe_reroll": True,
                })
                self.assertNotIn("旧的第一版回复", reroll_frame["ephemeral_frame"])
                self.assertIn("星界裂隙的秘银辉光似乎过载了", reroll_frame["ephemeral_frame"])

                rt.tool_st_commit_turn({
                    "char_mes": "*新的重抽平行分支：她递来琥珀茶。*",
                    "swipe_mode": True,
                })
                hdr_after_swipe, _ = read_jsonl_chat(chat_p)
                self.assertEqual(hdr_after_swipe["chat_metadata"]["timed_effects"]["sticky"], sticky_before)
            finally:
                rt.close()

    def test_06_contracts_include_snapshot_retry_uncommitted_and_p2_fixes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tf_reg_contracts_") as td:
            root = Path(td)
            st_root = _copy_sandbox_st_data(root)
            state_dir = root / "state"

            # 1. Contract A: `--sidecar-op` with `_include_snapshot: true` returns inline `snapshot`
            cli_proc = subprocess.run(
                [
                    sys.executable,
                    str(PLUGIN_ROOT / "server" / "mcp_server.py"),
                    "--data-root",
                    str(st_root),
                    "--state-dir",
                    str(state_dir),
                    "--sidecar-op",
                    "bootstrap",
                    "--args",
                    "-",
                ],
                input=json.dumps({"character_name": "Seraphina", "mode": "chat", "_include_snapshot": True}),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                check=True,
            )
            cli_out = json.loads(cli_proc.stdout)
            self.assertTrue(cli_out["ok"])
            self.assertEqual(cli_out["data"]["mode"], "--chat")
            self.assertIn("snapshot", cli_out)
            self.assertEqual(cli_out["snapshot"]["session"]["mode"], "chat")

            rt = TavernFusionRuntime(st_data_root=st_root, state_dir=state_dir)
            try:
                # 2. Contract B: `last_persistence_error` exposed in snapshot & replayed by `retry_uncommitted_turn`
                rt.update_active_session({
                    "persistence_failure_count": 2,
                    "last_persistence_error": {
                        "error": "WinError 32",
                        "ts": time.time(),
                        "uncommitted_turn": {
                            "character_name": "Seraphina.png",
                            "user_mes": "补交测试玩家台词",
                            "char_mes": "*她将星盘归位。* \"这层记录已补齐。\"",
                            "swipe_mode": False,
                        },
                    },
                })
                snap_with_err = rt.handle_sidecar_op("snapshot", {})
                self.assertIsNotNone(snap_with_err["session"]["last_persistence_error"])
                retry_res = rt.handle_sidecar_op("retry_uncommitted_turn", {})
                self.assertTrue(retry_res["retried"])
                snap_after_retry = rt.handle_sidecar_op("snapshot", {})
                self.assertIsNone(snap_after_retry["session"]["last_persistence_error"])
                self.assertTrue(any("这层记录已补齐" in m["mes"] for m in snap_after_retry["messages"]))
                with self.assertRaises(ValueError):
                    rt.handle_sidecar_op("retry_uncommitted_turn", {})

                # 3. Contract C: Expression sprite folder matched by `Path(card_file).stem` before `data.name`
                write_character_card_png(
                    {
                        "spec": "chara_card_v3",
                        "spec_version": "3.0",
                        "data": {
                            "name": "Display Title",
                            "description": "Adult scholar, 26 years old.",
                            "first_mes": "Hello.",
                        },
                    },
                    st_root / "characters" / "stem_custom_01.png",
                )
                expr_dir = st_root / "characters" / "stem_custom_01"
                expr_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(st_root / "characters" / "Seraphina.png", expr_dir / "joy.png")
                rt.handle_sidecar_op("bootstrap", {"character_name": "stem_custom_01.png"})
                snap_expr = rt.handle_sidecar_op("snapshot", {})
                card_entry = next(c for c in snap_expr["cards"] if c["file"] == "stem_custom_01.png")
                self.assertEqual(card_entry["expressions_count"], 1)
                self.assertEqual(snap_expr["expressions"], ["joy"])

                # 4. `st_switch_profile` rejects invalid / credential-bearing `byok_base_url` upfront
                with self.assertRaises(ValueError):
                    rt.tool_st_switch_profile({"byok_base_url": "http://remote.example.com/v1"})
                with self.assertRaises(ValueError):
                    rt.tool_st_switch_profile({"byok_base_url": "https://user:secret@api.example.com/v1"})

                # 5. `evolve_lorebook_file` upsert honors `disable` & `outlet_name`/`role`, and recursion respects cooldown/prob
                lore_file = st_root / "worlds" / "RegLore.json"
                r_ins = evolve_lorebook_file(lore_file, "upsert", {
                    "comment": "Outlet Entry",
                    "key": ["board"],
                    "content": "Bounty: Dragon",
                    "disable": True,
                    "position": "outlet",
                    "outlet_name": "quest_board",
                    "role": "user",
                })
                parsed_entries = load_lorebook_file(lore_file)
                self.assertFalse(parsed_entries[0].enabled)
                self.assertEqual(parsed_entries[0].position, "outlet")
                self.assertEqual(parsed_entries[0].outlet_name, "quest_board")
                self.assertEqual(parsed_entries[0].role, "user")
                evolve_lorebook_file(lore_file, "upsert", {"uid": r_ins["uid"], "enabled": True})
                self.assertTrue(load_lorebook_file(lore_file)[0].enabled)

                eng = HybridLoreEngine()
                try:
                    rec_entries = [
                        NormalizedLoreEntry(uid=1, source_book="B", source_tier=2, keys=["start"], content="mentions secret_cd and zero_prob"),
                        NormalizedLoreEntry(uid=2, source_book="B", source_tier=2, keys=["secret_cd"], content="CD_LEAK", cooldown=2),
                        NormalizedLoreEntry(uid=3, source_book="B", source_tier=2, keys=["zero_prob"], content="PROB_LEAK", use_probability=True, probability=0),
                    ]
                    rec_res = eng.evaluate_and_splice(
                        entries=rec_entries,
                        chat_messages=[],
                        latest_user_input="start",
                        timed_state={"sticky": {}, "cooldown": {"B:2": 1}},
                    )
                    active_uids = {t["uid"] for t in rec_res["active_trace"]}
                    self.assertEqual(active_uids, {1})
                finally:
                    eng.close()

                # 6. Macro backslash safety, `before_examples`/`after_examples` + post-history custom prompts + `promptOnly` regex + emotion boundaries
                expanded = resolve_st_macros(r"Hi {{char}} and {{user}}", r"A\sB", r"C\1D")
                self.assertEqual(expanded, r"Hi A\sB and C\1D")

                frame_check = compile_st_prompt_frame(
                    card_v3={
                        "data": {
                            "name": "Lyra",
                            "mes_example": "<START>\n{{user}}: Hi\n{{char}}: *nods* \"Hello.\"",
                            "extensions": {
                                "regex_scripts": [
                                    {
                                        "findRegex": r"/\[HUD:[^\]]*\]/g",
                                        "replaceString": "",
                                        "promptOnly": True,
                                        "markdownOnly": False,
                                    }
                                ]
                            },
                        }
                    },
                    persona={"name": "Ren", "description": ""},
                    preset={
                        "preset_name": "CustomP",
                        "ordered_identifiers": ["main", "dialogueExamples", "chatHistory", "after_custom"],
                        "custom_prompts": {
                            "after_custom": {"name": "TailRule", "role": "system", "content": "POST_HIST_CUSTOM_MARKER", "injection_position": 0, "injection_depth": 0}
                        },
                    },
                    lore_result={
                        "active_trace": [
                            {"uid": 10, "comment": "bex", "position": "before_examples", "constant": True, "content": "BEFORE_EX_MARKER", "trigger": "constant", "order": 1, "tokens": 4},
                            {"uid": 11, "comment": "aex", "position": "after_examples", "constant": True, "content": "AFTER_EX_MARKER", "trigger": "constant", "order": 2, "tokens": 4},
                        ],
                        "spliced_chat": [{"name": "Lyra", "is_user": False, "mes": "[HUD:HP=100] *She waves.*"}],
                        "slots": {},
                        "outlets": {},
                        "used_tokens": 8,
                        "token_budget": 2048,
                        "evicted_trace": [],
                    },
                    chat_header={"chat_metadata": {"variables": {}}},
                )
                self.assertIn("BEFORE_EX_MARKER", frame_check["static_prefix_block"])
                self.assertIn("AFTER_EX_MARKER", frame_check["static_prefix_block"])
                self.assertIn("POST_HIST_CUSTOM_MARKER", frame_check["dynamic_tail_block"])
                self.assertNotIn("[HUD:HP=100]", frame_check["ephemeral_frame"])
                self.assertIn("*She waves.*", frame_check["ephemeral_frame"])

                self.assertEqual(
                    detect_message_emotion(
                        "*She approached the desk and stepped away with sober eyes.*",
                        ["neutral", "admiration", "sadness", "grief"],
                    ),
                    "neutral",
                )
                self.assertEqual(
                    detect_message_emotion("*She laughs warmly and smiles!*", ["neutral", "joy"]),
                    "joy",
                )
            finally:
                rt.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
