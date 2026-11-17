---
name: lore-weaver
description: >-
  Queries, diagnoses, authors, and evolves SillyTavern World Info / Lorebook JSON files using hybrid keyword/regex gates and semantic similarity. Use when the user types /lore-weaver or asks to test or update world lore.
---
# Dynamic Lorebook Weaver (`/lore-weaver`)

1. **Test Recall & Gate Logic**: Call `st_query_lorebook(lorebook_names=[...], query_text=..., token_budget=2048)` and `st_lint_card_and_lore(lorebook_name=...)` to inspect which entries trigger via `constant`, `keyword`, `semantic`, or `recursive` paths, and check whether `selective: true` secondary gates (`AND_ANY`, `AND_ALL`, `NOT_ANY`, `NOT_ALL`) or token budgets filtered any entry.
2. **Evolve World State (`Dramaturgi` Workflow)**:
   - Ensure each entry `content` is self-contained (SillyTavern does not inject `key` or `comment` titles into the model prompt).
   - Set `selective: true` whenever `keys_secondary` should gate activation.
   - Call `st_evolve_lore_entry(lorebook_name=..., operation="upsert"|"toggle"|"archive"|"delete", entry_payload=...)` (supporting `uid`, `comment`, `keys`, `keys_secondary`, `selective`, `selective_logic`, `position`, `depth`, `order`, `constant`, `enabled`, `content`) to persist changes atomically to `worlds/<name>.json`.
