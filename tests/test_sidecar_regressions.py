#!/usr/bin/env python3
"""Targeted regression tests for Tavern Stage Sidecar (`main.mjs`) and `st-ag-sync` extension.

Covers:
1. Anti-cache `Cache-Control` headers across all page, media (200/400/404/415), and API routes.
2. `dirStamp` / `computeDiskEtag`: same-mtime size delta & active character expression directory tracking.
3. `serveMediaAsset`: `card` stem priority over `char`, fallback to `char`, ADS/device-name rejection.
4. `invokePythonOp`: >1.5MB stdin payload into early-exiting Python child without `EPIPE` crash,
   plus `~` home-path sanitization in error responses.
5. `/api/state` `mutationEpoch` anti-poisoning, `inflightSnapshot` single-flight deduplication,
   and `_include_snapshot` inline piggybacking on `POST /api/op`.
6. Single-flight HTTP `409` turn lock, timeout process-tree cleanup (including grandchild),
   `byok_stream_preview.json` cleanup & `cancel_turn(force_reset=True)` recovery.
7. `/api/stream_preview` lifecycle (active, expired, missing `ENOENT`, mid-write partial JSON).
8. `resolveStDataRoot` priority & read-only guarantee (`plugin_settings.json` never overwritten by GET),
   `normalizeWinPath`, and `sanitizeFilenameStem`.
9. `st-ag-sync` pure decision engine & simulated browser runtime (`/api/chats/recent` pinned probe,
   404 fallback to `/api/chats/search`, local vs external `integrity` check, pill state classes).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request

for _np_var in ("NO_PROXY", "no_proxy"):
    _np_cur = os.environ.get(_np_var, "")
    if "127.0.0.1" not in _np_cur:
        os.environ[_np_var] = ",".join(x for x in (_np_cur, "127.0.0.1", "localhost") if x)

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from server.card_codec import write_character_card_png

MAIN_MJS = PLUGIN_ROOT / "sidecars" / "tavern_stage" / "main.mjs"
ST_SYNC_JS = PLUGIN_ROOT / "st_web_extension" / "st-ag-sync" / "index.js"
ST_SYNC_CSS = PLUGIN_ROOT / "st_web_extension" / "st-ag-sync" / "style.css"


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_proc_safely(proc: subprocess.Popen) -> None:
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream:
            try:
                stream.close()
            except OSError:
                pass
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            proc.kill()
            proc.wait(timeout=4)


def _make_minimal_st_sandbox(tmp_dir: Path) -> tuple[Path, bytes]:
    src = PLUGIN_ROOT / "examples" / "mock_st_data" / "default-user"
    dst = tmp_dir / "data" / "default-user"
    shutil.copytree(src, dst)
    char_dir = dst / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)
    card_payload = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "苍银守望者",
            "description": "Keeper of the Starfall Belfry.",
            "personality": "Calm and observant.",
            "scenario": "Belfry at night.",
            "first_mes": '"Welcome to the belfry."',
            "mes_example": "",
            "system_prompt": "{{original}}",
            "alternate_greetings": [],
            "tags": ["fantasy"],
            "creator": "安钰 (AnYu)",
            "extensions": {"world": "Eldoria"},
        },
    }
    card_path = char_dir / "seraphina_v2.png"
    write_character_card_png(card_payload, card_path)
    png_bytes = card_path.read_bytes()
    return dst, png_bytes


def _http_get(url: str, timeout: float = 8.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except urllib.error.HTTPError as err:
        try:
            headers = {k.lower(): v for k, v in err.headers.items()}
            return err.code, headers, err.read()
        finally:
            err.close()


def _http_post_json(
    url: str,
    payload: dict | bytes,
    timeout: float = 10.0,
    content_type: str = "application/json",
) -> tuple[int, dict[str, str], dict]:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status, headers, body
    except urllib.error.HTTPError as err:
        try:
            headers = {k.lower(): v for k, v in err.headers.items()}
            body = json.loads(err.read().decode("utf-8"))
            return err.code, headers, body
        finally:
            err.close()


MOCK_MCP_CONTROLLER_PY = textwrap.dedent(
    """\
    import argparse
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--sidecar-op", default="")
    parser.add_argument("--args", default="{}")
    parsed = parser.parse_args()

    state_dir = Path(parsed.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    op = parsed.sidecar_op
    log_file = state_dir / "mock_invocations.jsonl"

    # Early exit BEFORE reading stdin to test >64KB EPIPE resilience in `main.mjs`!
    if op == "import_card" and (state_dir / "force_early_exit.flag").exists():
        home_leak = str(Path.home() / "secret_dir" / "bad_card.png")
        sys.stdout.write(json.dumps({"ok": False, "error": f"Early validation reject at {home_leak}"}) + "\\n")
        sys.stdout.flush()
        sys.exit(0)

    raw_in = sys.stdin.read() if parsed.args == "-" else parsed.args
    args = json.loads(raw_in) if raw_in.strip() else {}
    want_snap = bool(isinstance(args, dict) and args.pop("_include_snapshot", False))

    with open(log_file, "a", encoding="utf-8") as lf:
        lf.write(json.dumps({"op": op, "args": args, "want_snap": want_snap, "pid": os.getpid()}) + "\\n")

    ver_file = state_dir / "version_counter.txt"
    cur_ver = int(ver_file.read_text(encoding="utf-8")) if ver_file.exists() else 1

    if op == "snapshot":
        # Read `cur_ver` BEFORE sleeping to faithfully simulate a stale read in flight!
        cur_ver = int(ver_file.read_text(encoding="utf-8")) if ver_file.exists() else 1
        delay_file = state_dir / "snapshot_delay_ms.txt"
        if delay_file.exists():
            ms = int(delay_file.read_text(encoding="utf-8").strip() or "0")
            if ms > 0:
                delay_file.unlink(missing_ok=True)
                time.sleep(ms / 1000.0)
        snap = {
            "version": cur_ver,
            "session": {
                "active": True,
                "character_name": "苍银守望者",
                "card_file": "seraphina_v2.png",
                "stage_busy": False,
            },
            "messages": [{"name": "苍银守望者", "is_user": False, "mes": f"v={cur_ver}"}],
        }
        sys.stdout.write(json.dumps({"ok": True, "data": snap}) + "\\n")
        sys.exit(0)

    if op == "manage_variables":
        next_ver = cur_ver + 1
        ver_file.write_text(str(next_ver), encoding="utf-8")
        sess_p = state_dir / "active_session.json"
        sess_p.write_text(
            json.dumps({
                "active": True,
                "character_name": "苍银守望者",
                "card_file": "seraphina_v2.png",
                "avatar_stem": "seraphina_v2",
                "ver": next_ver,
            }),
            encoding="utf-8",
        )
        out = {"ok": True, "data": {"scope": "chat", "variables": {args.get("key", "k"): args.get("value")}}}
        if want_snap and not (state_dir / "disable_inline_snap.flag").exists():
            out["snapshot"] = {
                "version": next_ver,
                "session": {
                    "active": True,
                    "character_name": "苍银守望者",
                    "card_file": "seraphina_v2.png",
                    "stage_busy": False,
                },
                "messages": [{"name": "苍银守望者", "is_user": False, "mes": f"v={next_ver}"}],
            }
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.exit(0)

    if op == "byok_turn":
        sess_p = state_dir / "active_session.json"
        sess_p.write_text(
            json.dumps({
                "active": True,
                "character_name": "苍银守望者",
                "card_file": "seraphina_v2.png",
                "stage_busy": True,
                "pending_uncommitted_turn": True,
            }),
            encoding="utf-8",
        )
        preview_p = state_dir / "byok_stream_preview.json"
        preview_p.write_text(
            json.dumps({
                "active": True,
                "action_kind": "send_turn",
                "started_at": time.time(),
                "text": "Streaming partial reply...",
            }),
            encoding="utf-8",
        )
        # Spawn a grandchild sleeper to verify Windows `/T` process-tree termination!
        grandchild = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (state_dir / "grandchild.pid").write_text(str(grandchild.pid), encoding="utf-8")
        mode = str(args.get("mock_mode") or "hang")
        if mode == "cooperative_cancel":
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if (state_dir / "byok_cancel.flag").exists():
                    (state_dir / "byok_cancel.flag").unlink(missing_ok=True)
                    preview_p.unlink(missing_ok=True)
                    grandchild.terminate()
                    grandchild.wait(timeout=3)
                    sess_p.write_text(
                        json.dumps({"active": True, "character_name": "苍银守望者", "stage_busy": False}),
                        encoding="utf-8",
                    )
                    sys.stdout.write(json.dumps({"ok": False, "error": "BYOK 生成已取消"}) + "\\n")
                    sys.exit(0)
                time.sleep(0.05)
        # Otherwise simulate hung remote socket (ignores cancel flag until tree-killed)
        time.sleep(30)
        sys.exit(0)

    if op == "cancel_turn":
        force = bool(args.get("force_reset"))
        (state_dir / "byok_cancel.flag").write_text("1", encoding="utf-8")
        if force:
            (state_dir / "byok_stream_preview.json").unlink(missing_ok=True)
            (state_dir / "byok_cancel.flag").unlink(missing_ok=True)
            (state_dir / "active_session.json").write_text(
                json.dumps({
                    "active": True,
                    "character_name": "苍银守望者",
                    "card_file": "seraphina_v2.png",
                    "stage_busy": False,
                    "pending_uncommitted_turn": False,
                }),
                encoding="utf-8",
            )
        out = {"ok": True, "data": {"cancelled": True, "force_reset": force}}
        if want_snap:
            out["snapshot"] = {
                "version": cur_ver,
                "session": {"active": True, "character_name": "苍银守望者", "stage_busy": False},
                "messages": [],
            }
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.exit(0)

    if op == "retry_uncommitted_turn":
        sys.stdout.write(json.dumps({"ok": True, "data": {"replayed": True}}) + "\\n")
        sys.exit(0)

    sys.stdout.write(json.dumps({"ok": True, "data": {"op": op}}) + "\\n")
    """
)


class SidecarServerRegressionsTest(unittest.TestCase):
    def _boot_sidecar(self, env_overrides: dict[str, str]) -> tuple[subprocess.Popen, str]:
        env = {
            **os.environ,
            "TAVERN_STAGE_STANDALONE": "1",
            "PORT": "0",
            "TAVERN_PYTHON": sys.executable,
            **env_overrides,
        }
        proc = subprocess.Popen(
            ["node", str(MAIN_MJS)],
            cwd=str(MAIN_MJS.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.addCleanup(_terminate_proc_safely, proc)
        assert proc.stdout is not None
        banner = proc.stdout.readline().decode("utf-8", errors="replace").strip()
        m = re.search(r"127\.0\.0\.1:(\d+)", banner)
        self.assertIsNotNone(m, f"Sidecar failed to emit listen banner: {banner!r}")
        port = int(m.group(1))  # type: ignore[union-attr]
        return proc, f"http://127.0.0.1:{port}"

    def test_01_anti_cache_headers_media_card_stem_and_etag_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            st_root, png_bytes = _make_minimal_st_sandbox(tdir)
            state_dir = tdir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)

            # Expression sprites: `joy.png` ONLY under card stem `seraphina_v2/`,
            # `sad.png` ONLY under character display name `苍银守望者/`.
            stem_expr_dir = st_root / "characters" / "seraphina_v2"
            name_expr_dir = st_root / "characters" / "苍银守望者"
            stem_expr_dir.mkdir(parents=True, exist_ok=True)
            name_expr_dir.mkdir(parents=True, exist_ok=True)
            joy_sprite = stem_expr_dir / "joy.png"
            sad_sprite = name_expr_dir / "sad.png"
            joy_sprite.write_bytes(png_bytes)
            sad_sprite.write_bytes(png_bytes)

            bad_svg = st_root / "characters" / "evil.png"
            bad_svg.write_text("<svg><script>alert(1)</script></svg>", encoding="utf-8")

            mock_py = tdir / "mock_mcp.py"
            mock_py.write_text(MOCK_MCP_CONTROLLER_PY, encoding="utf-8")
            (state_dir / "active_session.json").write_text(
                json.dumps({
                    "active": True,
                    "character_name": "苍银守望者",
                    "card_file": "seraphina_v2.png",
                    "avatar_stem": "seraphina_v2",
                }),
                encoding="utf-8",
            )

            _, base = self._boot_sidecar({
                "ST_DATA_ROOT": str(st_root),
                "PLUGIN_STATE_DIR": str(state_dir),
                "TAVERN_MCP_SCRIPT": str(mock_py),
            })

            # 1. All page & script routes MUST carry `Cache-Control: no-cache`
            for page_route in ("/", "/styles.css", "/app.js", "/preload.js"):
                status, hdrs, body = _http_get(f"{base}{page_route}")
                self.assertEqual(status, 200, f"Unexpected status on {page_route}")
                self.assertIn("no-cache", hdrs.get("cache-control", ""), f"Missing no-cache on {page_route}")
                self.assertNotIn("max-age", hdrs.get("cache-control", ""), f"Forbidden max-age on {page_route}")
                self.assertGreater(len(body), 20)

            # 2. `/api/state` and `/api/stream_preview` MUST carry `no-cache` / `no-store`
            st_code, st_hdrs, st_raw = _http_get(f"{base}/api/state")
            self.assertEqual(st_code, 200)
            self.assertIn("no-cache", st_hdrs.get("cache-control", ""))
            self.assertIn("no-store", st_hdrs.get("cache-control", ""))
            etag_0 = json.loads(st_raw.decode("utf-8"))["etag"]
            self.assertEqual(len(etag_0), 16)

            sp_code, sp_hdrs, sp_raw = _http_get(f"{base}/api/stream_preview")
            self.assertEqual(sp_code, 200)
            self.assertIn("no-store", sp_hdrs.get("cache-control", ""))
            self.assertEqual(json.loads(sp_raw.decode("utf-8")), {"active": False})

            # 3. `serveMediaAsset` resolves `card` stem directory BEFORE `char` name, and falls back to `char`
            #    Also verify `Cache-Control: no-cache` on 200, 400, 404, and 415 responses!
            q_joy = urllib.parse.urlencode({
                "kind": "expression",
                "card": "seraphina_v2.png",
                "char": "苍银守望者",
                "expr": "joy",
            })
            m_code, m_hdrs, m_bytes = _http_get(f"{base}/api/media?{q_joy}")
            self.assertEqual(m_code, 200)
            self.assertEqual(m_hdrs.get("cache-control"), "no-cache")
            self.assertEqual(m_hdrs.get("x-content-type-options"), "nosniff")
            self.assertEqual(m_bytes, png_bytes)

            # Without `card`, `joy` is NOT under `苍银守望者/` -> 404 with `no-cache`
            q_joy_no_card = urllib.parse.urlencode({"kind": "expression", "char": "苍银守望者", "expr": "joy"})
            c404, h404, _ = _http_get(f"{base}/api/media?{q_joy_no_card}")
            self.assertEqual(c404, 404)
            self.assertEqual(h404.get("cache-control"), "no-cache")

            # Fallback to `char` when `sad` is absent under `seraphina_v2/`
            q_sad = urllib.parse.urlencode({
                "kind": "expression",
                "card": "seraphina_v2.png",
                "char": "苍银守望者",
                "expr": "sad",
            })
            c_sad, h_sad, _ = _http_get(f"{base}/api/media?{q_sad}")
            self.assertEqual(c_sad, 200)
            self.assertEqual(h_sad.get("cache-control"), "no-cache")

            # Disguised SVG -> 415 with `no-cache`; ADS colon & reserved device `CON` -> 400 with `no-cache`
            c415, h415, _ = _http_get(f"{base}/api/media?kind=avatar&file=evil.png")
            self.assertEqual(c415, 415)
            self.assertEqual(h415.get("cache-control"), "no-cache")

            for bad_file in ("seraphina_v2.png:Zone.Identifier", "CON", "aux.png"):
                c400, h400, _ = _http_get(
                    f"{base}/api/media?{urllib.parse.urlencode({'kind': 'avatar', 'file': bad_file})}"
                )
                self.assertEqual(c400, 400, f"Expected 400 for {bad_file}")
                self.assertEqual(h400.get("cache-control"), "no-cache")

            # 4. Same-mtime, different-size mutation on character card MUST change `etag`!
            card_file = st_root / "characters" / "seraphina_v2.png"
            orig_stat = card_file.stat()
            card_file.write_bytes(png_bytes + b"\x00" * 37)
            os.utime(card_file, ns=(orig_stat.st_atime_ns, orig_stat.st_mtime_ns))

            _, _, st_after_card = _http_get(f"{base}/api/state?etag={etag_0}")
            resp_card = json.loads(st_after_card.decode("utf-8"))
            self.assertFalse(resp_card["unchanged"], "Same-mtime card size change failed to bust ETag")
            etag_1 = resp_card["etag"]
            self.assertNotEqual(etag_1, etag_0)

            # 5. Modifying an expression sprite inside `characters/seraphina_v2/` (even with identical mtime!)
            #    MUST ALSO change `etag`!
            joy_stat = joy_sprite.stat()
            joy_sprite.write_bytes(png_bytes + b"\x00" * 19)
            os.utime(joy_sprite, ns=(joy_stat.st_atime_ns, joy_stat.st_mtime_ns))

            _, _, st_after_expr = _http_get(f"{base}/api/state?etag={etag_1}")
            resp_expr = json.loads(st_after_expr.decode("utf-8"))
            self.assertFalse(resp_expr["unchanged"], "Expression subdirectory change failed to bust ETag")
            etag_2 = resp_expr["etag"]
            self.assertNotEqual(etag_2, etag_1)

    def test_02_stdin_epipe_resilience_home_redaction_and_snapshot_anti_poisoning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            st_root, _ = _make_minimal_st_sandbox(tdir)
            state_dir = tdir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            mock_py = tdir / "mock_mcp.py"
            mock_py.write_text(MOCK_MCP_CONTROLLER_PY, encoding="utf-8")

            proc, base = self._boot_sidecar({
                "ST_DATA_ROOT": str(st_root),
                "PLUGIN_STATE_DIR": str(state_dir),
                "TAVERN_MCP_SCRIPT": str(mock_py),
            })

            # 1. >1.5MB stdin write into an immediately-exiting Python child MUST NOT crash Sidecar (`EPIPE`)
            #    AND error output MUST redact OS home directory to `~`!
            (state_dir / "force_early_exit.flag").write_text("1", encoding="utf-8")
            huge_b64 = "A" * (1600 * 1024)
            code, _, err_body = _http_post_json(
                f"{base}/api/op",
                {"op": "import_card", "args": {"filename": "big.png", "base64_data": huge_b64}},
            )
            self.assertEqual(code, 400)
            self.assertFalse(err_body["ok"])
            self.assertIn("~/secret_dir/bad_card.png", err_body["error"].replace("\\", "/"))
            self.assertNotIn(str(Path.home()), err_body["error"])
            self.assertIsNone(proc.poll(), "Sidecar process crashed after EPIPE!")

            # Non-JSON POST content-type is rejected with 415
            c_ct, _, _ = _http_post_json(
                f"{base}/api/op",
                b'{"op":"lint"}',
                content_type="text/plain",
            )
            self.assertEqual(c_ct, 415)

            # 2. Single-Flight deduplication on concurrent `/api/state` calls
            log_file = state_dir / "mock_invocations.jsonl"
            log_file.unlink(missing_ok=True)
            (state_dir / "snapshot_delay_ms.txt").write_text("220", encoding="utf-8")

            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(_http_get, f"{base}/api/state") for _ in range(4)]
                results = [json.loads(f.result()[2].decode("utf-8")) for f in futs]

            for r in results:
                self.assertFalse(r["unchanged"])
                self.assertEqual(r["state"]["version"], 1)

            invocations = [
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            snap_calls = [x for x in invocations if x["op"] == "snapshot"]
            self.assertEqual(len(snap_calls), 1, f"Expected single-flight 1 snapshot spawn, got {len(snap_calls)}")

            # 3. Anti-Poisoning + Inline `_include_snapshot`:
            #    Bust disk ETag first so `/api/state` actually spawns a slow (380ms) snapshot that reads version=1;
            #    while it is in flight, complete a `POST /api/op` (`manage_variables` -> bumps version 1 -> 2).
            (state_dir / "stage_suggestions.json").write_text('{"suggestions":["a"]}', encoding="utf-8")
            (state_dir / "snapshot_delay_ms.txt").write_text("380", encoding="utf-8")
            with ThreadPoolExecutor(max_workers=2) as pool:
                slow_state_fut = pool.submit(_http_get, f"{base}/api/state")
                time.sleep(0.08)
                op_code, _, op_resp = _http_post_json(
                    f"{base}/api/op",
                    {"op": "manage_variables", "args": {"key": "affection", "value": 99}},
                )
                _, _, slow_raw = slow_state_fut.result()

            self.assertEqual(op_code, 200)
            self.assertTrue(op_resp["ok"])
            # Contract A: `/api/op` piggybacks `state` + `etag` from single Python invocation!
            self.assertEqual(op_resp["state"]["version"], 2)
            fresh_etag = op_resp["etag"]

            slow_resp = json.loads(slow_raw.decode("utf-8"))
            self.assertEqual(
                slow_resp["state"]["version"],
                2,
                "Stale in-flight snapshot leaked old version=1 over concurrent mutation!",
            )

            # Subsequent `/api/state` (without etag) MUST serve version=2, never poisoned version=1
            _, _, check_raw = _http_get(f"{base}/api/state")
            check_resp = json.loads(check_raw.decode("utf-8"))
            self.assertEqual(check_resp["state"]["version"], 2)
            self.assertEqual(check_resp["etag"], fresh_etag)

    def test_03_single_flight_409_cancel_turn_tree_kill_and_timeout_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            st_root, _ = _make_minimal_st_sandbox(tdir)
            state_dir = tdir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            mock_py = tdir / "mock_mcp.py"
            mock_py.write_text(MOCK_MCP_CONTROLLER_PY, encoding="utf-8")

            _, base = self._boot_sidecar({
                "ST_DATA_ROOT": str(st_root),
                "PLUGIN_STATE_DIR": str(state_dir),
                "TAVERN_MCP_SCRIPT": str(mock_py),
                "TAVERN_BYOK_TIMEOUT_MS": "900",
                "TAVERN_CANCEL_GRACE_MS": "250",
            })

            # 1. `/api/stream_preview` handles active, partial/corrupt JSON, expired, and missing (`ENOENT`)
            preview_file = state_dir / "byok_stream_preview.json"
            preview_file.write_text(
                json.dumps({"active": True, "action_kind": "send_turn", "started_at": time.time(), "text": "你好"}),
                encoding="utf-8",
            )
            _, _, p_ok = _http_get(f"{base}/api/stream_preview")
            self.assertEqual(json.loads(p_ok.decode("utf-8")), {"active": True, "action_kind": "send_turn", "text": "你好"})

            preview_file.write_text("{corrupt_half_written", encoding="utf-8")
            _, _, p_half = _http_get(f"{base}/api/stream_preview")
            self.assertEqual(json.loads(p_half.decode("utf-8")), {"active": True, "text": None})

            preview_file.write_text(
                json.dumps({"active": True, "action_kind": "send_turn", "started_at": time.time() - 999, "text": "old"}),
                encoding="utf-8",
            )
            _, _, p_exp = _http_get(f"{base}/api/stream_preview")
            self.assertEqual(json.loads(p_exp.decode("utf-8")), {"active": False})
            preview_file.unlink(missing_ok=True)

            # 2. Hung `byok_turn`:
            #    - Concurrent `byok_turn` or `prepare_agent_turn` returns HTTP 409;
            #    - `cancel_turn` waits grace window, kills exact process tree (including grandchild!),
            #      removes `byok_stream_preview.json`, and resets `stage_busy` via `force_reset=True`.
            gpid_file = state_dir / "grandchild.pid"
            gpid_file.unlink(missing_ok=True)
            with ThreadPoolExecutor(max_workers=3) as pool:
                byok_fut = pool.submit(
                    _http_post_json,
                    f"{base}/api/op",
                    {"op": "byok_turn", "args": {"mock_mode": "hang"}},
                    15.0,
                )
                deadline = time.time() + 4.0
                while not gpid_file.exists() and time.time() < deadline:
                    time.sleep(0.03)
                self.assertTrue(gpid_file.exists(), "Mock byok_turn did not spawn grandchild in time")
                gpid = int(gpid_file.read_text(encoding="utf-8").strip())
                self.assertTrue(_is_pid_alive(gpid))

                # Concurrent turn dispatch MUST be rejected with HTTP 409!
                c409_a, _, body_409_a = _http_post_json(f"{base}/api/op", {"op": "byok_turn", "args": {}})
                c409_b, _, body_409_b = _http_post_json(f"{base}/api/op", {"op": "prepare_agent_turn", "args": {}})
                self.assertEqual(c409_a, 409)
                self.assertEqual(c409_b, 409)
                self.assertFalse(body_409_a["ok"])
                self.assertFalse(body_409_b["ok"])

                # Trigger `cancel_turn` while `byok_turn` is hung
                c_cancel, _, cancel_body = _http_post_json(f"{base}/api/op", {"op": "cancel_turn", "args": {}})
                self.assertEqual(c_cancel, 200)
                self.assertTrue(cancel_body["ok"])
                self.assertTrue(cancel_body["data"]["force_reset"])

                byok_code, _, _ = byok_fut.result()
                self.assertEqual(byok_code, 400)

            # Grandchild MUST be terminated by tree kill, preview removed, and `stage_busy` reset!
            time.sleep(0.1)
            self.assertFalse(_is_pid_alive(gpid), f"Grandchild PID {gpid} leaked after cancel_turn tree kill!")
            self.assertFalse(preview_file.exists())
            sess_after_cancel = json.loads((state_dir / "active_session.json").read_text(encoding="utf-8"))
            self.assertFalse(sess_after_cancel["stage_busy"])

            # 3. Timeout on `byok_turn` (~900ms) ALSO kills grandchild tree, cleans preview, and resets `stage_busy`!
            gpid_file.unlink(missing_ok=True)
            t_code, _, t_body = _http_post_json(
                f"{base}/api/op",
                {"op": "byok_turn", "args": {"mock_mode": "hang"}},
                15.0,
            )
            self.assertEqual(t_code, 400)
            self.assertIn("timed out", t_body["error"])
            gpid_2 = int(gpid_file.read_text(encoding="utf-8").strip())
            time.sleep(0.1)
            self.assertFalse(_is_pid_alive(gpid_2), f"Grandchild PID {gpid_2} leaked after timeout tree kill!")
            self.assertFalse(preview_file.exists())
            sess_after_timeout = json.loads((state_dir / "active_session.json").read_text(encoding="utf-8"))
            self.assertFalse(sess_after_timeout["stage_busy"])

            # `retry_uncommitted_turn` is permitted in `ALLOWED_OPS`
            r_code, _, r_body = _http_post_json(f"{base}/api/op", {"op": "retry_uncommitted_turn", "args": {}})
            self.assertEqual(r_code, 200)
            self.assertTrue(r_body["data"]["replayed"])

    def test_04_node_unit_helpers_and_st_ag_sync_extension_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            remembered_dir = tdir / "remembered_st" / "data" / "default-user"
            ws_dir = tdir / "ws_project"
            ws_st_dir = ws_dir / "data" / "default-user"
            remembered_dir.mkdir(parents=True, exist_ok=True)
            ws_st_dir.mkdir(parents=True, exist_ok=True)
            state_dir = tdir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            settings_file = state_dir / "plugin_settings.json"
            orig_settings = {
                "last_st_data_root": str(remembered_dir),
                "generation_backend": "byok",
                "byok_model": "gemini-test",
            }
            settings_file.write_text(json.dumps(orig_settings), encoding="utf-8")

            node_driver = textwrap.dedent(
                """\
                import assert from 'node:assert/strict';
                import { readFileSync } from 'node:fs';
                import { pathToFileURL } from 'node:url';
                import vm from 'node:vm';

                const [mainPath, syncJsPath, syncCssPath, stateDir, remDir, wsUri] = process.argv.slice(2);
                process.env.TAVERN_STAGE_NO_LISTEN = '1';
                process.env.PLUGIN_STATE_DIR = stateDir;
                delete process.env.ST_DATA_ROOT;
                delete process.env.ST_USER_DATA_DIR;

                const mainMod = await import(pathToFileURL(mainPath).href);

                // 1. `normalizeWinPath`, `sanitizeFilenameStem`, `sanitizeErrorText`
                assert.equal(mainMod.normalizeWinPath('d:/Foo/Bar', 'win32').slice(0, 2), 'D:');
                assert.equal(mainMod.normalizeWinPath('/d/SillyTavern/data', 'win32').slice(0, 2), 'D:');
                assert.equal(mainMod.sanitizeFilenameStem('Mr. X.  '), 'Mr. X');
                assert.equal(mainMod.sanitizeFilenameStem('CON'), '_CON');
                assert.equal(mainMod.sanitizeFilenameStem('nul.png'), '_nul.png');
                assert.equal(mainMod.sanitizeFilenameStem('..'), '');
                assert.equal(
                  mainMod.sanitizeErrorText('Failed at C:\\\\Users\\\\DemoPerson\\\\secret.json', 'C:\\\\Users\\\\DemoPerson'),
                  'Failed at ~\\\\secret.json'
                );

                // 2. `resolveStDataRoot`: remembered `last_st_data_root` wins over workspace guess
                //    AND never mutates `plugin_settings.json`!
                const beforeSettings = readFileSync(`${stateDir}/plugin_settings.json`, 'utf-8');
                const resolved = mainMod.resolveStDataRoot(wsUri, stateDir);
                assert.equal(resolved, mainMod.normalizeWinPath(remDir));
                const afterSettings = readFileSync(`${stateDir}/plugin_settings.json`, 'utf-8');
                assert.equal(afterSettings, beforeSettings);

                // 3. `st-ag-sync` pure decision logic (`evaluateSyncDecision` & `computeBackoffDelay`)
                const syncCode = readFileSync(syncJsPath, 'utf-8');
                await import(pathToFileURL(syncJsPath).href);
                const internals = globalThis.__stAgSyncInternals;
                assert.ok(internals, '__stAgSyncInternals must be exposed');

                // 3a. In-app ST save: disk integrity === memory integrity -> NEVER reload!
                const rLocal = internals.evaluateSyncDecision({
                  row: {
                    file_id: 'chat1',
                    file_size: '12.40 KB',
                    chat_items: 5,
                    last_mes: '2026-11-18 02:00:00',
                    mes: 'New message typed inside ST',
                    chat_metadata: { integrity: 'uuid-same-111' },
                  },
                  prevSignature: 'uuid-old|4:2026-11-18 01:59:00:12.10 KB:Old',
                  memIntegrity: 'uuid-same-111',
                });
                assert.equal(rLocal.shouldReload, false);
                assert.equal(rLocal.reason, 'in_memory_integrity_match');

                // 3b. External Tavern Stage edit on floor #1 (same floor count, same file_size KB string,
                //     same last message, ONLY `integrity` rotated!) -> MUST reload!
                const rExtEdit = internals.evaluateSyncDecision({
                  row: {
                    file_id: 'chat1',
                    file_size: '12.40 KB',
                    chat_items: 5,
                    last_mes: '2026-11-18 02:00:00',
                    mes: 'New message typed inside ST',
                    chat_metadata: { integrity: 'uuid-rotated-by-ag-222' },
                  },
                  prevSignature: rLocal.nextSignature,
                  memIntegrity: 'uuid-same-111',
                });
                assert.equal(rExtEdit.shouldReload, true);
                assert.equal(rExtEdit.reason, 'external_integrity_rotated');

                // 3c. Loop guard: if same rotated signature is seen again before memory updates, don't infinite-loop
                const rNoLoop = internals.evaluateSyncDecision({
                  row: {
                    file_id: 'chat1',
                    file_size: '12.40 KB',
                    chat_items: 5,
                    last_mes: '2026-11-18 02:00:00',
                    mes: 'New message typed inside ST',
                    chat_metadata: { integrity: 'uuid-rotated-by-ag-222' },
                  },
                  prevSignature: rExtEdit.nextSignature,
                  memIntegrity: 'uuid-same-111',
                });
                assert.equal(rNoLoop.shouldReload, false);

                // 3d. Legacy `/api/chats/search` fallback (no integrity): suppresses self-reload on local event match,
                //     reloads on external delta
                const baseFallback = internals.evaluateSyncDecision({
                  row: { file_name: 'chat1', file_size: '5 KB', message_count: 2, last_mes: 't1', preview_message: 'hi' },
                  prevSignature: '',
                });
                assert.equal(baseFallback.shouldReload, false);

                const localFallback = internals.evaluateSyncDecision({
                  row: { file_name: 'chat1', file_size: '6 KB', message_count: 3, last_mes: 't2', preview_message: 'self reply' },
                  prevSignature: baseFallback.nextSignature,
                  memChatLength: 3,
                  memLastMes: 'self reply',
                  localDirtySinceLastPoll: true,
                });
                assert.equal(localFallback.shouldReload, false);

                const extFallback = internals.evaluateSyncDecision({
                  row: { file_name: 'chat1', file_size: '7 KB', message_count: 4, last_mes: 't3', preview_message: 'ag reply' },
                  prevSignature: localFallback.nextSignature,
                  memChatLength: 3,
                  memLastMes: 'self reply',
                  localDirtySinceLastPoll: false,
                });
                assert.equal(extFallback.shouldReload, true);

                // 3e. Simulated DOM + fetch runtime: `/api/chats/recent` pinned payload, 404 fallback, pill toggle
                const cssText = readFileSync(syncCssPath, 'utf-8');
                assert.match(cssText, /\\.st-ag-sync-pill\\.is-disabled\\s+\\.st-ag-sync-dot/);
                assert.match(cssText, /\\.st-ag-sync-pill\\.has-error\\s+\\.st-ag-sync-dot/);

                let reloadCalls = 0;
                const fetchCalls = [];
                let recentMode = 'ok';
                let pillEl = null;
                const listeners = {};
                const fakeTopBar = {
                  appendChild(el) { pillEl = el; },
                };
                const makeClassList = () => {
                  const set = new Set();
                  return {
                    toggle(cls, force) {
                      const next = force === undefined ? !set.has(cls) : Boolean(force);
                      if (next) set.add(cls); else set.delete(cls);
                      return next;
                    },
                    contains(cls) { return set.has(cls); },
                  };
                };
                const fakeCtx = {
                  characterId: 0,
                  chatId: '2026-11-18@02h00m00s.jsonl',
                  characters: [{ avatar: 'Seraphina.png' }],
                  chatMetadata: { integrity: 'mem-uuid-1' },
                  chat: [{ mes: 'hello' }],
                  eventTypes: { CHAT_CHANGED: 'chat_changed', MESSAGE_SENT: 'message_sent' },
                  eventSource: {
                    on(evt, fn) { listeners[evt] = fn; },
                  },
                  getRequestHeaders: () => ({ 'Content-Type': 'application/json' }),
                  reloadCurrentChat: async () => { reloadCalls += 1; },
                };

                let scheduledFn = null;
                let clearedCount = 0;
                const sandbox = {
                  console,
                  URLSearchParams,
                  AbortController,
                  Date,
                  Math,
                  Number,
                  String,
                  Boolean,
                  Array,
                  Set,
                  JSON,
                  Error,
                  setTimeout: (fn) => { scheduledFn = fn; return 77; },
                  clearTimeout: () => { clearedCount += 1; },
                  setInterval: () => 88,
                  clearInterval: () => {},
                  fetch: async (url, opts) => {
                    fetchCalls.push({ url, body: JSON.parse(opts.body) });
                    if (url === '/api/chats/recent') {
                      if (recentMode === '404') return { ok: false, status: 404, json: async () => [] };
                      return {
                        ok: true,
                        status: 200,
                        json: async () => [{
                          file_id: '2026-11-18@02h00m00s',
                          avatar: 'Seraphina.png',
                          chat_items: 2,
                          file_size: '1.2 KB',
                          last_mes: 't',
                          mes: 'ag floor',
                          chat_metadata: { integrity: 'disk-uuid-external-2' },
                        }],
                      };
                    }
                    return {
                      ok: true,
                      status: 200,
                      json: async () => [{
                        file_name: '2026-11-18@02h00m00s',
                        message_count: 3,
                        file_size: '1.5 KB',
                        last_mes: 't2',
                        preview_message: 'fallback floor',
                      }],
                    };
                  },
                  document: {
                    hidden: false,
                    readyState: 'complete',
                    addEventListener: () => {},
                    getElementById: (id) => (id === 'st-ag-sync-pill' ? pillEl : null),
                    querySelector: (sel) => (sel === '#top-bar' ? fakeTopBar : (sel === '#st-ag-sync-pill' ? pillEl : null)),
                    createElement: () => ({
                      id: '',
                      className: '',
                      innerHTML: '',
                      title: '',
                      classList: makeClassList(),
                      addEventListener(ev, fn) { this._click = fn; },
                    }),
                  },
                  window: null,
                };
                sandbox.window = { SillyTavern: { getContext: () => fakeCtx } };
                sandbox.globalThis = sandbox;
                vm.runInNewContext(syncCode, sandbox);

                assert.ok(pillEl, 'Sync pill mounted');
                assert.ok(scheduledFn, 'Initial poll scheduled');
                // Run first tick -> `/api/chats/recent` sees external integrity mismatch and triggers reload!
                await scheduledFn();
                assert.equal(fetchCalls[0].url, '/api/chats/recent');
                assert.deepEqual(fetchCalls[0].body.pinned, [{ file_name: '2026-11-18@02h00m00s.jsonl', avatar: 'Seraphina.png' }]);
                assert.equal(reloadCalls, 1);
                assert.ok(listeners.chat_changed && listeners.message_sent, 'Context events lazily bound');

                // Simulate 404 on `/api/chats/recent` -> falls back cleanly to `/api/chats/search`
                recentMode = '404';
                await scheduledFn();
                assert.equal(fetchCalls[fetchCalls.length - 1].url, '/api/chats/search');

                // Clicking pill disables autoSync, applies `.is-disabled`, and clears timer
                const clearsBefore = clearedCount;
                pillEl._click();
                assert.equal(pillEl.classList.contains('is-disabled'), true);
                assert.ok(clearedCount > clearsBefore);
                console.log('NODE_ASSERTIONS_OK');
                """
            )
            driver_file = tdir / "run_node_checks.mjs"
            driver_file.write_text(node_driver, encoding="utf-8")

            res = subprocess.run(
                [
                    "node",
                    str(driver_file),
                    str(MAIN_MJS),
                    str(ST_SYNC_JS),
                    str(ST_SYNC_CSS),
                    str(state_dir),
                    str(remembered_dir),
                    ws_dir.as_uri(),
                ],
                cwd=str(PLUGIN_ROOT),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(
                res.returncode,
                0,
                f"Node helper assertions failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
            )
            self.assertIn("NODE_ASSERTIONS_OK", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
