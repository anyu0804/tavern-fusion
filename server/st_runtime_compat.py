"""SillyTavern runtime compatibility facade (`st_runtime_compat.py`).

Backwards-compatible re-export shim. The implementation moved in the v3.1 refactor:
- macros, settings/persona, presets, regex pipeline, prompt frame compiler -> `server/prompt/`
- host policy gate (`policy_mode`, `evaluate_policy_compliance`)            -> `server/policy/`
Every name that used to be importable from here still is.
"""

from __future__ import annotations

from .lore_engine import estimate_tokens  # noqa: F401
from .policy.compliance import (  # noqa: F401
    MATURE_BOUNDARY_PATTERNS,
    ZERO_TOLERANCE_PATTERNS,
    PolicyMode,
    evaluate_policy_compliance,
    resolve_policy_mode,
)
from .prompt.epistemic import (  # noqa: F401
    SYSTEM_INSTRUCTION_BY_EPISTEMIC,
    EpistemicLevel,
    normalize_epistemic_level,
)
from .prompt.frame import compile_st_prompt_frame, format_lexical_dialogue_examples  # noqa: F401
from .prompt.macros import resolve_st_macros  # noqa: F401
from .prompt.presets import (  # noqa: F401
    DEFAULT_RP_MAIN_PROMPT,
    list_openai_presets,
    load_openai_preset,
)
from .prompt.regex_pipeline import (  # noqa: F401
    _JS_REPL_TOKEN_RE,
    BUILTIN_IMMERSION_GUARD_REGEX,
    _apply_js_regex_replacement,
    run_regex_pipeline,
)
from .prompt.st_settings import get_active_persona, load_st_settings  # noqa: F401
