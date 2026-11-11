"""Policy layer: host policy gate (`policy_mode`) and the Card Doctor minor-risk gate.

Stdlib-only and free of runtime/session state so lifecycle hooks can import it cheaply.
"""

from __future__ import annotations

from .compliance import (
    MATURE_BOUNDARY_PATTERNS,
    ZERO_TOLERANCE_PATTERNS,
    PolicyMode,
    evaluate_policy_compliance,
    resolve_policy_mode,
)
from .minor_gate import evaluate_minor_risk_gate

__all__ = [
    "MATURE_BOUNDARY_PATTERNS",
    "ZERO_TOLERANCE_PATTERNS",
    "PolicyMode",
    "evaluate_minor_risk_gate",
    "evaluate_policy_compliance",
    "resolve_policy_mode",
]
