---
name: dump-prompt
description: >-
  Inspects the exact assembled SillyTavern prompt stack (full OpenAI Settings prompt_order), Prefix Cache static vs. dynamic partition ratio, @D Lorebook injection depths, and budget-evicted entries. Use when the user types /dump-prompt or debugs prompt assembly.
---
# Assembled Prompt & Gate Inspector (`/dump-prompt`)

1. Call `st_dump_prompt_frame(character_name=..., user_input=..., token_budget=...)`.
2. Present the `dump_report` diagnostic breakdown clearly:
   - Static Prefix Cache ratio (`Slot 1` Preset `prompt_order` + Card System through `Slot 4` Constant Lore & Examples);
   - Dynamic `@ D` depth-spliced Lorebook entries with exact trigger reason (`constant`, `keyword`, `semantic(score)`, `recursive`) and `order` priority;
   - Any entries evicted due to Token budget exhaustion or `selective: true` gate suppression.
