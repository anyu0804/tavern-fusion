"""Runtime core: paths/settings (`context`), session state (`session_store`), first-run notice."""

from __future__ import annotations

from .context import DEFAULT_STATE_DIR, ROOT_DIR, RuntimeContext, resolve_data_root, resolve_state_dir
from .notice import FIRST_RUN_NOTICE, FIRST_RUN_NOTICE_V1, FIRST_RUN_NOTICE_VERSION
from .session_store import ACTIVE_SESSION_FILENAME, SessionStore

__all__ = [
    "ACTIVE_SESSION_FILENAME",
    "DEFAULT_STATE_DIR",
    "FIRST_RUN_NOTICE",
    "FIRST_RUN_NOTICE_V1",
    "FIRST_RUN_NOTICE_VERSION",
    "ROOT_DIR",
    "RuntimeContext",
    "SessionStore",
    "resolve_data_root",
    "resolve_state_dir",
]
