"""Bi-directional SillyTavern `.jsonl` Chat Persistence, Floor Revision, Swipes & Branch Engine (`chat_sync.py`).

Directly reads and writes `data/default-user/chats/<Character_Name>/<Timestamp>.jsonl`
(plus `groups/*.json` metadata discovery) with Windows-safe OS-level exclusive locking
and `fsync` atomic replacement.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Iterator, Literal
import urllib.error
import urllib.request
import uuid

from .fs_safety import atomic_write_text, ensure_under_dir, sanitize_filename_stem

try:
    import fcntl  # Unix / macOS
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore


def format_st_timestamp(dt: datetime | None = None) -> str:
    """Formats timestamp compatible with SillyTavern `.jsonl` filenames & `create_date`."""
    now = dt or datetime.now()
    return now.strftime("%Y-%m-%d@%Hh%Mm%Ss")


def format_human_send_date(dt: datetime | None = None) -> str:
    now = dt or datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def exclusive_file_lock(lock_target: Path, timeout_sec: float = 5.0) -> Iterator[None]:
    """Cross-platform exclusive lock preventing race conditions on Windows & POSIX.

    On Windows, `msvcrt.locking` locks byte ranges relative to the current file
    pointer—so we always seek to offset 0 before both locking and unlocking.
    """
    lock_path = lock_target.with_suffix(lock_target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    locked = False
    try:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"L")
            fh.flush()

        deadline = time.monotonic() + timeout_sec
        while True:
            try:
                fh.seek(0)
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:  # pragma: no cover
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock on {lock_path.name}")
                time.sleep(0.025)
        yield
    finally:
        if locked:
            try:
                fh.seek(0)
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fh.close()
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def notify_st_web_bridge(
    event_type: str,
    payload: dict[str, Any],
    st_web_url: str | None = None,
) -> bool:
    """Optional non-blocking sync ping to local SillyTavern Web (`127.0.0.1:8000`) if running."""
    base_url = (st_web_url or os.environ.get("ST_WEB_URL", "http://127.0.0.1:8000")).rstrip("/")
    endpoint = f"{base_url}/api/plugins/tavern-fusion-bridge/notify"
    body = json.dumps({"event": event_type, "timestamp": time.time(), "payload": payload}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "X-Tavern-Fusion": "3.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=0.25) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def get_character_chat_dir(st_data_root: str | Path, character_name: str) -> Path:
    root = Path(st_data_root).resolve()
    safe_name = sanitize_filename_stem(character_name)
    chat_dir = root / "chats" / safe_name
    return ensure_under_dir(chat_dir, root)


def list_chat_files(st_data_root: str | Path, character_name: str) -> list[Path]:
    """Lists `.jsonl` chat files for a character ordered newest-modified first."""
    chat_dir = get_character_chat_dir(st_data_root, character_name)
    if not chat_dir.exists():
        return []
    files = [p for p in chat_dir.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def list_groups(st_data_root: str | Path) -> list[dict[str, Any]]:
    """Lists SillyTavern group definitions from `groups/*.json` if present."""
    groups_dir = Path(st_data_root) / "groups"
    if not groups_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(groups_dir.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "id": str(raw.get("id", p.stem)),
                "name": str(raw.get("name", p.stem)),
                "members": list(raw.get("members", [])),
                "chat_id": str(raw.get("chat_id", "")),
            })
        except Exception:
            continue
    return out


def read_jsonl_chat(chat_path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reads a SillyTavern `.jsonl` file into `(header_dict, messages_list)`.
    Safely handles empty lines or trailing partial lines (ST v1.19.0 resilience).
    """
    path = Path(chat_path)
    if not path.exists():
        raise FileNotFoundError(f"Chat file does not exist: {path}")

    header: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_idx, raw_line in enumerate(fh):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if line_idx == 0 and ("chat_metadata" in obj or "user_name" in obj) and "mes" not in obj:
                header = obj
            else:
                messages.append(obj)

    header.setdefault("chat_metadata", {})
    header["chat_metadata"].setdefault("variables", {})
    header["chat_metadata"].setdefault("timed_effects", {"sticky": {}, "cooldown": {}})
    return header, messages


def write_jsonl_chat_atomic(
    chat_path: str | Path,
    header: dict[str, Any],
    messages: list[dict[str, Any]],
) -> None:
    """Atomically writes `(header, messages)` to `.jsonl` under exclusive lock + `fsync`."""
    path = Path(chat_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header, ensure_ascii=False)]
    for msg in messages:
        lines.append(json.dumps(msg, ensure_ascii=False))
    payload_text = "\n".join(lines) + "\n"
    with exclusive_file_lock(path):
        atomic_write_text(path, payload_text)


def create_new_chat_session(
    st_data_root: str | Path,
    character_name: str,
    user_name: str = "旅行者",
    greeting_text: str = "",
    alternate_greetings: list[str] | None = None,
    greeting_index: int = 0,
) -> dict[str, Any]:
    """Creates a brand-new `.jsonl` chat file and seeds `first_mes` ONCE (Terva & ST rule)."""
    chat_dir = get_character_chat_dir(st_data_root, character_name)
    chat_dir.mkdir(parents=True, exist_ok=True)
    ts_label = format_st_timestamp()
    chat_path = chat_dir / f"{ts_label}.jsonl"
    if chat_path.exists():
        chat_path = chat_dir / f"{ts_label}_{uuid.uuid4().hex[:4]}.jsonl"

    header: dict[str, Any] = {
        "user_name": user_name,
        "character_name": character_name,
        "create_date": ts_label,
        "chat_metadata": {
            "integrity": uuid.uuid4().hex,
            "variables": {},
            "timed_effects": {"sticky": {}, "cooldown": {}},
            "note_prompt": "",
            "note_depth": 4,
            "note_interval": 1,
            "note_role": 0,
        },
    }

    all_greetings = [greeting_text] if greeting_text else []
    if alternate_greetings:
        all_greetings.extend([g for g in alternate_greetings if g])

    messages: list[dict[str, Any]] = []
    if all_greetings:
        chosen_idx = max(0, min(greeting_index, len(all_greetings) - 1))
        chosen_mes = all_greetings[chosen_idx]
        now_human = format_human_send_date()
        messages.append({
            "name": character_name,
            "is_user": False,
            "is_system": False,
            "send_date": now_human,
            "mes": chosen_mes,
            "swipes": all_greetings,
            "swipe_id": chosen_idx,
            "swipe_info": [{"send_date": now_human, "extra": {}} for _ in all_greetings],
            "extra": {"seeded_greeting": True},
        })

    write_jsonl_chat_atomic(chat_path, header, messages)
    notify_st_web_bridge("CHAT_CREATED", {"character": character_name, "chat_file": chat_path.name})
    return {
        "chat_path": str(chat_path.resolve()),
        "chat_file": chat_path.name,
        "floor_count": len(messages),
        "seeded_greeting": bool(messages),
    }


def append_turn_to_chat(
    chat_path: str | Path,
    character_name: str,
    user_name: str,
    user_mes: str | None,
    char_mes: str,
    swipe_mode: bool = False,
    variables_delta: dict[str, Any] | None = None,
    next_timed_state: dict[str, Any] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically appends a full RP turn (or appends a new Swipe to the last reply).

    Note: If the Sidecar already pre-appended `user_mes` as a pending floor (`is_user=True`
    at `messages[-1]` matching `user_mes`), avoids duplicating the user message!
    """
    path = Path(chat_path)
    header, messages = read_jsonl_chat(path)

    meta = header.setdefault("chat_metadata", {})
    vars_store = meta.setdefault("variables", {})
    if variables_delta:
        for k, v in variables_delta.items():
            vars_store[str(k)] = v
    if next_timed_state is not None:
        meta["timed_effects"] = next_timed_state

    now_human = format_human_send_date()
    extra_payload = dict(extra_meta or {})
    extra_payload["source"] = "antigravity_tavern_fusion"

    if swipe_mode and messages and not messages[-1].get("is_user", False):
        last_msg = messages[-1]
        swipes: list[str] = list(last_msg.get("swipes") or [last_msg.get("mes", "")])
        swipes.append(char_mes)
        new_swipe_idx = len(swipes) - 1
        last_msg["swipes"] = swipes
        last_msg["swipe_id"] = new_swipe_idx
        last_msg["mes"] = char_mes
        last_msg["send_date"] = now_human
        swipe_info = list(last_msg.get("swipe_info") or [])
        while len(swipe_info) < len(swipes) - 1:
            swipe_info.append({"send_date": now_human, "extra": {}})
        swipe_info.append({"send_date": now_human, "extra": extra_payload})
        last_msg["swipe_info"] = swipe_info
    else:
        if user_mes is not None and user_mes.strip():
            clean_u = user_mes.strip()
            is_backstage = bool(extra_payload.get("is_backstage", False))
            already_pending = (
                bool(messages)
                and bool(messages[-1].get("is_user"))
                and str(messages[-1].get("mes", "")).strip() == clean_u
            )
            if already_pending and is_backstage:
                messages[-1].setdefault("extra", {})["is_backstage"] = True
            elif not already_pending:
                u_extra: dict[str, Any] = {"source": "antigravity_user"}
                if is_backstage:
                    u_extra["is_backstage"] = True
                messages.append({
                    "name": user_name,
                    "is_user": True,
                    "is_system": False,
                    "send_date": now_human,
                    "mes": clean_u,
                    "extra": u_extra,
                })
        messages.append({
            "name": character_name,
            "is_user": False,
            "is_system": False,
            "send_date": now_human,
            "mes": char_mes,
            "swipes": [char_mes],
            "swipe_id": 0,
            "swipe_info": [{"send_date": now_human, "extra": extra_payload}],
            "extra": extra_payload,
        })

    write_jsonl_chat_atomic(path, header, messages)
    turn_hash = hashlib.sha256(f"{len(messages)}:{char_mes}".encode("utf-8")).hexdigest()[:16]
    web_notified = notify_st_web_bridge(
        "CHAT_UPDATED",
        {
            "character": character_name,
            "chat_file": path.name,
            "floor_count": len(messages),
            "turn_hash": turn_hash,
        },
    )
    return {
        "chat_path": str(path.resolve()),
        "floor_count": len(messages),
        "active_swipe_id": messages[-1].get("swipe_id", 0),
        "total_swipes": len(messages[-1].get("swipes", [1])),
        "variables": vars_store,
        "turn_hash": turn_hash,
        "st_web_notified": web_notified,
    }


def stage_floor_operation(
    chat_path: str | Path,
    character_name: str,
    operation: Literal[
        "edit_floor",
        "delete_floor",
        "switch_swipe",
        "prune_swipes",
        "fork_from_floor",
        "append_user_floor",
        "toggle_backstage",
    ],
    floor_index: int = -1,
    swipe_id: int = 0,
    new_text: str = "",
    user_name: str = "旅行者",
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Interactive Stage floor revision operations inspired by Terva's Stage grammar."""
    path = Path(chat_path)
    header, messages = read_jsonl_chat(path)
    if not messages and operation != "append_user_floor":
        raise ValueError("Chat session has no floors to modify.")

    idx = (len(messages) - 1) if floor_index < 0 else floor_index
    if operation != "append_user_floor" and not (0 <= idx < len(messages)):
        raise ValueError(f"floor_index {floor_index} out of range (0..{len(messages) - 1}).")

    if operation == "append_user_floor":
        clean_u = new_text.strip()
        if not clean_u:
            raise ValueError("Cannot append an empty user message.")
        u_extra: dict[str, Any] = {"source": "tavern_stage_sidecar"}
        if isinstance(extra_meta, dict):
            u_extra.update(extra_meta)
        messages.append({
            "name": user_name,
            "is_user": True,
            "is_system": False,
            "send_date": format_human_send_date(),
            "mes": clean_u,
            "extra": u_extra,
        })
        write_jsonl_chat_atomic(path, header, messages)
        return {
            "operation": operation,
            "floor_count": len(messages),
            "floor_index": len(messages) - 1,
            "is_backstage": bool(u_extra.get("is_backstage", False)),
        }

    if operation == "toggle_backstage":
        target = messages[idx]
        ext = target.setdefault("extra", {})
        next_val = not bool(ext.get("is_backstage", False))
        ext["is_backstage"] = next_val
        write_jsonl_chat_atomic(path, header, messages)
        notify_st_web_bridge("CHAT_UPDATED", {"character": character_name, "floor_count": len(messages)})
        return {"operation": "toggle_backstage", "floor_index": idx, "is_backstage": next_val}

    if operation == "edit_floor":
        target = messages[idx]
        target["mes"] = new_text
        swipes = list(target.get("swipes") or [])
        cur_s = int(target.get("swipe_id", 0))
        if swipes and 0 <= cur_s < len(swipes):
            swipes[cur_s] = new_text
            target["swipes"] = swipes
        target.setdefault("extra", {})["edited_at"] = format_human_send_date()
        write_jsonl_chat_atomic(path, header, messages)
        notify_st_web_bridge("CHAT_UPDATED", {"character": character_name, "floor_count": len(messages)})
        return {"operation": "edit_floor", "floor_index": idx, "mes": new_text}

    if operation == "delete_floor":
        if len(messages) <= 1 and idx == 0:
            raise ValueError("Cannot delete the sole greeting floor; create a new chat instead.")
        backup_path = path.with_suffix(".jsonl.bak")
        shutil.copy2(path, backup_path)
        removed = messages.pop(idx)
        write_jsonl_chat_atomic(path, header, messages)
        notify_st_web_bridge("CHAT_UPDATED", {"character": character_name, "floor_count": len(messages)})
        return {
            "operation": "delete_floor",
            "deleted_floor_index": idx,
            "deleted_sender": removed.get("name", ""),
            "remaining_floors": len(messages),
        }

    if operation == "switch_swipe":
        target = messages[idx]
        swipes = list(target.get("swipes") or [target.get("mes", "")])
        if not (0 <= swipe_id < len(swipes)):
            raise ValueError(f"swipe_id {swipe_id} out of range (0..{len(swipes) - 1}).")
        target["swipe_id"] = swipe_id
        target["mes"] = swipes[swipe_id]
        write_jsonl_chat_atomic(path, header, messages)
        notify_st_web_bridge("SWIPE_CHANGED", {"character": character_name, "floor_index": idx, "swipe_id": swipe_id})
        return {
            "operation": "switch_swipe",
            "floor_index": idx,
            "swipe_id": swipe_id,
            "total_swipes": len(swipes),
            "mes": target["mes"],
        }

    if operation == "prune_swipes":
        target = messages[idx]
        swipes = list(target.get("swipes") or [target.get("mes", "")])
        cur_s = max(0, min(int(target.get("swipe_id", 0)), len(swipes) - 1))
        kept = swipes[cur_s]
        pruned_count = len(swipes) - 1
        target["swipes"] = [kept]
        target["swipe_id"] = 0
        target["mes"] = kept
        target["swipe_info"] = [{"send_date": format_human_send_date(), "extra": {"pruned": True}}]
        write_jsonl_chat_atomic(path, header, messages)
        return {"operation": "prune_swipes", "floor_index": idx, "pruned_count": pruned_count}

    if operation == "fork_from_floor":
        cutoff = idx + 1
        branch_ts = format_st_timestamp()
        branch_path = path.parent / f"{path.stem}_fork_f{cutoff}_{branch_ts}.jsonl"
        branch_header = json.loads(json.dumps(header))
        branch_header.setdefault("chat_metadata", {})["main_chat"] = path.stem
        branch_header["chat_metadata"]["forked_at_floor"] = cutoff
        write_jsonl_chat_atomic(branch_path, branch_header, messages[:cutoff])
        return {
            "operation": "fork_from_floor",
            "branch_path": str(branch_path.resolve()),
            "branch_file": branch_path.name,
            "floors_copied": cutoff,
        }

    raise ValueError(f"Unsupported stage floor operation: {operation}")


def manage_chat_branch(
    st_data_root: str | Path,
    character_name: str,
    chat_path: str | Path,
    action: Literal["switch_swipe", "fork_checkpoint", "rollback"],
    target_id: int = 0,
) -> dict[str, Any]:
    """Switches swipes, forks a checkpoint branch `.jsonl`, or rolls back floors."""
    path = Path(chat_path)
    header, messages = read_jsonl_chat(path)

    if action == "switch_swipe":
        if not messages or messages[-1].get("is_user", False):
            raise ValueError("Last floor is not a character message with swipes.")
        res = stage_floor_operation(path, character_name, "switch_swipe", floor_index=len(messages) - 1, swipe_id=target_id)
        return {"action": "switch_swipe", "swipe_id": res["swipe_id"], "mes": res["mes"]}

    if action == "fork_checkpoint":
        cutoff = len(messages) if target_id <= 0 else min(len(messages), target_id)
        branch_ts = format_st_timestamp()
        branch_path = path.parent / f"{path.stem}_branch_{branch_ts}.jsonl"
        branch_header = json.loads(json.dumps(header))
        branch_header.setdefault("chat_metadata", {})["main_chat"] = path.stem
        write_jsonl_chat_atomic(branch_path, branch_header, messages[:cutoff])
        return {
            "action": "fork_checkpoint",
            "branch_path": str(branch_path.resolve()),
            "branch_file": branch_path.name,
            "floors_copied": cutoff,
        }

    if action == "rollback":
        steps = max(1, target_id)
        if steps >= len(messages):
            raise ValueError("Cannot roll back all floors including greeting; start a new chat instead.")
        backup_path = path.with_suffix(".jsonl.bak")
        shutil.copy2(path, backup_path)
        trimmed = messages[:-steps]
        write_jsonl_chat_atomic(path, header, trimmed)
        notify_st_web_bridge("CHAT_UPDATED", {"character": character_name, "floor_count": len(trimmed)})
        return {
            "action": "rollback",
            "floors_removed": steps,
            "remaining_floors": len(trimmed),
            "backup_path": str(backup_path.resolve()),
        }

    raise ValueError(f"Unknown branch action: {action}")
