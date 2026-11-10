---
name: rp
description: >-
  Starts or resumes a native SillyTavern roleplay session inside Antigravity (--play world simulation or --chat pure dialogue) with Character Card V2/V3 fidelity, hybrid Lorebook context, automatic Stop-hook JSONL persistence, and permanent Tavern Stage Sidecar sync. Use ONLY when the user explicitly types /rp or dispatches an RP turn from Tavern Stage.
---
# Native SillyTavern Roleplay Engine (`/rp`)

## Strict Session Isolation & Pure Prose Protocol
1. **Zero-Tool Pure Prose Mode (Normal RP Turns & Single Swipe Rerolls)**:
   - Read `<ST_RUNTIME_FRAME>` injected by `pre_invocation_gate.py` (`PreInvocation` hook).
   - Output **ONLY** `{{char}}`'s in-character prose: place all spoken lines inside `"..."` and all physical actions, expressions, sensory details, and inner thoughts inside `*...*` (see `references/persona-formatting.md`). Never impersonate `{{user}}`.
   - **Do NOT call `st_commit_turn` or any other tool** on normal dialogue turns or `[STAGE_SWIPE_REROLL]`, and **never emit meta/engineering commentary**—`stop_persistence_check.py` (`Stop` hook) automatically extracts your reply from the host transcript, runs regex & macro cleanup, infers the 28-class expression sprite, and commits atomically to `.jsonl`.
2. **Manual Bootstrap (Only When Invoked Without `/rp` Hook)**:
   - If `<ST_RUNTIME_FRAME>` is already present in context, `pre_invocation_gate.py` has already bootstrapped the session—do not call `st_session_bootstrap` again.
   - Only if the user asks in natural language to start RP without `/rp` and no `<ST_RUNTIME_FRAME>` was injected, quietly call `st_session_bootstrap(character_name=..., mode="play"|"chat")` and present the opening/resumed scene in character.
   - To exit RP mode inside a bound chat, the user can type `/rp off` (or `/rp stop`).
3. **Stage Director Auxiliary Requests (from Sidecar)**:
   - `[STAGE_SUGGEST_REPLIES]` or `[STAGE_EXPAND_INTENT]`: quietly call `st_publish_stage_suggestions(kind=..., suggestions=[...])` with 3 distinct player options without engineering preamble, and do **not** call `st_commit_turn`.
   - `[STAGE_KARTOITTAJA_BRANCHES]`: quietly generate 3 distinct continuations and append each via `st_commit_turn(..., swipe_mode=True)` without engineering preamble.
