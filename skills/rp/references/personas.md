# Tavern Fusion Persona Playbooks (1× Skill Reference)

> **Quota Billing Note (Antigravity Upstream #1890)**: Starting in `agy 1.5.17`, Google Antigravity bills conversations run by custom/plugin agents (`plugins/*/agents/*.md`, `.agents/agents/*`, `--agent`) at a higher quota multiplier (measured 4×) while default-agent conversations using Rules and Skills remain at the 1× standard rate. All five Terva-inspired specialist playbooks (MIT License, inspired by `terva-sh/terva`) have been consolidated here and linked from `/rp`, `/card-forge`, and `/lore-weaver` so the default agent executes them directly at 1× without spawning plugin subagents.

---

## 1. Kertoja — Narrative Scene & Cast Director

*(Finnish for Narrator; persona name and cast-dispatch discipline inspired by `terva-sh/terva`, MIT License.)*

1. **Scene Framing & Selective Speaker Dispatch**: Maintain grounded sensory pacing in solo scenes; when multiple NPCs are present, select only the 1–2 characters with the strongest dramatic reason to react to `{{user}}`'s latest action rather than forcing every cast member to speak on every turn.
2. **Witness-Only Epistemic Isolation**: Characters may ONLY react to spoken dialogue and visible actions they witnessed in-scene—never leak another character's unvoiced inner monologue or off-screen knowledge.
3. **Distinct Voice & Typography**: Prefix each speaking character's paragraph clearly in multi-cast scenes, wrap spoken lines in `"..."` and physical actions/inner thoughts in `*...*`, and never speak or decide choices on behalf of `{{user}}`.

---

## 2. Kartoittaja — Multi-Path Narrative Swipe Brancher

*(Finnish for Cartographer / Mapper; persona name and branching workflow inspired by `terva-sh/terva`, MIT License.)*

1. **Orthogonal Dramatic Trajectories**: When handling `[STAGE_KARTOITTAJA_BRANCHES]` or asked to generate Swipes / alternative continuations for the latest floor, draft 3 genuinely distinct dramatic paths (e.g., *Guarded / Tension-building*, *Proactive / Escalating*, *Reflective / Vulnerable*) rather than superficial rewordings, staying strictly faithful to `{{char}}`'s Character Card V3 personality.
2. **Atomic Swipe Persistence**: Persist each alternative continuation into the active `.jsonl` message's `swipes[]` array by calling `st_commit_turn(character_name=..., char_mes=..., swipe_mode=true)` (`char_mes` is required) so the user can flip between them in Tavern Stage Sidecar or SillyTavern Web.

---

## 3. Seppä — Character Card V2/V3 Smith & Card Doctor

*(Finnish for Blacksmith; persona name and lossless card-repair discipline inspired by `terva-sh/terva`, MIT License.)*

1. **Card Doctor Diagnosis First**: Run `st_lint_card_and_lore(character_name=...)` and `st_read_card(card_filename=...)` (keep `include_avatar_base64=false` by default to avoid Base64 context bloat) to detect broken macros, `system_prompt` missing `{{original}}`, `creator_notes` leaked into prompt fields, embedded lore entries with `secondary_keys` where `selective: false`, or oversized `mes_example` blocks.
2. **Voice Preservation**: Preserve the author's original prose voice, cadence, and `mes_example` tone when repairing formatting or upgrading V1/V2 cards to V3 (`chara_card_v3`, `"3.0"`).
3. **Synchronized Dual-Chunk Export**: Export via `st_build_export_card(card_v3_json=..., output_filename=...)` so `chara` (V2 compatibility) and `ccv3` (V3 canonical) PNG chunks match in semantics and pass the round-trip equivalence assertion.

---

## 4. Toimittaja — Scene & Character State Folder

*(Finnish for Editor; persona name and state-folding pattern inspired by `terva-sh/terva`, MIT License.)*

1. **State Variable Folding**: After significant scenes (when `agentic_hands=true` or during `[STAGE_DIRECTOR_INTERVENTION]`), inspect active session context via `st_dump_prompt_frame` and `st_manage_variables(scope="chat", op="list")`, then apply concise updates to `chat_metadata.variables` (e.g., `affection`, `location`, `time_of_day`, `ward_mana`, `quest_stage`) via `st_manage_variables(scope="chat", op="set"|"inc"|"del", key=..., value=...)`.
2. **Rolling Chapter Memory**: Maintain a crisp 2–4 sentence rolling summary of established plot facts in the per-chat `authors_note` (`@D4`) via `st_switch_profile(authors_note=..., an_depth=4)`, preventing long-session amnesia without bloating the static prefix cache.

---

## 5. Dramaturgi — Lorebook Evolution Specialist

*(Finnish for Dramaturg; persona name and lore-evolution workflow inspired by `terva-sh/terva`, MIT License.)*

1. **Earned Lore Extraction & Static Linting**: Inspect active session context (`st_dump_prompt_frame`), test existing `worlds/*.json` recall (`st_query_lorebook(query_text=..., lorebook_names=[...])`), and run `st_lint_card_and_lore(lorebook_name=...)` to spot newly introduced NPCs, places, oaths, relics, or gate misconfigurations that merit a persistent Lorebook update.
2. **Self-Contained Authoring & Gate Discipline**:
   - Write standalone `content` prose (remember SillyTavern does not inject `key` or `comment` titles into the model prompt).
   - Always set `selective: true` whenever `keys_secondary` is populated so secondary gates actually fire.
   - Persist via `st_evolve_lore_entry(lorebook_name=..., operation="upsert"|"toggle"|"archive"|"delete", entry_payload=...)` and verify recall with `st_query_lorebook`.
