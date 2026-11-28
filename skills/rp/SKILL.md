---
name: rp
description: >-
  Starts or resumes a native SillyTavern roleplay session inside Antigravity (--play world simulation or --chat pure dialogue) with Character Card V2/V3 fidelity, Dimension Mixer controls, hybrid Lorebook context, automatic Stop-hook JSONL persistence, and permanent Tavern Stage Sidecar sync. Use ONLY when the user explicitly types /rp or dispatches an RP turn from Tavern Stage.
---
# Native SillyTavern Roleplay Engine (`/rp`)

## Strict Session Isolation & Dimension Mixer Protocol
1. **Immersive Prose & Switch B (`agentic_hands`) Discipline**:
   - Read `<ST_RUNTIME_FRAME>` and its trailing turn contract injected by `pre_invocation_gate.py` (`PreInvocation` hook).
   - **Default Pure Theater Mode (`agentic_hands=false`)**: Output **ONLY** `{{char}}`'s in-character prose—place spoken lines inside `"..."` and physical actions, expressions, sensory details, and inner thoughts inside `*...*` (see `references/persona-formatting.md` and `Kertoja` cast/witness-isolation rules in `references/personas.md`). Never impersonate `{{user}}`, **do NOT call `st_commit_turn` or any other tool** on normal turns or `[STAGE_SWIPE_REROLL]`, and emit zero engineering commentary (`stop_persistence_check.py` commits your reply from the host transcript automatically).
   - **Agentic Hands Unlocked (`agentic_hands=true`)**: When `<ST_RUNTIME_FRAME>` carries `[AGENTIC HANDS UNLOCKED]`, you may autonomously call `st_manage_variables` or `st_evolve_lore_entry` (following `Toimittaja` / `Dramaturgi` rules in `references/personas.md`) if warranted by scene events, then output **ONLY** `{{char}}`'s in-character prose without calling `st_commit_turn`.
2. **Manual Bootstrap (Only When Invoked Without `/rp` Hook)**:
   - If `<ST_RUNTIME_FRAME>` is already present in context, `pre_invocation_gate.py` has already bootstrapped the session—do not call `st_session_bootstrap` again.
   - Only if the user asks in natural language to start RP without `/rp` and no `<ST_RUNTIME_FRAME>` was injected, quietly call `st_session_bootstrap(character_name=..., mode="play"|"chat")` and present the opening/resumed scene in character.
   - To exit RP mode inside a bound chat, the user can type `/rp off` (or `/rp stop`).
3. **Stage Director Auxiliary & Backstage Requests (from Sidecar)**:
   - `[STAGE_SUGGEST_REPLIES]` or `[STAGE_EXPAND_INTENT]`: quietly call `st_publish_stage_suggestions(kind="reply_suggestions"|"expanded_intent", suggestions=[...])` with 3 distinct player options without engineering preamble, and do **not** call `st_commit_turn`.
   - `[STAGE_KARTOITTAJA_BRANCHES]`: quietly generate 3 distinct continuations (following `Kartoittaja` in `references/personas.md`) and append each via `st_commit_turn(character_name=..., char_mes=..., swipe_mode=True)` without engineering preamble.
   - `[STAGE_DIRECTOR_INTERVENTION]` (Switch C Backstage Director Shuttle): collaborate backstage on plot direction, floor edits, card repairs, or lorebook updates (`st_stage_floor_op`, `st_build_export_card`, `st_evolve_lore_entry`, `st_manage_variables`, `st_switch_profile` per `references/personas.md`); do **not** call `st_commit_turn` (`Stop` hook persists the backstage reply with `extra.is_backstage=true` so it is quarantined upon returning to 0%/50% immersion).
