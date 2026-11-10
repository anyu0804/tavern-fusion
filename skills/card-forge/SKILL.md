---
name: card-forge
description: >-
  Creates, audits (Card Doctor), repairs, or upgrades Character Card V2/V3 PNG files using Seppä's lossless dual-chunk (ccv3 + chara) workflow. Use when the user types /card-forge or asks to inspect, lint, edit, or export a SillyTavern character card.
---
# Character Card V2/V3 Forge & Card Doctor (`/card-forge`)

1. **Run Card Doctor First**: Call `st_lint_card_and_lore(character_name=...)` and `st_read_card(card_filename=..., include_avatar_base64=true)` to inspect all V3 fields and check for common pitfalls (missing `ccv3` chunk, `system_prompt` wiping preset without `{{original}}`, `selective: false` secondary-key traps, leaked `creator_notes`, or oversized `mes_example`).
2. **Design or Repair (`Seppä` Workflow)**:
   - Keep `spec` as `"chara_card_v3"` and `spec_version` as `"3.0"`.
   - Include `{{original}}` in `system_prompt` if the card should layer atop user presets rather than replacing them.
   - Preserve the original author's prose voice when repairing existing cards.
3. **Atomic Dual-Chunk Export**: Call `st_build_export_card(card_v3_json=..., output_filename=..., portrait_image_path=...)` to write both `chara` (V2) and `ccv3` (V3) chunks and verify round-trip parse equivalence.
