#!/usr/bin/env python3
"""Antigravity `PreInvocation` Lifecycle Hook (`hooks/pre_invocation_gate.py`).

Strict `rp_conv_id` Whitelist Isolation + Zero-Tool Pure Prose Protocol:
1. Non-RP conversations (`conversationId != session.rp_conv_id` and no `/rp` or Sidecar launch)
   immediately receive `{"injectSteps": []}` with ZERO context leakage, ZERO policy false
   positives on engineering prompts, and ZERO mutations to `active_session.json`.
2. `/rp <CharacterName> [--play|--chat]` or a fresh `Tavern Stage` Sidecar launch binds
   `rp_conv_id = conversationId` (`/rp off` | `/rp exit` | `/rp stop` unbinds it).
3. Normal RP dialogue and single Swipe rerolls execute in **Zero-Tool Pure Prose Mode**:
   the model outputs ONLY `{{char}}`'s in-character response without calling `st_commit_turn`,
   and `stop_persistence_check.py` persists the turn from the host transcript automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from hooks.session_init import discover_st_data_from_workspaces
from server.mcp_server import TavernFusionRuntime
from server.st_runtime_compat import evaluate_policy_compliance, resolve_policy_mode

RP_SLASH_RE = re.compile(
    r"^\s*/rp(?:\s+((?!--)\S+))?(?:\s+--(play|chat))?(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)
RP_EXIT_ARGS = frozenset({"off", "stop", "exit", "quit", "end", "close"})
STAGE_PREFIXES = (
    "[STAGE_SUGGEST_REPLIES]",
    "[STAGE_EXPAND_INTENT]",
    "[STAGE_SWIPE_REROLL]",
    "[STAGE_KARTOITTAJA_BRANCHES]",
    "[STAGE_DIRECTOR_INTERVENTION]",
)
UTILITY_SLASHES = ("/dump-prompt", "/lore-weaver", "/card-forge", "/tavern-stage")


def main() -> None:
    raw_in = sys.stdin.read().strip()
    payload = json.loads(raw_in) if raw_in else {}
    last_input = str(payload.get("lastUserInput") or "").strip()
    conv_id = str(payload.get("conversationId") or "").strip()
    ws_paths = payload.get("workspacePaths") or []
    inv_num = int(payload.get("invocationNum", 0))

    # Fast skip on tool continuation loops
    if inv_num > 0:
        sys.stdout.write(json.dumps({"injectSteps": []}) + "\n")
        return

    discovered = discover_st_data_from_workspaces(ws_paths)
    runtime = TavernFusionRuntime(st_data_root=discovered)
    try:
        session = runtime.load_active_session()

        # 1. Claim Tavern Stage Sidecar dispatch (`startConversation` or bound `sendMessage`)
        #    Note: On `window.sidecar.agent.sendMessage` to an existing conversation, the host
        #    keeps `payload.lastUserInput` at the last message typed in the main chat box.
        pending_launch = session.get("pending_sidecar_launch")
        claimed_from_sidecar = False
        effective_raw_input = last_input
        sidecar_user_text = ""
        sidecar_floor_appended = False
        if isinstance(pending_launch, dict) and conv_id:
            launch_ts = float(pending_launch.get("ts") or 0)
            expected_prompt = str(pending_launch.get("prompt") or "").strip()
            age = time.time() - launch_ts
            is_fresh_start = expected_prompt and last_input == expected_prompt
            is_bound_send = (
                bool(expected_prompt)
                and session.get("rp_conv_id") == conv_id
                and (
                    not last_input
                    or last_input == expected_prompt
                    or last_input == session.get("last_seen_host_input")
                    or bool(session.get("stage_busy"))
                )
            )
            if 0 <= age < 60.0 and (is_fresh_start or is_bound_send):
                session["rp_conv_id"] = conv_id
                session["active"] = True
                effective_raw_input = expected_prompt
                sidecar_user_text = str(pending_launch.get("user_text") or "").strip()
                sidecar_floor_appended = bool(pending_launch.get("user_floor_already_appended", False))
                session.pop("pending_sidecar_launch", None)
                claimed_from_sidecar = True

        rp_match = RP_SLASH_RE.match(effective_raw_input)
        is_utility_slash = effective_raw_input.lower().startswith(UTILITY_SLASHES)

        # 2. Handle `/rp off` / `/rp stop` exit command
        if rp_match and rp_match.group(1) and rp_match.group(1).strip().lower() in RP_EXIT_ARGS:
            session["active"] = False
            session["rp_conv_id"] = ""
            session["pending_uncommitted_turn"] = False
            session["user_floor_already_appended"] = False
            session["stage_busy"] = False
            session["stop_retry_count"] = 0
            session.pop("pending_sidecar_launch", None)
            if last_input:
                session["last_seen_host_input"] = last_input
            runtime.save_active_session(session)
            sys.stdout.write(
                json.dumps(
                    {
                        "injectSteps": [
                            {
                                "ephemeralMessage": "[Tavern Fusion] 已退出当前会话的 RP 模式并解除绑定，现已恢复常规工程对话模式。"
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            return

        # 3. Auto-bootstrap & bind `rp_conv_id` on `/rp <CharName> [--play|--chat]`
        is_bootstrap_turn = False
        if claimed_from_sidecar:
            effective_user_input = (
                sidecar_user_text
                if effective_raw_input.startswith(STAGE_PREFIXES)
                else (sidecar_user_text or effective_raw_input)
            )
        else:
            effective_user_input = effective_raw_input
        if rp_match and rp_match.group(1):
            char_arg = rp_match.group(1).strip()
            mode_arg = rp_match.group(2) or session.get("mode", "play")
            trailing_msg = (rp_match.group(3) or "").strip()
            try:
                runtime.tool_st_session_bootstrap({
                    "character_name": char_arg,
                    "mode": mode_arg,
                    "rp_conv_id": conv_id,
                    "activate_rp": True,
                })
                session = runtime.load_active_session()
                is_bootstrap_turn = True
                effective_user_input = trailing_msg
                sidecar_floor_appended = False
            except Exception:
                pass

        is_rp_conv = (
            bool(session.get("active"))
            and bool(session.get("character_name"))
            and (
                (bool(conv_id) and session.get("rp_conv_id") == conv_id)
                or claimed_from_sidecar
                or is_bootstrap_turn
            )
        )

        # 4. Strict Isolation Gate: Non-RP conversations stay 100% untouched!
        if not is_rp_conv and not is_utility_slash:
            sys.stdout.write(json.dumps({"injectSteps": []}) + "\n")
            return

        # 5. Enforced Host Policy Gate (evaluated ONLY inside RP / Tavern utility turns;
        #    defense-in-depth clamp ensures host hooks never enter `passthrough` via env or JSON)
        host_policy_mode = resolve_policy_mode(state_dir=runtime.state_dir)
        if host_policy_mode not in ("host_gentle", "strict_block"):
            host_policy_mode = "host_gentle"
        gate_check_text = (
            f"{effective_raw_input}\n{effective_user_input}".strip()
            if effective_user_input and effective_user_input != effective_raw_input
            else effective_raw_input
        )
        compliant, policy_detail = evaluate_policy_compliance(
            gate_check_text,
            policy_mode=host_policy_mode,
            state_dir=runtime.state_dir,
        )
        if not compliant:
            if session.get("stage_busy") or session.get("pending_uncommitted_turn"):
                session["stage_busy"] = False
                session["pending_uncommitted_turn"] = False
                session["user_floor_already_appended"] = False
                runtime.save_active_session(session)
            block_out = {
                "injectSteps": [
                    {
                        "errorMessage": {
                            "userMessage": f"[Tavern Fusion 安全拦截] 请求触及安全红线已熔断：{policy_detail}"
                        }
                    }
                ],
                "terminationBehavior": "terminate",
            }
            sys.stdout.write(json.dumps(block_out, ensure_ascii=False) + "\n")
            return

        boundary_notice = (
            policy_detail.removeprefix("gentle_notice:")
            if policy_detail.startswith("gentle_notice:")
            else ""
        )

        if is_utility_slash and not is_rp_conv:
            sys.stdout.write(
                json.dumps(
                    {
                        "injectSteps": [
                            {
                                "ephemeralMessage": "[UTILITY INSPECTION TURN]: Execute the requested Tavern Fusion diagnostic/forge tool."
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            return

        char_name = str(session["character_name"])
        char_ref = str(session.get("card_file") or char_name)
        is_suggestion_only = effective_raw_input.startswith(("[STAGE_SUGGEST_REPLIES]", "[STAGE_EXPAND_INTENT]"))
        is_swipe_reroll = effective_raw_input.startswith("[STAGE_SWIPE_REROLL]")
        is_kartoittaja = effective_raw_input.startswith("[STAGE_KARTOITTAJA_BRANCHES]")
        is_director = effective_raw_input.startswith("[STAGE_DIRECTOR_INTERVENTION]") or (
            bool(session.get("director_mode", False))
            and not is_suggestion_only
            and not is_swipe_reroll
            and not is_kartoittaja
            and not is_bootstrap_turn
        )
        agentic_hands = bool(session.get("agentic_hands", False))
        epistemic_level = int(session.get("epistemic_level", 0))

        already_bootstrapped_here = (
            bool(conv_id)
            and session.get("bootstrapped_conv_id") == conv_id
            and session.get("bootstrapped_char") == char_ref
            and not is_bootstrap_turn
        )

        frame_info = runtime.tool_st_dump_prompt_frame({
            "character_name": char_ref,
            "user_input": effective_user_input,
            "mode": session.get("mode", "play"),
            "compact_agent_delta": already_bootstrapped_here,
            "boundary_notice": boundary_notice,
            "epistemic_level": epistemic_level,
            "agentic_hands": agentic_hands,
            "director_mode": is_director,
        })

        if conv_id:
            session["rp_conv_id"] = conv_id
            if not already_bootstrapped_here:
                session["bootstrapped_conv_id"] = conv_id
                session["bootstrapped_char"] = char_ref

        if is_suggestion_only:
            session["pending_uncommitted_turn"] = False
            session["user_floor_already_appended"] = False
            session["pending_turn_mode"] = "suggestions"
            contract_line = (
                "[STAGE SUGGESTION PROTOCOL]: Quietly call `st_publish_stage_suggestions` with 3 distinct options "
                "for {{user}} with zero meta/engineering preamble. Do NOT call `st_commit_turn`."
            )
        elif is_director:
            session["pending_uncommitted_turn"] = True
            session["user_floor_already_appended"] = sidecar_floor_appended
            session["pending_turn_mode"] = "director"
            session["stop_retry_count"] = 0
            session["last_user_input"] = effective_user_input
            # Force re-bootstrap on next normal RP turn after backstage intervention
            session["bootstrapped_conv_id"] = ""
            contract_line = (
                f"[SUPER-DIRECTOR BACKSTAGE INTERVENTION — QUARANTINED (`extra.is_backstage=true`)]: "
                f"You are in Backstage Director Mode with {{{{user}}}} alongside {char_name}. You may freely discuss "
                f"plot direction, code, or card/lorebook mechanics, and you may invoke `st_stage_floor_op`, "
                f"`st_build_export_card`, `st_evolve_lore_entry`, or `st_manage_variables` to modify the scene/card/worldbook "
                f"on the spot. Do NOT call `st_commit_turn`—the Stop hook will persist your backstage reply with "
                f"`extra.is_backstage=true` so it is automatically quarantined when returning to 0%/50% immersion."
            )
        elif is_kartoittaja:
            session["pending_uncommitted_turn"] = True
            session["user_floor_already_appended"] = False
            session["pending_turn_mode"] = "kartoittaja"
            session["stop_retry_count"] = 0
            contract_line = (
                "[STAGE KARTOITTAJA PROTOCOL]: Quietly append 3 distinct alternative replies via "
                "`st_commit_turn(..., swipe_mode=True)` with zero meta/engineering preamble."
            )
        elif is_swipe_reroll:
            session["pending_uncommitted_turn"] = True
            session["user_floor_already_appended"] = False
            session["pending_turn_mode"] = "swipe"
            session["stop_retry_count"] = 0
            contract_line = (
                f"[PURE IMMERSIVE SWIPE REROLL — ZERO TOOL CALLS]: Output ONLY {char_name}'s alternative reply "
                "(dialogue in \"...\" and actions/inner thoughts in *...*). Do NOT call any tools and output NO "
                "engineering/meta commentary—the Stop hook persists this Swipe automatically."
            )
        elif is_bootstrap_turn and not effective_user_input:
            session["pending_uncommitted_turn"] = False
            session["user_floor_already_appended"] = False
            session["pending_turn_mode"] = "bootstrap_greeting"
            contract_line = (
                f"[PURE IMMERSIVE SCENE ENTRY — ZERO TOOL CALLS]: Present {char_name}'s opening or resumed scene "
                "strictly in character (\"...\" and *...*) with zero meta/engineering commentary. Do NOT call "
                "`st_session_bootstrap` or `st_commit_turn` (already bootstrapped on disk)."
            )
        elif effective_user_input and not is_utility_slash:
            session["pending_uncommitted_turn"] = True
            session["user_floor_already_appended"] = sidecar_floor_appended
            session["pending_turn_mode"] = "normal"
            session["stop_retry_count"] = 0
            session["last_user_input"] = effective_user_input
            if agentic_hands:
                contract_line = (
                    f"[AGENTIC HANDS UNLOCKED — AUTONOMOUS RP TURN]: Portray {char_name}'s reply in character "
                    f"(\"...\” and *...*). Switch B (Agentic Hands) is ON: you may autonomously invoke "
                    f"`st_manage_variables` (to update affection/status) or `st_evolve_lore_entry` (to record new "
                    f"plot facts into the worldbook) if warranted by the scene, then output ONLY {char_name}'s "
                    f"in-character prose. Do NOT call `st_commit_turn`—the Stop hook persists your final prose."
                )
            else:
                contract_line = (
                    f"[PURE IMMERSIVE RP TURN — ZERO TOOL CALLS (TOOLS PHYSICALLY UNPLUGGED)]: Output ONLY {char_name}'s "
                    "in-character reply (spoken dialogue in \"...\" and physical actions/inner thoughts in *...*). "
                    "Do NOT call `st_commit_turn` or any other tool, and output NO meta/engineering preamble—the host "
                    "Stop hook will automatically persist your prose to `.jsonl`."
                )
        else:
            session["pending_uncommitted_turn"] = False
            session["user_floor_already_appended"] = False
            contract_line = "[UTILITY INSPECTION TURN]: Execute the requested Tavern Fusion diagnostic/forge tool."

        if last_input:
            session["last_seen_host_input"] = last_input
        runtime.save_active_session(session)

        out = {
            "injectSteps": [
                {
                    "ephemeralMessage": f"{frame_info['ephemeral_frame']}\n{contract_line}"
                }
            ]
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception as exc:
        sys.stdout.write(
            json.dumps({
                "injectSteps": [{"ephemeralMessage": f"[Tavern Fusion Notice] Lore assembly skipped: {exc}"}]
            })
            + "\n"
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
