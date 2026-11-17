---
name: tavern-stage
description: >-
  Opens or manages the permanent right-pane Tavern Stage Sidecar workbench (interactive chat bubbles, Dimension Mixer, BYOK streaming, in-place floor editing, persistent Swipes, Fork checkpoints, reply suggestions, World Studio, Card Doctor, and --dump-prompt inspector) or compiles a fallback Generative UI HTML widget.
---
# Tavern Stage Workbench (`/tavern-stage`)

1. **Permanent Right-Pane Sidecar (Recommended)**:
   - When the `tavern-fusion` plugin is enabled, emit the clickable Aux Pane pill `[Tavern Stage](sidecar://tavern-fusion/tavern_stage/)` so the user can toggle the live interactive workbench in the right-hand pane.
   - Inside Tavern Stage, turns can run on Antigravity's built-in models via `sidecar_sdk` (`sendMessage` / `startConversation`) or an optional user-configured OpenAI-compatible BYOK endpoint (`generation_backend="byok"` with live streaming & cancellation), synchronizing bi-directionally with local `chats/**/*.jsonl`.
2. **Standalone Snapshot Widget (Optional)**:
   - If the user asks for an inline/artifact snapshot card inside chat, call `st_compile_stage_widget(character_name=..., placement="inline"|"standalone")` (`character_name` is required when no session is currently active) using the CSP-compliant gstatic Tailwind renderer.
