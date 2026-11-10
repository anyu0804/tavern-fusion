"""Filesystem containment and `fsync` atomic write primitives (`fs_safety.py`).

Combines strict directory containment checks (`ensure_under_dir`) with
Windows-resilient `fsync` + `os.replace` atomic file replacement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any


def sanitize_filename_stem(raw_name: str) -> str:
    """Sanitizes an arbitrary string into a safe single-component filename stem."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(raw_name).strip())
    cleaned = cleaned.rstrip(". ").strip()
    if not cleaned or cleaned in {".", ".."} or ".." in cleaned:
        raise ValueError(f"Invalid filename stem after sanitization: {raw_name!r}")
    return cleaned


def ensure_under_dir(target_path: str | Path, base_dir: str | Path) -> Path:
    """Ensures `target_path` resolves strictly inside `base_dir` (blocks traversal/symlinks)."""
    resolved_base = Path(base_dir).resolve()
    resolved_target = Path(target_path).resolve()
    if not resolved_target.is_relative_to(resolved_base):
        raise ValueError(
            f"Path containment violation: '{resolved_target.name}' escapes '{resolved_base.name}'."
        )
    return resolved_target


def atomic_write_bytes(target_path: str | Path, payload: bytes) -> Path:
    """Atomically writes `payload` via a same-directory temporary file + `fsync` + `os.replace`."""
    dst = Path(target_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dst.parent),
        prefix=f".{dst.stem}_",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        # Retry briefly on Windows in case a reader holds a transient share handle
        last_err: OSError | None = None
        for attempt in range(5):
            try:
                os.replace(tmp_path, dst)
                last_err = None
                break
            except PermissionError as err:
                last_err = err
                time.sleep(0.03 * (attempt + 1))
        if last_err is not None:
            raise last_err
        return dst
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(target_path: str | Path, text: str) -> Path:
    """Atomically writes UTF-8 text to `target_path`."""
    return atomic_write_bytes(target_path, text.encode("utf-8"))


def atomic_write_json(target_path: str | Path, data: Any, indent: int = 2) -> Path:
    """Atomically writes UTF-8 JSON to `target_path`."""
    serialized = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    return atomic_write_text(target_path, serialized)
