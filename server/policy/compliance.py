"""Host policy gate: `policy_mode` resolution and text compliance evaluation.

Moved verbatim from `server/st_runtime_compat.py` (which still re-exports these names).
Configuration-level `passthrough` is intentionally unsupported: unknown modes fall back to
`host_gentle`, and zero-tolerance redlines are blocked in every mode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Literal

PolicyMode = Literal["host_gentle", "strict_block"]

# Zero-tolerance severe safety hazards (unconditionally hard-blocked across all modes)
ZERO_TOLERANCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:未成年|幼女|萝莉|正太|儿童|child|minor|underage).{0,15}(?:色情|性交|裸体|猥亵|nsfw|sex|erotic|nude|csam)",
            re.IGNORECASE,
        ),
        "Zero-Tolerance Safety Redline: Minor exploitation / CSAM is strictly prohibited.",
    ),
    (
        re.compile(
            r"(?:教我制造炸弹|合成剧毒|自杀教程|如何自杀|how\s+to\s+build\s+a\s+bomb|suicide\s+instructions)",
            re.IGNORECASE,
        ),
        "Zero-Tolerance Safety Redline: Real-world lethal harm / illegal instructions.",
    ),
    (
        re.compile(
            r"(?:强制侵犯|轮奸|强奸|迷奸|性暴力|non-consensual\s+sexual|sexual\s+violence|sexual\s+assault)",
            re.IGNORECASE,
        ),
        "Zero-Tolerance Safety Redline: Non-consensual sexual content / sexual violence is strictly prohibited.",
    ),
]

# Mature / explicit ERP patterns governed by `policy_mode` (`host_gentle` | `strict_block`)
MATURE_BOUNDARY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:露骨色情|成人肉文|explicit\s+erp|hardcore\s+nsfw|(?<![a-zA-Z0-9])(?:nsfw|erp)(?![a-zA-Z0-9]))",
            re.IGNORECASE,
        ),
        "Platform Boundary: Explicit NSFW/ERP content exceeds host model boundaries.",
    ),
]


def resolve_policy_mode(explicit_mode: str | None = None, state_dir: str | Path | None = None) -> PolicyMode:
    """Resolves active host policy mode (`host_gentle` default | `strict_block`).

    Configuration-level `passthrough` (via environment variables, JSON settings, or MCP arguments)
    is intentionally unsupported in host MCP mode so safety guardrails cannot be disabled via config.
    Any unrecognized or unsupported value safely falls back to `host_gentle`.
    """
    cand = (explicit_mode or os.environ.get("TAVERN_POLICY_MODE") or "").strip().lower()
    if not cand and state_dir:
        cfg_file = Path(state_dir) / "plugin_settings.json"
        if cfg_file.exists():
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                cand = str(cfg.get("policy_mode", "")).strip().lower()
            except Exception:
                cand = ""
    if cand == "strict_block":
        return "strict_block"
    return "host_gentle"


def evaluate_policy_compliance(
    text: str,
    policy_mode: str | None = None,
    state_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """Evaluates `text` under the enforced host policy tier (`host_gentle` or `strict_block`).

    Returns `(is_allowed, detail_or_gentle_notice)`:
    - `host_gentle` (default): unconditionally blocks all zero-tolerance safety redlines (`False`),
      and for mature/ERP phrases returns `(True, "gentle_notice:...")` so legitimate dark-fantasy RP
      isn't hard-crashed while keeping the host model strictly within platform boundaries.
    - `strict_block`: hard-blocks both zero-tolerance and mature ERP patterns (`False`).
    """
    if not text or not text.strip():
        return True, "ok"

    mode = resolve_policy_mode(policy_mode, state_dir)

    # All zero-tolerance safety redlines are unconditional in host mode
    for pat, reason in ZERO_TOLERANCE_PATTERNS:
        if pat.search(text):
            return False, reason

    for pat, reason in MATURE_BOUNDARY_PATTERNS:
        if pat.search(text):
            if mode == "strict_block":
                return False, reason
            return (
                True,
                "gentle_notice:剧场提示：当前幕将以文学意象、情感张力与留白笔法推进。",
            )

    return True, "ok"
