"""Prompt layer: SillyTavern macros, settings/persona, presets, regex pipeline, prompt frame compiler."""

from __future__ import annotations

from .epistemic import (
    SYSTEM_INSTRUCTION_BY_EPISTEMIC,
    EpistemicLevel,
    normalize_epistemic_level,
)
from .frame import compile_st_prompt_frame, format_lexical_dialogue_examples
from .macros import resolve_st_macros
from .presets import DEFAULT_RP_MAIN_PROMPT, list_openai_presets, load_openai_preset
from .regex_pipeline import BUILTIN_IMMERSION_GUARD_REGEX, run_regex_pipeline
from .st_settings import get_active_persona, load_st_settings

__all__ = [
    "BUILTIN_IMMERSION_GUARD_REGEX",
    "DEFAULT_RP_MAIN_PROMPT",
    "SYSTEM_INSTRUCTION_BY_EPISTEMIC",
    "EpistemicLevel",
    "compile_st_prompt_frame",
    "format_lexical_dialogue_examples",
    "get_active_persona",
    "list_openai_presets",
    "load_openai_preset",
    "load_st_settings",
    "normalize_epistemic_level",
    "resolve_st_macros",
    "run_regex_pipeline",
]
