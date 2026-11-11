#!/usr/bin/env python3
"""Antigravity `Stop` Auto-Persistence Hook (`hooks/stop_persistence_check.py`).

Zero-Tool Pure Prose Persistence + Strict `rp_conv_id` Isolation:
1. Immediately emits `{"decision": "allow"}` for ANY conversation whose `conversationId`
   does not match `active_session.json["rp_conv_id"]`, guaranteeing engineering sessions
   can NEVER be blocked or mutated.
2. For the bound RP conversation (`conversationId == rp_conv_id`) with
   `pending_uncommitted_turn == True`, extracts the latest pure character reply directly
   from the host conversation transcript (`transcript_full.jsonl` / `transcript.jsonl`)
   and atomically commits it to `.jsonl` via `runtime.tool_st_commit_turn` in <10ms—
   eliminating model-side `st_commit_turn` tool calls, double token generation, and
   pre-tool meta commentary!
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hooks.session_init import discover_st_data_from_workspaces
from server.mcp_server import TavernFusionRuntime

NORMAL_COMPLETION_REASONS = frozenset({"NO_TOOL_CALL", "model_stop", ""})
MAX_PERSISTENCE_FAILURES = 3


def _clear_persistence_failure_state(runtime, sess: dict) -> None:
    """Resets the retry counter after a successful commit.

    `tool_st_commit_turn` reloads and saves its own session snapshot, so the
    counter must be cleared from a fresh read rather than from our stale `sess`.
    This is pure bookkeeping: a failure here must never make the caller treat an
    already-committed turn as failed and write the same floor a second time.
    """
    if not sess.get("persistence_failure_count"):
        return
    try:
        runtime.update_active_session({"persistence_failure_count": 0})
    except Exception:
        pass


def _candidate_transcript_files(transcript_path: str, conv_id: str) -> list[Path]:
    cands: list[Path] = []
    if transcript_path:
        tp = Path(transcript_path)
        if tp.name.lower() == "transcript.jsonl":
            cands.append(tp.with_name("transcript_full.jsonl"))
        cands.append(tp)
    if conv_id and all(ch.isalnum() or ch in "_-" for ch in conv_id):
        logs_dir = Path.home() / ".gemini" / "antigravity" / "brain" / conv_id / ".system_generated" / "logs"
        cands.append(logs_dir / "transcript_full.jsonl")
        cands.append(logs_dir / "transcript.jsonl")
    seen: set[str] = set()
    unique: list[Path] = []
    for p in cands:
        key = str(p)
        if key not in seen and p.exists() and p.is_file():
            seen.add(key)
            unique.append(p)
    return unique


def extract_latest_model_prose(transcript_path: str, conv_id: str, fallback_text: str = "") -> str:
    for tf in _candidate_transcript_files(transcript_path, conv_id):
        try:
            size = tf.stat().st_size
            read_offset = max(0, size - 524288)
            with open(tf, "rb") as fh:
                fh.seek(read_offset)
                raw_chunk = fh.read()
            text_chunk = raw_chunk.decode("utf-8", errors="replace")
            lines = text_chunk.splitlines()
            if read_offset > 0 and lines:
                # Drop potentially partial first line when seeking mid-file
                lines = lines[1:]

            latest_pure = ""
            latest_any = ""
            for raw_line in lines:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                row_type = str(row.get("type") or "")
                row_source = str(row.get("source") or "")
                if row_type == "USER_INPUT" or row_source == "USER_EXPLICIT":
                    latest_pure = ""
                    latest_any = ""
                    continue
                if row_source == "MODEL" and row_type in ("PLANNER_RESPONSE", "ASSISTANT_RESPONSE", "MODEL_RESPONSE"):
                    content = str(row.get("content") or "").strip()
                    if not content:
                        continue
                    tool_calls = row.get("tool_calls") or []
                    latest_any = content
                    if not tool_calls:
                        latest_pure = content
            chosen = latest_pure or latest_any
            if chosen:
                return chosen
        except OSError:
            continue
    return fallback_text.strip()


def main() -> None:
    raw_in = sys.stdin.read().strip()
    payload = json.loads(raw_in) if raw_in else {}
    exec_num = int(payload.get("executionNum", 1))
    term_reason = str(payload.get("terminationReason", "NO_TOOL_CALL"))
    conv_id = str(payload.get("conversationId") or "").strip()
    transcript_path = str(payload.get("transcriptPath") or "").strip()
    fallback_msg = str(
        payload.get("lastAssistantMessage")
        or payload.get("assistantResponse")
        or payload.get("modelResponse")
        or ""
    )
    ws_paths = payload.get("workspacePaths") or []

    discovered = discover_st_data_from_workspaces(ws_paths)
    runtime = TavernFusionRuntime(st_data_root=discovered)
    try:
        sess = runtime.load_active_session()
        rp_conv_id = str(sess.get("rp_conv_id") or "").strip()

        # 1. Strict Isolation: NEVER block or mutate any conversation other than `rp_conv_id`!
        if not sess.get("active") or not rp_conv_id or conv_id != rp_conv_id:
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
            return

        retries = int(sess.get("stop_retry_count", 0))
        turn_mode = str(sess.get("pending_turn_mode") or "normal")

        # 2. Circuit breaker on abnormal stop reasons (cancel/error)
        if term_reason not in NORMAL_COMPLETION_REASONS:
            if sess.get("pending_uncommitted_turn") or sess.get("stage_busy") or retries:
                runtime.update_active_session({
                    "pending_uncommitted_turn": False,
                    "user_floor_already_appended": False,
                    "stage_busy": False,
                    "stop_retry_count": 0,
                })
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
            return

        # 3. Auto-persist uncommitted normal / single-swipe / backstage-director prose directly from transcript!
        if sess.get("pending_uncommitted_turn") and sess.get("character_name"):
            if turn_mode in ("normal", "swipe", "director"):
                prose = extract_latest_model_prose(transcript_path, conv_id, fallback_msg)
                if prose:
                    is_swipe = turn_mode == "swipe"
                    is_director = turn_mode == "director"
                    already_appended = bool(sess.get("user_floor_already_appended", False))
                    idem_seed = f"{conv_id}:{turn_mode}:{sess.get('last_user_input') or ''}:{prose}"
                    idem_key = hashlib.sha256(idem_seed.encode("utf-8")).hexdigest()[:20]
                    extra_meta: dict[str, object] = {"idempotency_key": idem_key}
                    if is_director:
                        extra_meta["is_backstage"] = True
                    commit_payload = {
                        "character_name": str(sess.get("card_file") or sess["character_name"]),
                        "user_mes": None if (is_swipe or already_appended) else sess.get("last_user_input"),
                        "char_mes": prose,
                        "swipe_mode": is_swipe,
                        "extra_meta": extra_meta,
                    }
                    try:
                        runtime.tool_st_commit_turn(commit_payload)
                        _clear_persistence_failure_state(runtime, sess)
                        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
                        return
                    except Exception as first_err:
                        # One transient retry (e.g. momentary `.jsonl` lock contention).
                        # Never silently drop the turn: keep the pending flag so the next
                        # Stop hook can retry, and store the full payload as a recovery
                        # point that the Stage inspector can surface and replay.
                        time.sleep(0.5)
                        try:
                            runtime.tool_st_commit_turn(commit_payload)
                            _clear_persistence_failure_state(runtime, sess)
                            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
                            return
                        except Exception as retry_err:
                            def _record_persistence_failure(cur: dict) -> dict:
                                failures = int(cur.get("persistence_failure_count", 0)) + 1
                                cur["persistence_failure_count"] = failures
                                cur["last_persistence_error"] = {
                                    "error": str(retry_err),
                                    "first_error": str(first_err),
                                    "turn_mode": turn_mode,
                                    "ts": time.time(),
                                    "attempts": failures,
                                    "uncommitted_turn": commit_payload,
                                    "hint": (
                                        "关闭正在占用 .jsonl 的 SillyTavern / 同步扩展后重试本轮；"
                                        "未落盘原文保存在 uncommitted_turn.char_mes"
                                    ),
                                }
                                if failures >= MAX_PERSISTENCE_FAILURES:
                                    cur["pending_uncommitted_turn"] = False
                                    cur["user_floor_already_appended"] = False
                                    cur["stage_busy"] = False
                                    cur["stop_retry_count"] = 0
                                return cur

                            runtime.update_active_session(_record_persistence_failure)
                            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
                            return

            elif turn_mode == "kartoittaja" and exec_num < 2 and retries < 1:
                runtime.update_active_session({"stop_retry_count": retries + 1})
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "continue",
                            "reason": (
                                f"[Tavern Fusion] 请静默调用 `st_commit_turn(..., swipe_mode=True)` 将 "
                                f"'{sess.get('character_name')}' 的分支写入 `.jsonl`。"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                return

        # 4. Always clean busy/pending flags cleanly so no session ever hangs
        if sess.get("pending_uncommitted_turn") or sess.get("stage_busy") or retries:
            runtime.update_active_session({
                "pending_uncommitted_turn": False,
                "user_floor_already_appended": False,
                "stage_busy": False,
                "stop_retry_count": 0,
            })
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
