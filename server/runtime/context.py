"""Runtime context: plugin paths, `state_dir`, SillyTavern data-root resolution and plugin settings.

`RuntimeContext` is the light-weight core shared by the MCP server, the sidecar control plane and
the lifecycle hooks, and the `ctx` argument of every tool handler in `server.tools`. The lore
vector cache (SQLite) is opened lazily on first use (`TavernFusionRuntime` opens it eagerly, as
before), so hooks that only read or write session state stay cheap.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from ..chat_sync import exclusive_file_lock
from ..fs_safety import atomic_write_json
from ..policy.compliance import resolve_policy_mode
from .notice import FIRST_RUN_NOTICE_VERSION
from .session_store import SessionStore

# Plugin root (the directory holding plugin.json / mcp_config.json / hooks.json).
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

DEFAULT_STATE_DIR = Path.home() / ".gemini" / "antigravity" / "tavern-fusion" / "state"

PLUGIN_SETTINGS_FILENAME = "plugin_settings.json"
STAGE_SUGGESTIONS_FILENAME = "stage_suggestions.json"


def resolve_state_dir(state_dir: str | Path | None = None) -> Path:
    """`state_dir` arg > `PLUGIN_STATE_DIR` env > default. Unexpanded `${PLUGIN_DATA}` counts as unset."""
    raw_state = state_dir or os.environ.get("PLUGIN_STATE_DIR") or ""
    if str(raw_state) in ("${PLUGIN_DATA}", ""):
        raw_state = DEFAULT_STATE_DIR
    return Path(raw_state).resolve()


def resolve_data_root(explicit: str | Path | None, state_dir: Path | None = None) -> Path:
    """Resolves the SillyTavern `default-user` directory (explicit > env > remembered > workspace > mock)."""
    remembered: str | None = None
    if state_dir is not None:
        settings_file = state_dir / PLUGIN_SETTINGS_FILENAME
        if settings_file.exists():
            try:
                s_obj = json.loads(settings_file.read_text(encoding="utf-8"))
                if isinstance(s_obj, dict) and s_obj.get("last_st_data_root"):
                    remembered = str(s_obj["last_st_data_root"])
            except Exception:
                remembered = None

    candidates = [
        explicit,
        os.environ.get("ST_DATA_ROOT"),
        os.environ.get("ST_USER_DATA_DIR"),
        remembered,
        Path.cwd() / "data" / "default-user",
        Path.cwd() / "SillyTavern" / "data" / "default-user",
        ROOT_DIR.parent / "SillyTavern" / "data" / "default-user",
        ROOT_DIR.parent / "data" / "default-user",
        ROOT_DIR / "examples" / "mock_st_data" / "default-user",
    ]
    for c in candidates:
        if c and not str(c).startswith("${"):
            p = Path(c)
            if p.exists() and p.is_dir():
                return p.resolve()
    if explicit and not str(explicit).startswith("${"):
        p = Path(explicit)
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()
    fallback = ROOT_DIR / "examples" / "mock_st_data" / "default-user"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve()


class RuntimeContext:
    """Paths + settings + session store for one plugin process (MCP server, CLI, sidecar op or hook)."""

    def __init__(self, st_data_root: str | Path | None = None, state_dir: str | Path | None = None) -> None:
        self._explicit_data_root = st_data_root
        self.state_dir = resolve_state_dir(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.st_data_root = self._resolve_data_root(st_data_root, self.state_dir)
        self.sessions = SessionStore(self.state_dir)
        self._lore_engine: Any = None

    @property
    def lore_engine(self) -> Any:
        """`HybridLoreEngine` backed by `state_dir/lore_vectors.sqlite`, opened on first use."""
        if self._lore_engine is None:
            from ..lore_engine import HybridLoreEngine

            self._lore_engine = HybridLoreEngine(self.state_dir / "lore_vectors.sqlite")
        return self._lore_engine

    @lore_engine.setter
    def lore_engine(self, engine: Any) -> None:
        self._lore_engine = engine

    def close(self) -> None:
        """Releases held resources (the lore vector cache, if it was opened)."""
        if self._lore_engine is not None:
            self._lore_engine.close()

    _resolve_data_root = staticmethod(resolve_data_root)

    def refresh_data_root(self) -> None:
        """Re-resolves `st_data_root` when it was not given explicitly (picks up a newly remembered root)."""
        if self._explicit_data_root is None:
            self.st_data_root = self._resolve_data_root(None, self.state_dir)

    # ------------------------------------------------------------------
    # State file locations
    # ------------------------------------------------------------------
    @property
    def active_session_file(self) -> Path:
        return self.sessions.path

    @property
    def plugin_settings_file(self) -> Path:
        return self.state_dir / PLUGIN_SETTINGS_FILENAME

    @property
    def stage_suggestions_file(self) -> Path:
        return self.state_dir / STAGE_SUGGESTIONS_FILENAME

    # ------------------------------------------------------------------
    # Session state (compat wrappers around `SessionStore`)
    # ------------------------------------------------------------------
    def _read_active_session_unlocked(self) -> dict[str, Any]:
        return self.sessions.read_unlocked()

    def load_active_session(self) -> dict[str, Any]:
        return self.sessions.load()

    def save_active_session(self, session: dict[str, Any]) -> None:
        self.sessions.save(session)

    def update_active_session(
        self,
        updates: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Locked Read-Modify-Write update on `active_session.json` preventing lost updates."""
        return self.sessions.update(updates)

    # ------------------------------------------------------------------
    # Plugin settings (`plugin_settings.json`)
    # ------------------------------------------------------------------
    def _read_plugin_settings_unlocked(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "policy_mode": resolve_policy_mode(state_dir=self.state_dir),
            "inference_channel": "antigravity_builtin",
            "first_run_notice_ack_v1": False,
            "first_run_cli_shown_v1": False,
        }
        if not self.plugin_settings_file.exists():
            return defaults
        try:
            loaded = json.loads(self.plugin_settings_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except Exception:
            pass
        defaults["policy_mode"] = resolve_policy_mode(str(defaults.get("policy_mode", "")))
        # `first_run_notice_ack_v1` / `first_run_cli_shown_v1` keep their historical names for
        # UI/test compatibility, but now mean "the CURRENT notice version was acknowledged/shown".
        # Settings written before versioning count as version 1, so a materially changed notice
        # (version bump) is shown again once.
        ack_ver = int(defaults.get("first_run_notice_ack_version") or (1 if defaults.get("first_run_notice_ack_v1") else 0))
        shown_ver = int(defaults.get("first_run_cli_shown_version") or (1 if defaults.get("first_run_cli_shown_v1") else 0))
        defaults["first_run_notice_ack_version"] = ack_ver
        defaults["first_run_cli_shown_version"] = shown_ver
        defaults["first_run_notice_ack_v1"] = ack_ver >= FIRST_RUN_NOTICE_VERSION
        defaults["first_run_cli_shown_v1"] = shown_ver >= FIRST_RUN_NOTICE_VERSION
        return defaults

    def load_plugin_settings(self) -> dict[str, Any]:
        with exclusive_file_lock(self.plugin_settings_file):
            return self._read_plugin_settings_unlocked()

    def save_plugin_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        with exclusive_file_lock(self.plugin_settings_file):
            cur = self._read_plugin_settings_unlocked()
            cur.update(updates)
            cur["policy_mode"] = resolve_policy_mode(str(cur.get("policy_mode", "")))
            atomic_write_json(self.plugin_settings_file, cur, indent=2)
            return cur
