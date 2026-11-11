"""Card Doctor minor-risk gate (`evaluate_minor_risk_gate`).

The implementation stays in `server/card_codec.py` (left untouched in this refactor because the
gate shares its regex tables with the card linter); this module is the policy-layer entry point
that the runtime, tools and hooks import from.
"""

from __future__ import annotations

from ..card_codec import evaluate_minor_risk_gate

__all__ = ["evaluate_minor_risk_gate"]
