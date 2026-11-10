"""Dynamic Hybrid World Info / Lorebook Engine (`lore_engine.py`).

Combines 100% SillyTavern v1.19.0 deterministic World Info rules with a genre-agnostic
SQLite character/word N-gram + TF-weighted vector similarity index (plus optional
external Embedding HTTP endpoint support), ensuring semantic recall works across
fantasy, sci-fi, historical, wuxia, and mystery settings with zero mandatory dependencies.

Also provides `lint_lorebook_file` (World Info Doctor) and Windows-safe SQLite handle
lifecycle (`close()` / context manager) so temporary directories release cleanly.
"""

from __future__ import annotations

from collections import Counter
import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import time
from typing import Any, Literal
import urllib.error
import urllib.request

from .fs_safety import atomic_write_json

SelectiveLogic = Literal["AND_ANY", "AND_ALL", "NOT_ANY", "NOT_ALL"]
PositionType = Literal[
    "before_char",
    "after_char",
    "before_examples",
    "after_examples",
    "an_top",
    "an_bottom",
    "at_depth",
    "outlet",
]
RoleType = Literal["system", "user", "assistant"]

ST_POSITION_MAP: dict[int | str, PositionType] = {
    0: "before_char",
    1: "after_char",
    2: "an_top",
    3: "an_bottom",
    4: "at_depth",
    5: "before_examples",
    6: "after_examples",
    7: "outlet",
    "before_char": "before_char",
    "after_char": "after_char",
    "an_top": "an_top",
    "an_bottom": "an_bottom",
    "at_depth": "at_depth",
    "at_depth_system": "at_depth",
    "before_examples": "before_examples",
    "after_examples": "after_examples",
    "outlet": "outlet",
}

ST_SELECTIVE_LOGIC_MAP: dict[int | str, SelectiveLogic] = {
    0: "AND_ANY",
    1: "NOT_ALL",
    2: "NOT_ANY",
    3: "AND_ALL",
    "AND_ANY": "AND_ANY",
    "AND ANY": "AND_ANY",
    "NOT_ALL": "NOT_ALL",
    "NOT ALL": "NOT_ALL",
    "NOT_ANY": "NOT_ANY",
    "NOT ANY": "NOT_ANY",
    "AND_ALL": "AND_ALL",
    "AND ALL": "AND_ALL",
}

ST_ROLE_MAP: dict[int | str, RoleType] = {
    0: "system",
    1: "user",
    2: "assistant",
    "system": "system",
    "user": "user",
    "assistant": "assistant",
}

# Cross-lingual & cross-genre semantic bridge anchors (fantasy, sci-fi, mystery, combat, social)
BILINGUAL_CONCEPT_ANCHORS: list[tuple[set[str], str]] = [
    ({"shadowfang", "shadow", "dark", "wolf", "beast", "fang", "monster", "暗影", "黑雾", "魔狼", "魔兽", "狼影", "低吼", "瘴气", "黑兽", "怪物"}, "__concept_shadow_beast__"),
    ({"glade", "sanctuary", "shrine", "ward", "rune", "grove", "barrier", "圣域", "林间", "结界", "界石", "符文", "铭文", "古树", "屏障"}, "__concept_sacred_glade__"),
    ({"eldoria", "covenant", "elf", "elven", "forest", "ancient", "pact", "艾尔多利亚", "盟约", "精灵", "誓约", "森林", "古树海", "古约"}, "__concept_eldoria_lore__"),
    ({"lantern", "silver", "light", "torch", "glow", "flare", "beacon", "提灯", "银纹", "微光", "火光", "照明", "信标"}, "__concept_silver_light__"),
    ({"heal", "injury", "wound", "potion", "blood", "medicine", "medkit", "伤势", "治疗", "药剂", "流血", "恢复", "包扎", "医疗"}, "__concept_healing__"),
    ({"guild", "quest", "tavern", "coin", "gold", "bounty", "commission", "公会", "委托", "酒馆", "金币", "报酬", "悬赏"}, "__concept_guild_quest__"),
    ({"starship", "orbit", "reactor", "cyber", "neural", "drone", "laser", "星舰", "轨道", "反应堆", "义体", "神经", "无人机"}, "__concept_scifi_tech__"),
    ({"clue", "murder", "suspect", "evidence", "alibi", "cipher", "线索", "凶案", "嫌疑", "证物", "密文", "不在场证明"}, "__concept_mystery_clue__"),
    ({"sect", "sword", "qi", "cultivation", "meridian", "martial", "宗门", "剑气", "真气", "经脉", "修为", "江湖"}, "__concept_wuxia_xianxia__"),
]

STOPWORDS_EN = frozenset({"the", "and", "for", "with", "that", "this", "from", "into", "are", "was", "were", "have", "has"})
STOPCHARS_ZH = frozenset("的了是在有和与及或着过吧呢吗啊呀把被让向往从到于")


@dataclass
class NormalizedLoreEntry:
    uid: int
    source_book: str
    source_tier: int  # 0=chat, 1=persona, 2=character, 3=global
    keys: list[str]
    selective: bool = False
    keys_secondary: list[str] = field(default_factory=list)
    selective_logic: SelectiveLogic = "AND_ANY"
    content: str = ""
    comment: str = ""
    constant: bool = False
    vectorized: bool = True
    enabled: bool = True
    order: int = 100
    position: PositionType = "before_char"
    depth: int = 4
    role: RoleType = "system"
    outlet_name: str = ""
    probability: int = 100
    use_probability: bool = False
    group: str = ""
    group_weight: int = 100
    prioritize_inclusion: bool = False
    use_group_scoring: bool = False
    prevent_recursion: bool = False
    exclude_recursion: bool = False
    delay_until_recursion: bool = False
    sticky: int = 0
    cooldown: int = 0
    delay: int = 0
    case_sensitive: bool = False


def estimate_tokens(text: str) -> int:
    """Fast local heuristic token estimator handling mixed CJK and English prose."""
    if not text:
        return 0
    cjk_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_words = re.findall(r"[A-Za-z0-9_]+", text)
    punct = max(0, len(text) - cjk_chars - sum(len(w) for w in ascii_words))
    return max(1, int(cjk_chars * 1.15 + len(ascii_words) * 1.3 + punct * 0.25))


def build_feature_vector(text: str) -> dict[str, float]:
    """Builds a sublinear TF-weighted, L2-normalized sparse N-gram + concept vector."""
    cleaned = text.lower()
    raw_counts: Counter[str] = Counter()

    for word in re.findall(r"[a-z0-9_]{2,}", cleaned):
        if word in STOPWORDS_EN:
            continue
        raw_counts[f"w:{word}"] += 2.4
        if len(word) >= 4:
            for i in range(len(word) - 2):
                raw_counts[f"c3:{word[i:i+3]}"] += 0.65

    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", cleaned)
    for run in cjk_runs:
        filtered = "".join(ch for ch in run if ch not in STOPCHARS_ZH)
        target = filtered if len(filtered) >= 2 else run
        for ch in target:
            raw_counts[f"zh1:{ch}"] += 0.75
        for i in range(len(target) - 1):
            raw_counts[f"zh2:{target[i:i+2]}"] += 2.3
        for i in range(len(target) - 2):
            raw_counts[f"zh3:{target[i:i+3]}"] += 1.6

    for triggers, anchor_token in BILINGUAL_CONCEPT_ANCHORS:
        matched = sum(1 for t in triggers if t in cleaned)
        if matched > 0:
            raw_counts[anchor_token] += 3.2 * min(3, matched)

    # Sublinear BM25-style TF saturation (`1 + ln(tf)`) so long entries don't drown concise queries
    weighted: dict[str, float] = {}
    for feat, val in raw_counts.items():
        weighted[feat] = val if feat.startswith("__concept_") else (1.0 + math.log1p(val)) * (1.6 if feat.startswith(("w:", "zh2:")) else 0.9)

    norm = math.sqrt(sum(v * v for v in weighted.values()))
    if norm <= 1e-9:
        return {}
    return {k: round(v / norm, 6) for k, v in weighted.items()}


def cosine_sparse(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Computes cosine similarity between two L2-normalized sparse vectors."""
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    return sum(val * vec_b.get(k, 0.0) for k, val in vec_a.items())


def cosine_dense(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    na = math.sqrt(sum(a * a for a in vec_a))
    nb = math.sqrt(sum(b * b for b in vec_b))
    if na <= 1e-9 or nb <= 1e-9:
        return 0.0
    return dot / (na * nb)


def try_external_embedding(text: str) -> list[float] | None:
    """Optional pluggable dense embedding hook (`TAVERN_EMBEDDING_URL` + `TAVERN_EMBEDDING_MODEL`).
    Supports OpenAI-compatible `/v1/embeddings` or local Ollama `/api/embed`.
    Returns `None` immediately when unconfigured so offline sparse recall runs with zero latency.
    """
    embed_url = os.environ.get("TAVERN_EMBEDDING_URL", "").strip()
    if not embed_url:
        return None
    model = os.environ.get("TAVERN_EMBEDDING_MODEL", "nomic-embed-text").strip()
    api_key = os.environ.get("TAVERN_EMBEDDING_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "input": text[:2000]}
    req = urllib.request.Request(
        embed_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data.get("data"), list) and data["data"]:
                return [float(x) for x in data["data"][0].get("embedding", [])]
            if isinstance(data.get("embeddings"), list) and data["embeddings"]:
                return [float(x) for x in data["embeddings"][0]]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        return None
    return None


def _stable_text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class SQLiteLoreVectorCache:
    """Caches entry vectors in SQLite without holding persistent file locks on Windows.

    Opens short-lived connections for file-backed DBs (or keeps a single connection only
    for `:memory:`), guaranteeing zero `WinError 32` lock leaks during temp-dir cleanup!
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path) if db_path else ":memory:"
        self._is_memory = self.db_path == ":memory:"
        self._mem_conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._mem_conn = sqlite3.connect(":memory:")
            self._init_schema(self._mem_conn)
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                self._init_schema(conn)
            finally:
                conn.close()

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lore_vectors (
                book_name TEXT NOT NULL,
                uid INTEGER NOT NULL,
                text_digest TEXT NOT NULL,
                vec_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (book_name, uid)
            )
            """
        )
        conn.commit()

    def get_or_compute(self, entry: NormalizedLoreEntry) -> dict[str, float]:
        combined = " ".join(entry.keys + entry.keys_secondary + [entry.comment, entry.content])
        digest = _stable_text_digest(combined)

        if self._mem_conn is not None:
            return self._query_or_upsert(self._mem_conn, entry, digest, combined)

        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            return self._query_or_upsert(conn, entry, digest, combined)
        finally:
            conn.close()

    @staticmethod
    def _query_or_upsert(
        conn: sqlite3.Connection,
        entry: NormalizedLoreEntry,
        digest: str,
        combined: str,
    ) -> dict[str, float]:
        cur = conn.execute(
            "SELECT text_digest, vec_json FROM lore_vectors WHERE book_name = ? AND uid = ?",
            (entry.source_book, entry.uid),
        )
        row = cur.fetchone()
        if row and row[0] == digest:
            return json.loads(row[1])

        vec = build_feature_vector(combined)
        conn.execute(
            """
            INSERT INTO lore_vectors (book_name, uid, text_digest, vec_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(book_name, uid) DO UPDATE SET
                text_digest = excluded.text_digest,
                vec_json = excluded.vec_json,
                updated_at = excluded.updated_at
            """,
            (entry.source_book, entry.uid, digest, json.dumps(vec, ensure_ascii=False), time.time()),
        )
        conn.commit()
        return vec

    def close(self) -> None:
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    def __enter__(self) -> SQLiteLoreVectorCache:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _parse_key_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return []


def parse_lorebook_dict(
    raw_book: dict[str, Any],
    book_name: str = "inline_book",
    source_tier: int = 2,
) -> list[NormalizedLoreEntry]:
    """Parses either SillyTavern `worlds/*.json` (`entries` dict) or CCv2/CCv3 `character_book` (`entries` list)."""
    entries_raw = raw_book.get("entries", {})
    iterable: list[tuple[int, dict[str, Any]]] = []
    if isinstance(entries_raw, dict):
        for k, v in entries_raw.items():
            if isinstance(v, dict):
                uid = int(v.get("uid", v.get("id", k if str(k).isdigit() else len(iterable))))
                iterable.append((uid, v))
    elif isinstance(entries_raw, list):
        for idx, v in enumerate(entries_raw):
            if isinstance(v, dict):
                uid = int(v.get("uid", v.get("id", idx)))
                iterable.append((uid, v))

    parsed: list[NormalizedLoreEntry] = []
    for uid, item in iterable:
        if "disable" in item:
            enabled = not bool(item.get("disable", False))
        else:
            enabled = bool(item.get("enabled", True))

        keys = _parse_key_list(item.get("key", item.get("keys", [])))
        keys_sec = _parse_key_list(item.get("keysecondary", item.get("secondary_keys", [])))

        selective = bool(item.get("selective", bool(keys_sec and "selective" not in item)))
        raw_logic = item.get("selectiveLogic", item.get("selective_logic", 0))
        logic = ST_SELECTIVE_LOGIC_MAP.get(raw_logic, "AND_ANY")

        raw_pos = item.get("position", 0)
        pos = ST_POSITION_MAP.get(raw_pos, "before_char")
        ext = item.get("extensions", {}) if isinstance(item.get("extensions"), dict) else {}
        if ext.get("position") in ST_POSITION_MAP:
            pos = ST_POSITION_MAP[ext["position"]]

        depth = int(item.get("depth", ext.get("depth", 4)))
        raw_role = item.get("role", ext.get("role", 0))
        role = ST_ROLE_MAP.get(raw_role, "system")

        parsed.append(
            NormalizedLoreEntry(
                uid=uid,
                source_book=book_name,
                source_tier=source_tier,
                keys=keys,
                selective=selective,
                keys_secondary=keys_sec,
                selective_logic=logic,
                content=str(item.get("content", "")),
                comment=str(item.get("comment", item.get("name", ""))),
                constant=bool(item.get("constant", False)),
                vectorized=bool(item.get("vectorized", True)),
                enabled=enabled,
                order=int(item.get("order", item.get("insertion_order", 100))),
                position=pos,
                depth=depth,
                role=role,
                outlet_name=str(item.get("outletName", item.get("outlet_name", ""))).strip(),
                probability=int(item.get("probability", 100)),
                use_probability=bool(item.get("useProbability", False)),
                group=str(item.get("group", "")).strip(),
                group_weight=int(item.get("groupWeight", 100)),
                prioritize_inclusion=bool(item.get("groupOverride", item.get("prioritizeInclusion", False))),
                use_group_scoring=bool(item.get("useGroupScoring", False)),
                prevent_recursion=bool(item.get("preventRecursion", False)),
                exclude_recursion=bool(item.get("excludeRecursion", False)),
                delay_until_recursion=bool(item.get("delayUntilRecursion", False)),
                sticky=int(item.get("sticky", 0) or 0),
                cooldown=int(item.get("cooldown", 0) or 0),
                delay=int(item.get("delay", 0) or 0),
                case_sensitive=bool(item.get("caseSensitive", item.get("case_sensitive", False))),
            )
        )
    return parsed


def load_lorebook_file(path: str | Path, source_tier: int = 3) -> list[NormalizedLoreEntry]:
    p = Path(path)
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return parse_lorebook_dict(raw, book_name=p.stem, source_tier=source_tier)


def lint_lorebook_file(path: str | Path) -> list[dict[str, Any]]:
    """Static World Info linter (detects `selective: false` secondary-key traps, empty keys, etc.)."""
    p = Path(path)
    if not p.exists():
        return [{"code": "FILE_NOT_FOUND", "severity": "error", "field": str(p.name), "message": "Lorebook file does not exist."}]
    raw = json.loads(p.read_text(encoding="utf-8"))
    entries_raw = raw.get("entries", {})
    items = entries_raw.values() if isinstance(entries_raw, dict) else (entries_raw if isinstance(entries_raw, list) else [])

    issues: list[dict[str, Any]] = []
    seen_uids: set[int] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        uid = int(item.get("uid", idx))
        title = item.get("comment") or item.get("name") or f"uid={uid}"
        if uid in seen_uids:
            issues.append({
                "code": "DUPLICATE_UID",
                "severity": "error",
                "field": f"entries[{uid}]",
                "message": f"Duplicate entry UID {uid} ('{title}').",
            })
        seen_uids.add(uid)

        keys = _parse_key_list(item.get("key", item.get("keys", [])))
        sec = _parse_key_list(item.get("keysecondary", item.get("secondary_keys", [])))
        constant = bool(item.get("constant", False))
        disabled = bool(item.get("disable", False))

        if sec and item.get("selective") is False:
            issues.append({
                "code": "SELECTIVE_GATE_DISABLED",
                "severity": "warning",
                "field": f"entries[{uid}].selective",
                "message": f"Entry '{title}' has secondary keys {sec} while `selective: false`—secondary gate will be ignored!",
            })
        if not disabled and not constant and not keys and not bool(item.get("vectorized", False)):
            issues.append({
                "code": "UNREACHABLE_ENTRY",
                "severity": "warning",
                "field": f"entries[{uid}].key",
                "message": f"Enabled entry '{title}' is non-constant, non-vectorized, and has no primary keys.",
            })
        if not disabled and not str(item.get("content", "")).strip():
            issues.append({
                "code": "EMPTY_LORE_CONTENT",
                "severity": "info",
                "field": f"entries[{uid}].content",
                "message": f"Enabled entry '{title}' has empty `content`.",
            })
    return issues


def match_key_pattern(pattern: str, scan_buffer: str, case_sensitive: bool = False) -> bool:
    """Matches a SillyTavern plain key or JS-style `/regex/flags` pattern."""
    pat = pattern.strip()
    if not pat:
        return False
    if pat.startswith("/") and pat.count("/") >= 2:
        last_slash = pat.rfind("/")
        expr, flags_str = pat[1:last_slash], pat[last_slash + 1 :]
        flags = 0 if ("i" not in flags_str and case_sensitive) else re.IGNORECASE
        if "m" in flags_str:
            flags |= re.MULTILINE
        if "s" in flags_str:
            flags |= re.DOTALL
        try:
            return bool(re.search(expr, scan_buffer, flags=flags))
        except re.error:
            return False

    haystack = scan_buffer if case_sensitive else scan_buffer.lower()
    needle = pat if case_sensitive else pat.lower()
    if needle.isascii() and needle.replace("_", "").isalnum():
        return bool(re.search(rf"\b{re.escape(needle)}\b", haystack))
    return needle in haystack


def count_key_matches(entry: NormalizedLoreEntry, scan_buffer: str) -> tuple[bool, int]:
    """Evaluates primary + secondary gate and returns `(activated, group_score)`."""
    primary_hits = sum(1 for k in entry.keys if match_key_pattern(k, scan_buffer, entry.case_sensitive))
    if primary_hits == 0:
        return False, 0

    if not entry.selective or not entry.keys_secondary:
        return True, primary_hits

    sec_bools = [match_key_pattern(k, scan_buffer, entry.case_sensitive) for k in entry.keys_secondary]
    if entry.selective_logic == "AND_ANY":
        ok = any(sec_bools)
        return ok, (primary_hits + 1) if ok else 0
    if entry.selective_logic == "AND_ALL":
        ok = all(sec_bools)
        return ok, (primary_hits + len(sec_bools)) if ok else 0
    if entry.selective_logic == "NOT_ANY":
        ok = not any(sec_bools)
        return ok, primary_hits if ok else 0
    if entry.selective_logic == "NOT_ALL":
        ok = not all(sec_bools)
        return ok, primary_hits if ok else 0
    return True, primary_hits


def passes_negative_secondary_gate(entry: NormalizedLoreEntry, scan_buffer: str) -> bool:
    """Ensures semantic recall still honors `NOT_ANY` / `NOT_ALL` / `AND_ALL` secondary gates."""
    if not entry.selective or not entry.keys_secondary:
        return True
    sec_bools = [match_key_pattern(k, scan_buffer, entry.case_sensitive) for k in entry.keys_secondary]
    if entry.selective_logic == "NOT_ANY":
        return not any(sec_bools)
    if entry.selective_logic == "NOT_ALL":
        return not all(sec_bools)
    if entry.selective_logic == "AND_ALL":
        return all(sec_bools)
    return True


class HybridLoreEngine:
    """Executes the 5-stage Hybrid Lorebook Recall, Budget Waterfall & `@ D` Splicing."""

    def __init__(self, cache_db_path: str | Path | None = None) -> None:
        self.vector_cache = SQLiteLoreVectorCache(cache_db_path)

    def close(self) -> None:
        self.vector_cache.close()

    def evaluate_and_splice(
        self,
        entries: list[NormalizedLoreEntry],
        chat_messages: list[dict[str, Any]],
        latest_user_input: str = "",
        timed_state: dict[str, dict[str, int]] | None = None,
        scan_depth: int = 4,
        token_budget: int = 2048,
        semantic_threshold: float = 0.26,
        max_recursion_steps: int = 2,
        rng_seed: int | None = 42,
        quarantine_backstage: bool = True,
    ) -> dict[str, Any]:
        rng = random.Random(rng_seed)
        timed = copy.deepcopy(timed_state) if timed_state else {"sticky": {}, "cooldown": {}}
        sticky_map: dict[str, int] = timed.setdefault("sticky", {})
        cooldown_map: dict[str, int] = timed.setdefault("cooldown", {})

        quarantined_backstage_count = 0
        working_messages: list[dict[str, Any]] = []
        for m in chat_messages:
            is_bs = isinstance(m.get("extra"), dict) and bool(m["extra"].get("is_backstage"))
            if quarantine_backstage and is_bs:
                quarantined_backstage_count += 1
                continue
            working_messages.append(dict(m))
        if latest_user_input.strip():
            working_messages.append({"name": "User", "is_user": True, "mes": latest_user_input.strip()})

        total_msgs = len(working_messages)
        recent_window = working_messages[-scan_depth:] if scan_depth > 0 else []
        scan_buffer = "\n".join(
            f"\x01{m.get('name', 'User' if m.get('is_user') else 'Char')}: {m.get('mes', '')}"
            for m in recent_window
        )

        sem_query_text = " ".join(m.get("mes", "") for m in working_messages[-2:])
        query_vec = build_feature_vector(sem_query_text)
        dense_query = try_external_embedding(sem_query_text) if sem_query_text.strip() else None

        candidates: dict[tuple[str, int], tuple[int, float, int, NormalizedLoreEntry, str]] = {}

        for e in entries:
            if not e.enabled or not e.content.strip():
                continue
            state_key = f"{e.source_book}:{e.uid}"

            if e.delay > 0 and total_msgs < e.delay:
                continue
            if cooldown_map.get(state_key, 0) > 0 and sticky_map.get(state_key, 0) <= 0:
                continue

            if e.constant:
                candidates[(e.source_book, e.uid)] = (4, 1.0, 999, e, "constant")
                continue

            if sticky_map.get(state_key, 0) > 0:
                candidates[(e.source_book, e.uid)] = (4, 0.99, 999, e, f"sticky({sticky_map[state_key]})")
                continue

            if e.delay_until_recursion:
                continue

            if e.use_probability and e.probability < 100:
                if e.probability <= 0 or rng.randint(1, 100) > e.probability:
                    continue

            kw_ok, grp_score = count_key_matches(e, scan_buffer)
            if kw_ok:
                candidates[(e.source_book, e.uid)] = (3, 1.0, grp_score, e, "keyword")
                continue

            if e.vectorized and (query_vec or dense_query):
                sim = 0.0
                if dense_query is not None:
                    dense_entry = try_external_embedding(" ".join(e.keys + [e.comment, e.content]))
                    if dense_entry is not None:
                        sim = cosine_dense(dense_query, dense_entry)
                if sim <= 0.0 and query_vec:
                    entry_vec = self.vector_cache.get_or_compute(e)
                    sim = cosine_sparse(query_vec, entry_vec)
                if sim >= semantic_threshold and passes_negative_secondary_gate(e, scan_buffer):
                    candidates[(e.source_book, e.uid)] = (2, sim, 1, e, f"semantic({sim:.2f})")

        for _step in range(max(0, max_recursion_steps)):
            rec_sources = [item[3].content for item in candidates.values() if not item[3].prevent_recursion]
            if not rec_sources:
                break
            rec_buffer = "\n".join(rec_sources)
            added_any = False
            for e in entries:
                key_id = (e.source_book, e.uid)
                if not e.enabled or key_id in candidates or e.exclude_recursion or e.constant:
                    continue
                kw_ok, grp_score = count_key_matches(e, rec_buffer)
                if kw_ok:
                    candidates[key_id] = (1, 0.75, grp_score, e, "recursive")
                    added_any = True
            if not added_any:
                break

        groups: dict[str, list[tuple[int, float, int, NormalizedLoreEntry, str]]] = {}
        ungrouped: list[tuple[int, float, int, NormalizedLoreEntry, str]] = []
        for item in candidates.values():
            grp_name = item[3].group
            if not grp_name:
                ungrouped.append(item)
            else:
                groups.setdefault(grp_name, []).append(item)

        group_winners: list[tuple[int, float, int, NormalizedLoreEntry, str]] = []
        for grp_items in groups.values():
            pool = grp_items
            if any(x[3].use_group_scoring for x in pool):
                max_gs = max(x[2] for x in pool)
                pool = [x for x in pool if x[2] == max_gs]
            if any(x[3].prioritize_inclusion for x in pool):
                winner = max(pool, key=lambda x: (x[0], x[3].order, x[1]))
            elif len(pool) == 1:
                winner = pool[0]
            else:
                weights = [max(1, x[3].group_weight) for x in pool]
                winner = rng.choices(pool, weights=weights, k=1)[0]
            group_winners.append(winner)

        ranked_pool = sorted(
            ungrouped + group_winners,
            key=lambda x: (x[0], -x[3].source_tier, x[3].order, x[1]),
            reverse=True,
        )

        used_tokens = 0
        accepted: list[tuple[NormalizedLoreEntry, str, int]] = []
        evicted: list[dict[str, Any]] = []

        for _, _, _, entry, reason in ranked_pool:
            tok_cost = estimate_tokens(entry.content)
            if used_tokens + tok_cost <= token_budget:
                used_tokens += tok_cost
                accepted.append((entry, reason, tok_cost))
            else:
                evicted.append({
                    "book": entry.source_book,
                    "uid": entry.uid,
                    "comment": entry.comment or (entry.keys[0] if entry.keys else "constant"),
                    "tokens": tok_cost,
                    "reason": "budget_exhausted",
                })

        next_sticky: dict[str, int] = {}
        next_cooldown: dict[str, int] = {}
        for k, rem in cooldown_map.items():
            if rem > 1:
                next_cooldown[k] = rem - 1
        for k, rem in sticky_map.items():
            if rem > 1:
                next_sticky[k] = rem - 1
            elif rem == 1:
                for e in entries:
                    if f"{e.source_book}:{e.uid}" == k and e.cooldown > 0:
                        next_cooldown[k] = e.cooldown
        for entry, reason, _ in accepted:
            state_key = f"{entry.source_book}:{entry.uid}"
            if not reason.startswith("sticky") and entry.sticky > 0:
                next_sticky[state_key] = entry.sticky
            elif entry.sticky == 0 and entry.cooldown > 0:
                next_cooldown[state_key] = entry.cooldown

        accepted.sort(key=lambda item: (item[0].order, -item[0].source_tier))

        slots: dict[str, list[str]] = {
            "before_char": [],
            "after_char": [],
            "before_examples": [],
            "after_examples": [],
            "an_top": [],
            "an_bottom": [],
        }
        outlets: dict[str, list[str]] = {}
        depth_buckets: dict[int, list[NormalizedLoreEntry]] = {}
        active_trace: list[dict[str, Any]] = []

        for entry, reason, tok_cost in accepted:
            pos_label = f"@D{entry.depth}({entry.role})" if entry.position == "at_depth" else entry.position
            active_trace.append({
                "book": entry.source_book,
                "uid": entry.uid,
                "comment": entry.comment or (entry.keys[0] if entry.keys else "constant"),
                "keys": entry.keys,
                "selective": entry.selective,
                "keys_secondary": entry.keys_secondary,
                "trigger": reason,
                "position": pos_label,
                "order": entry.order,
                "tokens": tok_cost,
                "constant": entry.constant,
                "content": entry.content,
            })
            if entry.position == "at_depth":
                depth_buckets.setdefault(max(0, entry.depth), []).append(entry)
            elif entry.position == "outlet" and entry.outlet_name:
                outlets.setdefault(entry.outlet_name, []).append(entry.content)
            elif entry.position in slots:
                slots[entry.position].append(entry.content)

        spliced_chat = [dict(m) for m in working_messages]
        for depth_val in sorted(depth_buckets.keys(), reverse=True):
            idx = max(0, len(spliced_chat) - depth_val)
            injections = [
                {
                    "role": e.role,
                    "name": "WorldInfo",
                    "is_user": e.role == "user",
                    "is_system": e.role == "system",
                    "mes": e.content,
                    "lore_uid": e.uid,
                    "lore_book": e.source_book,
                }
                for e in depth_buckets[depth_val]
            ]
            spliced_chat[idx:idx] = injections

        return {
            "slots": slots,
            "outlets": {k: "\n".join(v) for k, v in outlets.items()},
            "spliced_chat": spliced_chat,
            "used_tokens": used_tokens,
            "token_budget": token_budget,
            "active_trace": active_trace,
            "evicted_trace": evicted,
            "quarantined_backstage_count": quarantined_backstage_count,
            "next_timed_state": {"sticky": next_sticky, "cooldown": next_cooldown},
        }


def evolve_lorebook_file(
    lorebook_path: str | Path,
    operation: Literal["upsert", "toggle", "archive", "delete"],
    entry_payload: dict[str, Any],
) -> dict[str, Any]:
    """`Dramaturgi` Lore Evolution persistence: atomically mutates SillyTavern `worlds/*.json`."""
    path = Path(lorebook_path)
    if path.exists():
        raw_book = json.loads(path.read_text(encoding="utf-8"))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_book = {"entries": {}}

    entries_dict = raw_book.get("entries")
    if not isinstance(entries_dict, dict):
        converted: dict[str, Any] = {}
        if isinstance(entries_dict, list):
            for idx, item in enumerate(entries_dict):
                converted[str(item.get("uid", idx))] = item
        entries_dict = converted
        raw_book["entries"] = entries_dict

    target_uid = entry_payload.get("uid")
    existing_key: str | None = None
    if target_uid is not None:
        for k, v in entries_dict.items():
            if str(v.get("uid", k)) == str(target_uid):
                existing_key = k
                break

    if operation == "delete":
        if existing_key is None:
            raise ValueError(f"Lorebook entry uid={target_uid} not found in {path.name}.")
        entries_dict.pop(existing_key, None)
        action_taken = "deleted"
        final_uid = int(target_uid)

    elif operation == "toggle":
        if existing_key is None:
            raise ValueError(f"Lorebook entry uid={target_uid} not found in {path.name}.")
        cur_disable = bool(entries_dict[existing_key].get("disable", False))
        new_enabled = entry_payload.get("enabled", cur_disable)
        entries_dict[existing_key]["disable"] = not bool(new_enabled)
        action_taken = "toggled"
        final_uid = int(entries_dict[existing_key].get("uid", existing_key))

    elif operation == "archive":
        if existing_key is None:
            raise ValueError(f"Lorebook entry uid={target_uid} not found in {path.name}.")
        entries_dict[existing_key]["disable"] = True
        comment_old = str(entries_dict[existing_key].get("comment", ""))
        if not comment_old.startswith("[ARCHIVED]"):
            entries_dict[existing_key]["comment"] = f"[ARCHIVED] {comment_old}".strip()
        action_taken = "archived"
        final_uid = int(entries_dict[existing_key].get("uid", existing_key))

    elif operation == "upsert":
        if existing_key is None:
            existing_uids = [int(v.get("uid", k)) for k, v in entries_dict.items() if str(v.get("uid", k)).isdigit()]
            final_uid = int(target_uid) if target_uid is not None else (max(existing_uids, default=99) + 1)
            existing_key = str(final_uid)
            entries_dict[existing_key] = {
                "uid": final_uid,
                "key": [],
                "keysecondary": [],
                "comment": "",
                "content": "",
                "constant": False,
                "vectorized": True,
                "selective": False,
                "selectiveLogic": 0,
                "addMemo": True,
                "order": 150,
                "position": 4,
                "disable": False,
                "excludeRecursion": False,
                "preventRecursion": False,
                "delayUntilRecursion": False,
                "probability": 100,
                "useProbability": True,
                "depth": 4,
                "group": "",
                "groupOverride": False,
                "groupWeight": 100,
                "scanDepth": None,
                "caseSensitive": None,
                "matchWholeWords": None,
                "useGroupScoring": None,
                "automationId": "",
                "role": 0,
                "sticky": 0,
                "cooldown": 0,
                "delay": 0,
            }
            action_taken = "inserted"
        else:
            final_uid = int(entries_dict[existing_key].get("uid", existing_key))
            action_taken = "updated"

        rec = entries_dict[existing_key]
        if "keys" in entry_payload or "key" in entry_payload:
            rec["key"] = _parse_key_list(entry_payload.get("keys", entry_payload.get("key")))
        if "keys_secondary" in entry_payload or "keysecondary" in entry_payload:
            sec = _parse_key_list(entry_payload.get("keys_secondary", entry_payload.get("keysecondary")))
            rec["keysecondary"] = sec
            rec["selective"] = bool(entry_payload.get("selective", bool(sec)))
        elif "selective" in entry_payload:
            rec["selective"] = bool(entry_payload["selective"])

        if "selective_logic" in entry_payload or "selectiveLogic" in entry_payload:
            logic_in = entry_payload.get("selective_logic", entry_payload.get("selectiveLogic", 0))
            rev_logic = {"AND_ANY": 0, "NOT_ALL": 1, "NOT_ANY": 2, "AND_ALL": 3}
            rec["selectiveLogic"] = rev_logic.get(str(logic_in).upper(), int(logic_in) if isinstance(logic_in, int) else 0)

        for field_key, target_key, cast in (
            ("content", "content", str),
            ("comment", "comment", str),
            ("order", "order", int),
            ("depth", "depth", int),
            ("constant", "constant", bool),
            ("vectorized", "vectorized", bool),
            ("sticky", "sticky", int),
            ("cooldown", "cooldown", int),
        ):
            if field_key in entry_payload:
                rec[target_key] = cast(entry_payload[field_key])
        if "enabled" in entry_payload:
            rec["disable"] = not bool(entry_payload["enabled"])
        if "position" in entry_payload:
            pos_in = entry_payload["position"]
            rev_pos = {
                "before_char": 0,
                "after_char": 1,
                "an_top": 2,
                "an_bottom": 3,
                "at_depth": 4,
                "before_examples": 5,
                "after_examples": 6,
                "outlet": 7,
            }
            rec["position"] = rev_pos.get(str(pos_in), pos_in if isinstance(pos_in, int) else 4)
    else:
        raise ValueError(f"Unsupported lorebook operation: {operation}")

    atomic_write_json(path, raw_book, indent=2)
    return {
        "lorebook_path": str(path.resolve()),
        "action": action_taken,
        "uid": final_uid,
        "total_entries": len(entries_dict),
        "constant_cache_notice": bool(entry_payload.get("constant")),
    }
