"""Shared card / lore / path helpers used by several tool modules (moved from TavernFusionRuntime)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..card_codec import (
    read_character_card,
)
from ..fs_safety import ensure_under_dir
from ..lore_engine import (
    NormalizedLoreEntry,
    load_lorebook_file,
    parse_lorebook_dict,
)
from ..prompt import (
    load_st_settings,
)
from ..runtime.context import RuntimeContext


def find_character_card_path(ctx: RuntimeContext, character_query: str) -> Path:
    char_dir = ctx.st_data_root / "characters"
    char_dir.mkdir(parents=True, exist_ok=True)
    q_clean = character_query.strip()
    if ".." in q_clean or "/" in q_clean or "\\" in q_clean:
        # Allow full path ONLY if strictly inside char_dir
        cand_abs = Path(q_clean)
        if cand_abs.is_absolute() and cand_abs.exists():
            return ensure_under_dir(cand_abs, char_dir)
        raise ValueError("Invalid character query containing path separators.")

    direct = char_dir / q_clean
    if direct.exists() and direct.is_file():
        return ensure_under_dir(direct, char_dir)
    for ext in (".png", ".json"):
        cand = char_dir / f"{q_clean}{ext}"
        if cand.exists():
            return ensure_under_dir(cand, char_dir)
        prefixed = char_dir / f"default_{q_clean}{ext}"
        if prefixed.exists():
            return ensure_under_dir(prefixed, char_dir)

    q_low = q_clean.lower()
    for p in sorted(char_dir.iterdir(), key=lambda item: (item.name.lower(), item.name)):
        if p.is_file() and p.suffix.lower() in (".png", ".json"):
            if q_low in p.stem.lower():
                return p
            try:
                c_name = read_character_card(p, include_avatar_data_uri=False)["card"]["data"]["name"]
                if q_low == c_name.lower() or q_low in c_name.lower():
                    return p
            except Exception:
                continue
    raise FileNotFoundError(f"Character card matching '{character_query}' not found in {char_dir}.")


def collect_merged_lore_entries(
    ctx: RuntimeContext,
    card_v3: dict[str, Any],
    extra_books: list[str] | None = None,
) -> list[NormalizedLoreEntry]:
    entries: list[NormalizedLoreEntry] = []
    data = card_v3.get("data", {})
    if isinstance(data.get("character_book"), dict):
        entries.extend(parse_lorebook_dict(data["character_book"], book_name="character_book", source_tier=2))

    ext = data.get("extensions", {}) if isinstance(data.get("extensions"), dict) else {}
    linked_world = str(ext.get("world", "")).strip()
    book_names: list[str] = []
    if linked_world:
        book_names.append(linked_world)
    if extra_books:
        for b in extra_books:
            if b and b not in book_names:
                book_names.append(b)

    worlds_dir = ctx.st_data_root / "worlds"
    if not book_names and worlds_dir.exists():
        settings = load_st_settings(ctx.st_data_root)
        # SillyTavern stores this at `world_info_settings.world_info.globalSelect`;
        # top-level `world_info.globalSelect` is kept as a fallback for older mock data.
        wi_settings = settings.get("world_info_settings")
        wi_block = wi_settings.get("world_info") if isinstance(wi_settings, dict) else None
        if not isinstance(wi_block, dict):
            wi_block = settings.get("world_info") if isinstance(settings.get("world_info"), dict) else {}
        selected_global = wi_block.get("globalSelect", []) or []
        if selected_global:
            book_names.extend(selected_global)
        else:
            book_names.extend(
                p.stem for p in sorted(worlds_dir.glob("*.json"), key=lambda item: (item.name.lower(), item.name))[:5]
            )

    for b_name in book_names:
        fname = b_name if b_name.endswith(".json") else f"{b_name}.json"
        b_path = worlds_dir / fname
        if b_path.exists():
            tier = 2 if b_name == linked_world else 3
            try:
                entries.extend(load_lorebook_file(b_path, source_tier=tier))
            except Exception:
                # One corrupted/invalid world book must never kill the whole prompt frame
                continue
    return entries


def resolve_card_and_identity(
    ctx: RuntimeContext,
    char_query: str,
    session: dict[str, Any] | None = None,
    *,
    include_avatar_data_uri: bool = False,
) -> tuple[Path, dict[str, Any], str, str]:
    """Resolves `(card_path, card_bundle, char_name, avatar_stem)`.

    When `char_query` refers to the currently bound character in `session` and
    `session["card_file"]` exists on disk, prefers that exact PNG file so cards
    whose filename differs from `data.name` stay deterministically paired.
    """
    q_clean = str(char_query).strip()
    card_path: Path | None = None
    if session:
        sess_file = str(session.get("card_file") or "").strip()
        sess_char = str(session.get("character_name") or "").strip()
        sess_stem = str(session.get("avatar_stem") or "").strip()
        if sess_file and q_clean in (sess_char, sess_file, sess_stem):
            cand = (ctx.st_data_root / "characters" / sess_file).resolve()
            if cand.exists() and cand.is_file():
                card_path = ensure_under_dir(cand, ctx.st_data_root / "characters")
    if card_path is None:
        card_path = find_character_card_path(ctx, q_clean)
    bundle = read_character_card(card_path, include_avatar_data_uri=include_avatar_data_uri)
    char_name = str(bundle["card"]["data"]["name"])
    avatar_stem = card_path.stem
    return card_path, bundle, char_name, avatar_stem


def validate_contained_path(
    ctx: RuntimeContext,
    raw_path: str | Path,
    *,
    default_subdir: str | None = None,
    allowed_suffixes: tuple[str, ...] = (),
    label: str = "path",
) -> Path:
    raw_str = str(raw_path).strip()
    if not raw_str:
        raise ValueError(f"{label} cannot be empty.")
    cand = Path(raw_str)
    if ".." in cand.parts:
        raise ValueError(f"Path traversal ('..') is forbidden in {label}.")
    if allowed_suffixes and cand.suffix.lower() not in allowed_suffixes:
        raise ValueError(f"{label} must have one of suffixes {allowed_suffixes}, got '{cand.suffix}'.")

    allowed_roots = [
        ctx.state_dir.resolve(),
        ctx.st_data_root.resolve(),
        Path.cwd().resolve(),
    ]
    if not cand.is_absolute() and not cand.root:
        base = (ctx.state_dir / default_subdir).resolve() if default_subdir else ctx.state_dir.resolve()
        base.mkdir(parents=True, exist_ok=True)
        return ensure_under_dir(base / cand, base)

    resolved = cand.resolve()
    for root in allowed_roots:
        if resolved.is_relative_to(root):
            return resolved
    raise ValueError(
        f"Path containment violation: {label} '{resolved.name}' must reside inside "
        "plugin state_dir, SillyTavern data root, or the current workspace."
    )
