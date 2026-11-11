"""Session state store (`active_session.json`): the active character/chat binding and `rp_conv_id`.

Every read and write of the RP session state goes through `SessionStore`. The API is shaped for
v3.1 multi-session (methods take a conversation id), but v3.0 semantics are unchanged: there is
exactly ONE session file, bound to at most one host conversation via its `rp_conv_id` field.

- `load()` / `save()` / `update()` are the raw locked primitives (same lock + atomic write as
  before the refactor).
- `active()` returns the single session dict, `get(conv_id)` returns it only when it is bound to
  `conv_id` (or when `conv_id` is None), `bind()` / `unbind()` change the binding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..chat_sync import exclusive_file_lock
from ..fs_safety import atomic_write_json

ACTIVE_SESSION_FILENAME = "active_session.json"


class SessionStore:
    """Locked JSON store for the RP session state under the plugin `state_dir`."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    @property
    def path(self) -> Path:
        return self.state_dir / ACTIVE_SESSION_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    # ------------------------------------------------------------------
    # Raw locked primitives
    # ------------------------------------------------------------------
    def read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def load(self) -> dict[str, Any]:
        with exclusive_file_lock(self.path):
            return self.read_unlocked()

    def save(self, session: dict[str, Any]) -> None:
        with exclusive_file_lock(self.path):
            atomic_write_json(self.path, session, indent=2)

    def update(
        self,
        updates: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Locked Read-Modify-Write update on `active_session.json` preventing lost updates."""
        with exclusive_file_lock(self.path):
            cur = self.read_unlocked()
            if callable(updates):
                maybe_next = updates(cur)
                if isinstance(maybe_next, dict):
                    cur = maybe_next
            else:
                cur.update(updates)
            atomic_write_json(self.path, cur, indent=2)
            return cur

    # ------------------------------------------------------------------
    # Conversation-keyed API (single-session semantics in v3.0)
    # ------------------------------------------------------------------
    def active(self) -> dict[str, Any]:
        """The one session (bound or not). v3.1 will return the session of the focused conversation."""
        return self.load()

    def get(self, conv_id: str | None = None) -> dict[str, Any]:
        """The session bound to `conv_id`, or `{}` when it is bound elsewhere. `None` -> `active()`."""
        sess = self.load()
        if conv_id is None:
            return sess
        return sess if str(sess.get("rp_conv_id") or "") == str(conv_id) else {}

    def is_bound(self, conv_id: str) -> bool:
        """True when `conv_id` is non-empty, the session is active and bound to it."""
        if not conv_id:
            return False
        sess = self.load()
        return bool(sess.get("active")) and str(sess.get("rp_conv_id") or "") == conv_id

    def bind(self, conv_id: str, **fields: Any) -> dict[str, Any]:
        """Binds the session to `conv_id` (activating it when non-empty) and merges `fields`."""
        upd: dict[str, Any] = {"rp_conv_id": conv_id}
        if conv_id:
            upd["active"] = True
        upd.update(fields)
        return self.update(upd)

    def unbind(
        self,
        conv_id: str | None = None,
        mutate: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Applies `mutate` (or clears the binding) to the session. `conv_id` is reserved for v3.1."""
        if mutate is not None:
            return self.update(mutate)
        return self.update({"active": False, "rp_conv_id": ""})
