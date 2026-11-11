"""Character Card V1 / V2 / V3 lossless PNG & JSON codec + Card Doctor (`card_codec.py`).

Implements ISO/IEC 15948 PNG chunk parsing (`tEXt`, `zTXt`, `iTXt`) using pure
Python 3.12 standard library (`struct`, `zlib`, `base64`, `json`), with optional
Pillow thumbnail/Data-URI generation.

Key guarantees:
1. Reads `ccv3` chunk BEFORE `chara` chunk (matching SillyTavern v1.19.0).
2. Writes BOTH `chara` (`chara_card_v2`, "2.0") and `ccv3` (`chara_card_v3`, "3.0")
   atomically via `fsync` + `os.replace` so no client ever reads stale or half-written PNGs.
3. Enforces the `Card-is-Data` trust boundary: strips any unauthorized privilege
   or shell/hook escalation keys from imported cards while preserving valid ST
   extension fields (`talkativeness`, `fav`, `world`, `depth_prompt`, `regex_scripts`).
4. Runs Seppä's round-trip assertion (`JSON <-> PNG` parse equivalence) on write.
5. Provides `lint_character_card` (Card Doctor) and `list_character_expressions`.
"""

from __future__ import annotations

import base64
import copy
import io
import json
from pathlib import Path
import re
import struct
import time
from typing import Any
import zlib

from .fs_safety import atomic_write_bytes, ensure_under_dir, sanitize_filename_stem

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

FORBIDDEN_EXTENSION_KEYS = frozenset({
    "mcp_override",
    "hooks_override",
    "shell_command",
    "exec",
    "permission_escalation",
    "auto_approve_tools",
    "allow_outside_workspace",
})

DEFAULT_V3_DATA_FIELDS: dict[str, Any] = {
    "name": "",
    "description": "",
    "personality": "",
    "scenario": "",
    "first_mes": "",
    "mes_example": "",
    "creator_notes": "",
    "system_prompt": "",
    "post_history_instructions": "",
    "alternate_greetings": [],
    "group_only_greetings": [],
    "tags": [],
    "creator": "",
    "character_version": "1.0",
    "nickname": "",
    "source": [],
    "creation_date": 0,
    "modification_date": 0,
    "assets": [{"type": "icon", "uri": "ccdefault:", "name": "main", "ext": "png"}],
    "extensions": {},
}

EMOTION_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("amusement", ("amuse", "amused", "chuckle", "giggle", "tease", "playful", "wink", "忍俊不禁", "逗笑", "轻笑出声", "调侃", "戏谑", "狡黠", "打趣", "失笑")),
    ("embarrassment", ("blush", "flush", "shy", "embarrass", "fluster", "脸颊微红", "耳根泛红", "羞赧", "局促", "窘迫", "绯红")),
    ("excitement", ("excited", "excitement", "thrill", "eager", "sparkle", "兴奋", "雀跃", "振奋", "激动", "迫不及待")),
    ("love", ("love", "affection", "fondly", "adore", "cherish", "深情", "眷恋", "爱意", "宠溺", "痴迷", "心仪")),
    ("desire", ("desire", "yearn", "longing", "breathless", "hunger", "渴望", "炽热", "悸动", "贪恋")),
    ("gratitude", ("grateful", "gratitude", "thank", "appreciate", "感激", "谢意", "多谢", "感念")),
    ("pride", ("proud", "pride", "triumph", "dignity", "chin up", "自豪", "骄傲", "傲然", "矜持", "自信")),
    ("admiration", ("admire", "admiration", "respect", "impressed", "awe", "赞许", "钦佩", "敬佩", "赞叹")),
    ("approval", ("approve", "approval", "nod", "agree", "well done", "颔首", "认可", "赞同", "点头")),
    ("optimism", ("optimistic", "hope", "hopeful", "brighten", "dawn", "希冀", "期许", "乐观", "憧憬", "希望")),
    ("relief", ("relief", "relieved", "exhale", "relax", "phew", "松了口气", "释然", "舒展", "如释重负", "安心")),
    ("realization", ("realize", "realization", "aha", "dawned", "understand now", "恍然", "顿悟", "意识到", "明白过来")),
    ("surprise", ("surprise", "surprised", "startled", "widen", "gasp", "shock", "惊讶", "微怔", "瞳孔微缩", "一惊", "愕然", "怔住")),
    ("curiosity", ("curious", "curiosity", "tilt", "wonder", "inquire", "intrigue", "好奇", "打量", "探究", "挑眉", "饶有兴致")),
    ("confusion", ("confuse", "confused", "confusion", "puzzled", "bewildered", "blink", "困惑", "迷茫", "不解", "疑惑", "茫然")),
    ("remorse", ("remorse", "apologetic", "guilt", "sorry", "regret", "愧疚", "歉意", "自责", "悔恨", "抱歉")),
    ("grief", ("grief", "weep", "sob", "heartbreak", "devastated", "悲恸", "痛哭", "心碎", "恸哭", "哀恸")),
    ("sadness", ("sad", "sadness", "sorrow", "mourn", "tears", "ache", "黯然", "哀伤", "垂眸", "悲伤", "心酸", "落寞")),
    ("disappointment", ("disappoint", "disappointed", "letdown", "dejected", "失望", "叹惋", "失落", "黯淡")),
    ("disapproval", ("disapprove", "frown", "shake head", "forbid", "scold", "不许胡说", "蹙眉", "皱眉", "反对", "不赞同", "斥责")),
    ("annoyance", ("annoy", "annoyed", "irritate", "roll eyes", "tsk", "嗔怪", "无奈", "烦躁", "不满", "撇嘴")),
    ("anger", ("anger", "angry", "glare", "scowl", "furious", "rage", "恼怒", "冷冽", "怒意", "厉声", "震怒", "寒声")),
    ("disgust", ("disgust", "recoil", "grimace", "revulsion", "scorn", "厌恶", "嫌弃", "作呕", "鄙夷")),
    ("fear", ("fear", "terrified", "tremble", "dread", "shiver", "pale", "恐惧", "战栗", "发抖", "骇然", "惊恐")),
    ("nervousness", ("nervous", "hesitate", "tense", "uneasy", "anxious", "fidget", "紧张", "戒备", "不安", "攥紧", "踌躇")),
    ("caring", ("caring", "gentle", "softly", "tender", "soothe", "comfort", "heartache", "温柔", "轻抚", "关切", "柔声", "心疼", "安抚", "守着你")),
    ("joy", ("joy", "joyful", "laugh", "happy", "grin", "beam", "smile", "smiling", "欣喜", "欢愉", "开心", "笑意盈盈", "眉眼弯弯", "微笑")),
]

NEGATED_JOY_RE = re.compile(
    r"笑意[^\n。！？，,.!?]{0,6}(?:褪去|凝滞|敛去|消失|全无)|不再微笑|收起笑容|smile\s+(?:fades|freezes|vanishes|falters)|fading\s+smile",
    re.IGNORECASE,
)


class CardCodecError(ValueError):
    """Raised when a character card PNG or JSON fails validation or parsing."""


def create_minimal_rgba_png(
    width: int = 256,
    height: int = 256,
    rgb: tuple[int, int, int] = (20, 62, 54),
) -> bytes:
    """Generates a valid standalone RGBA PNG image using pure stdlib `zlib`."""
    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b, 255]) * width
    raw_scanlines = row * height

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(raw_scanlines, level=6)
    return PNG_SIGNATURE + make_chunk(b"IHDR", ihdr) + make_chunk(b"IDAT", idat) + make_chunk(b"IEND", b"")


def iter_png_chunks(png_bytes: bytes) -> list[tuple[bytes, bytes]]:
    """Parses raw PNG bytes into a list of `(chunk_type, chunk_data)` tuples."""
    if not png_bytes.startswith(PNG_SIGNATURE):
        raise CardCodecError("Invalid PNG signature: file is not a valid PNG image.")
    offset = len(PNG_SIGNATURE)
    total = len(png_bytes)
    chunks: list[tuple[bytes, bytes]] = []
    while offset + 8 <= total:
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        if data_end + 4 > total:
            raise CardCodecError(f"Truncated PNG chunk {chunk_type!r} at offset {offset}.")
        chunk_data = png_bytes[data_start:data_end]
        expected_crc = struct.unpack(">I", png_bytes[data_end : data_end + 4])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise CardCodecError(
                f"CRC mismatch in PNG chunk {chunk_type.decode('latin1', 'replace')}."
            )
        chunks.append((chunk_type, chunk_data))
        offset = data_end + 4
        if chunk_type == b"IEND":
            break
    return chunks


def _decode_text_chunk(chunk_type: bytes, data: bytes) -> tuple[str, str] | None:
    """Extracts `(keyword, text_value)` from `tEXt`, `zTXt`, or `iTXt` chunks."""
    if chunk_type == b"tEXt":
        if b"\x00" not in data:
            return None
        key_raw, val_raw = data.split(b"\x00", 1)
        return key_raw.decode("latin1"), val_raw.decode("latin1")

    if chunk_type == b"zTXt":
        if b"\x00" not in data:
            return None
        key_raw, rest = data.split(b"\x00", 1)
        if not rest or rest[0] != 0:
            return None
        decompressed = zlib.decompress(rest[1:])
        return key_raw.decode("latin1"), decompressed.decode("utf-8", errors="replace")

    if chunk_type == b"iTXt":
        nul1 = data.find(b"\x00")
        if nul1 == -1 or nul1 + 3 > len(data):
            return None
        keyword = data[:nul1].decode("latin1")
        comp_flag = data[nul1 + 1]
        rest = data[nul1 + 3 :]
        nul2 = rest.find(b"\x00")
        if nul2 == -1:
            return None
        rest2 = rest[nul2 + 1 :]
        nul3 = rest2.find(b"\x00")
        if nul3 == -1:
            return None
        payload = rest2[nul3 + 1 :]
        if comp_flag == 1:
            payload = zlib.decompress(payload)
        return keyword, payload.decode("utf-8", errors="replace")

    return None


def _encode_text_chunk(keyword: str, value: str) -> bytes:
    """Encodes an uncompressed `tEXt` chunk (`keyword\\x00value`) with CRC32."""
    chunk_type = b"tEXt"
    data = keyword.encode("latin1") + b"\x00" + value.encode("latin1")
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _decode_card_payload(raw_text: str) -> dict[str, Any]:
    """Decodes either raw UTF-8 JSON or Base64-encoded UTF-8 JSON envelope."""
    stripped = raw_text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    compact = "".join(stripped.split())
    padded = compact + "=" * ((4 - len(compact) % 4) % 4)
    decoded_bytes = base64.b64decode(padded)
    return json.loads(decoded_bytes.decode("utf-8"))


def sanitize_and_normalize_v3(raw_card: dict[str, Any]) -> dict[str, Any]:
    """Normalizes V1/V2/V3 card JSON into canonical Character Card V3 structure
    while enforcing the `Card-is-Data` zero-privilege boundary.
    """
    if not isinstance(raw_card, dict):
        raise CardCodecError("Character card root payload must be a JSON object.")

    source_data: dict[str, Any] = {}
    if isinstance(raw_card.get("data"), dict):
        for k in DEFAULT_V3_DATA_FIELDS:
            if k in raw_card and raw_card[k] is not None and raw_card[k] != "":
                source_data[k] = copy.deepcopy(raw_card[k])
        source_data.update(copy.deepcopy(raw_card["data"]))
    else:
        source_data = copy.deepcopy(raw_card)

    normalized_data = copy.deepcopy(DEFAULT_V3_DATA_FIELDS)
    for k, v in source_data.items():
        if v is not None:
            normalized_data[k] = v

    for str_field in (
        "description",
        "personality",
        "scenario",
        "first_mes",
        "mes_example",
        "system_prompt",
        "post_history_instructions",
    ):
        val = normalized_data.get(str_field)
        if isinstance(val, str) and val:
            normalized_data[str_field] = re.sub(
                r"<\|im_(?:start|end)\|>|</?ST_RUNTIME_FRAME[^>]*>",
                "",
                val,
                flags=re.IGNORECASE,
            )

    if not str(normalized_data.get("name", "")).strip():
        raise CardCodecError("Character card requires a non-empty 'name' field.")

    ext = normalized_data.get("extensions")
    if not isinstance(ext, dict):
        ext = {}
    removed_keys: list[str] = []
    for forbidden in list(ext.keys()):
        if forbidden in FORBIDDEN_EXTENSION_KEYS or forbidden.startswith("__"):
            removed_keys.append(forbidden)
            ext.pop(forbidden, None)
    normalized_data["extensions"] = ext

    now_ts = int(time.time())
    if not normalized_data.get("creation_date"):
        normalized_data["creation_date"] = now_ts
    normalized_data["modification_date"] = now_ts

    result: dict[str, Any] = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": normalized_data,
    }
    for v1_key in ("name", "description", "personality", "scenario", "first_mes", "mes_example"):
        result[v1_key] = normalized_data.get(v1_key, "")
    if removed_keys:
        result["_sanitized_forbidden_keys"] = removed_keys
    return result


def downgrade_v3_to_v2_envelope(v3_card: dict[str, Any]) -> dict[str, Any]:
    """Builds the companion `chara_card_v2` (`2.0`) JSON for the `chara` chunk."""
    v2 = copy.deepcopy(v3_card)
    v2.pop("_sanitized_forbidden_keys", None)
    v2["spec"] = "chara_card_v2"
    v2["spec_version"] = "2.0"
    return v2


def extract_avatar_data_uri(png_bytes: bytes, max_size: int = 384) -> str | None:
    """Converts PNG bytes into a compact Data URI for Generative UI snapshots."""
    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(png_bytes)) as img:
            img = img.convert("RGB")
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
    except Exception:
        if len(png_bytes) > 1_500_000:
            # Without Pillow, returning the full-size original PNG as a data URI would
            # bloat widgets and model context by megabytes — degrade to no avatar instead.
            return None
        b64 = base64.b64encode(png_bytes).decode("ascii")
        return f"data:image/png;base64,{b64}"


def read_character_card(
    card_path: str | Path,
    include_avatar_data_uri: bool = True,
) -> dict[str, Any]:
    """Reads a `.png` or `.json` character card, preferring `ccv3` over `chara`."""
    path = Path(card_path)
    if not path.exists():
        raise CardCodecError(f"Character card file not found: {path}")

    if path.suffix.lower() == ".json":
        raw_obj = json.loads(path.read_text(encoding="utf-8"))
        card_v3 = sanitize_and_normalize_v3(raw_obj)
        return {
            "source_path": str(path.resolve()),
            "detected_chunk": "json",
            "has_dual_chunks": False,
            "raw_card": raw_obj,
            "card": card_v3,
            "avatar_data_uri": None,
        }

    png_bytes = path.read_bytes()
    chunks = iter_png_chunks(png_bytes)
    text_entries: dict[str, str] = {}
    for c_type, c_data in chunks:
        if c_type in (b"tEXt", b"zTXt", b"iTXt"):
            parsed = _decode_text_chunk(c_type, c_data)
            if parsed is not None:
                kw, val = parsed
                text_entries[kw.lower()] = val

    if "ccv3" in text_entries:
        chosen_kw = "ccv3"
        raw_obj = _decode_card_payload(text_entries["ccv3"])
    elif "chara" in text_entries:
        chosen_kw = "chara"
        raw_obj = _decode_card_payload(text_entries["chara"])
    else:
        raise CardCodecError(
            f"PNG file '{path.name}' contains neither 'ccv3' nor 'chara' metadata chunks."
        )

    card_v3 = sanitize_and_normalize_v3(raw_obj)
    return {
        "source_path": str(path.resolve()),
        "detected_chunk": chosen_kw,
        "has_dual_chunks": ("ccv3" in text_entries and "chara" in text_entries),
        "raw_card": raw_obj,
        "card": card_v3,
        "avatar_data_uri": extract_avatar_data_uri(png_bytes) if include_avatar_data_uri else None,
    }


def write_character_card_png(
    card_payload: dict[str, Any],
    output_png_path: str | Path,
    base_png_path: str | Path | None = None,
    base_png_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Synthesizes a Character Card PNG with synchronized `chara` + `ccv3` chunks
    and verifies round-trip equivalence (`Seppä` assertion).
    """
    v3_card = sanitize_and_normalize_v3(card_payload)
    clean_v3 = copy.deepcopy(v3_card)
    sanitized_keys = clean_v3.pop("_sanitized_forbidden_keys", [])
    v2_card = downgrade_v3_to_v2_envelope(clean_v3)

    if base_png_bytes is not None:
        raw_png = base_png_bytes
    elif base_png_path is not None and Path(base_png_path).exists():
        raw_png = Path(base_png_path).read_bytes()
    elif Path(output_png_path).exists():
        raw_png = Path(output_png_path).read_bytes()
    else:
        raw_png = create_minimal_rgba_png()

    orig_chunks = iter_png_chunks(raw_png)
    kept_chunks: list[bytes] = []
    for c_type, c_data in orig_chunks:
        if c_type in (b"tEXt", b"zTXt", b"iTXt"):
            parsed = _decode_text_chunk(c_type, c_data)
            if parsed and parsed[0].lower() in ("chara", "ccv3"):
                continue
        if c_type == b"IEND":
            continue
        crc = zlib.crc32(c_type + c_data) & 0xFFFFFFFF
        kept_chunks.append(struct.pack(">I", len(c_data)) + c_type + c_data + struct.pack(">I", crc))

    b64_v2 = base64.b64encode(
        json.dumps(v2_card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    b64_v3 = base64.b64encode(
        json.dumps(clean_v3, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    chara_chunk = _encode_text_chunk("chara", b64_v2)
    ccv3_chunk = _encode_text_chunk("ccv3", b64_v3)
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    out_bytes = PNG_SIGNATURE + b"".join(kept_chunks) + chara_chunk + ccv3_chunk + iend_chunk
    out_path = atomic_write_bytes(output_png_path, out_bytes)

    verified = read_character_card(out_path, include_avatar_data_uri=False)
    if (
        not verified["has_dual_chunks"]
        or verified["detected_chunk"] != "ccv3"
        or verified["card"]["data"]["name"] != clean_v3["data"]["name"]
        or verified["card"]["data"]["first_mes"] != clean_v3["data"]["first_mes"]
    ):
        raise CardCodecError("Seppä round-trip assertion failed on written PNG.")

    return {
        "output_path": str(out_path.resolve()),
        "bytes_written": len(out_bytes),
        "character_name": clean_v3["data"]["name"],
        "dual_chunks_written": ["chara", "ccv3"],
        "sanitized_forbidden_keys": sanitized_keys,
    }


def list_character_expressions(st_data_root: str | Path, character_name: str) -> list[str]:
    """Lists available emotion sprite names under `characters/<CharacterName>/`."""
    root = Path(st_data_root)
    try:
        # `character_name` originates from untrusted card data (Card-is-Data): sanitize the
        # stem and enforce directory containment to block traversal outside `characters/`.
        safe_name = sanitize_filename_stem(character_name)
        expr_dir = ensure_under_dir(root / "characters" / safe_name, root / "characters")
    except (ValueError, OSError):
        return []
    if not expr_dir.exists() or not expr_dir.is_dir():
        return []
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    return sorted(p.stem for p in expr_dir.iterdir() if p.is_file() and p.suffix.lower() in allowed)


def detect_message_emotion(text: str, available_expressions: list[str]) -> str | None:
    """Infers the best matching expression sprite name for a character reply.

    Scores all 28 GoEmotions candidates across stage directions (`*...*`, 2x weight)
    and dialogue (`1x` weight) while filtering negated-smile false positives.
    """
    if not available_expressions:
        return None
    avail_map = {e.lower(): e for e in available_expressions}
    raw_text = text or ""
    sanitized_for_joy = NEGATED_JOY_RE.sub(" ", raw_text)
    action_spans = " ".join(re.findall(r"\*([^*\n]+)\*", raw_text)).lower()
    action_spans_joy = " ".join(re.findall(r"\*([^*\n]+)\*", sanitized_for_joy)).lower()
    lower_full = raw_text.lower()
    lower_full_joy = sanitized_for_joy.lower()

    best_key: str | None = None
    best_score = 0.0
    for rank_idx, (expr_key, triggers) in enumerate(EMOTION_KEYWORDS):
        if expr_key not in avail_map:
            continue
        full_hay = lower_full_joy if expr_key == "joy" else lower_full
        act_hay = action_spans_joy if expr_key == "joy" else action_spans
        score = 0.0
        for trig in triggers:
            if trig in act_hay:
                score += 2.0
            elif trig in full_hay:
                score += 1.0
        if score > 0:
            # Slight specificity bonus for earlier-ranked nuanced emotions over generic fallbacks
            score += max(0.0, (len(EMOTION_KEYWORDS) - rank_idx) * 0.01)
            if score > best_score:
                best_score = score
                best_key = expr_key

    if best_key is not None:
        return avail_map[best_key]
    if "neutral" in avail_map:
        return avail_map["neutral"]
    return available_expressions[0]


# ---------------------------------------------------------------------------
# Card Doctor — Underage / Minor Risk Gate Heuristics
# ---------------------------------------------------------------------------
MINOR_HARD_TAG_RE = re.compile(
    r"(?:^|[\s,;/|_-])(?:loli|lolicon|shota|shotacon|underage|preteen|prepubescent|"
    r"minor[-_ ]?(?:char|girl|boy)?|child[-_ ]?(?:char|girl|boy)?|toddler|kiddie|"
    r"elementary[-_ ]?school|middle[-_ ]?school)(?:$|[\s,;/|_-])|"
    r"(?:萝莉|正太|幼女|幼童|未成年|小学生|初中生|国中生|幼稚园|幼儿园)",
    re.IGNORECASE,
)

MINOR_HARD_TEXT_RE = re.compile(
    r"\b(?:lolicon|shotacon|loli\s+(?:girl|body|char)|shota\s+(?:boy|body|char)|"
    r"underage\s+(?:girl|boy|teen|character|student)|prepubescent)\b|"
    r"(?:小萝莉|小正太|萝莉体[型态]|幼女|幼童|未成年(?:少女|少年|学生|角色))",
    re.IGNORECASE,
)

# Terms that are themselves sexualised codings of minors: always refused, regardless of context.
SEXUALIZED_MINOR_TAG_RE = re.compile(
    r"(?:^|[\s,;/|_-])(?:loli|lolicon|shota|shotacon)(?:$|[\s,;/|_-])|(?:萝莉|正太|幼女|幼童)",
    re.IGNORECASE,
)

SEXUALIZED_MINOR_TEXT_RE = re.compile(
    r"\b(?:lolicon|shotacon|loli\s+body|shota\s+body)\b|(?:小萝莉|小正太|萝莉体[型态]|幼女体[型态])",
    re.IGNORECASE,
)

# Sexual or romantic-partner framing. A minor signal is only escalated to a hard refusal when
# combined with one of these; a minor character in a non-sexual story gets the red-screen warning.
MINOR_SEXUAL_CONTEXT_RE = re.compile(
    r"\b(?:nsfw|erp|smut|lewd|hentai|porn\w*|sex(?:ual|y)?|nude|naked|fetish|seduc\w*|r-?18|18\+|"
    r"lover|girlfriend|boyfriend|romance|romantic|dating|kiss(?:es|ing)?)\b|"
    r"(?:色情|情色|性爱|做爱|性交|性感|裸体|裸露|赤裸|调教|淫|肉便器|r18|18禁|成人向|涩涩|瑟瑟|"
    r"恋人|女友|男友|女朋友|男朋友|恋爱|约会|接吻|亲吻|暧昧|求爱|勾引|诱惑)",
    re.IGNORECASE,
)

MINOR_APPEARS_UNDERAGE_RE = re.compile(
    r"(?:合法萝莉|万年萝莉|千年萝莉|童颜幼体|幼女体[型态]|幼女身[体躯]|孩童身[体躯]|小孩体型|"
    r"未发育(?:完全)?的?(?:身体|身躯|体态|少女)|"
    r"(?:外表|外貌|看起来|看上去|长相|容貌|体态|身形|体型|身体)[^\n。；！？!?]{0,12}"
    r"(?:像|如同|宛如|停留在|保持在|只有|不过)?[^\n。；！？!?]{0,6}"
    r"(?:幼女|幼童|小孩|儿童|小女孩|小男孩|小学生|初中生|(?:[4-9]|1[0-7])\s*岁))|"
    r"\b(?:looks?|appears?|resembles?|shaped\s+like|body\s+of|physique\s+of)\s+"
    r"(?:like\s+)?(?:a\s+|an\s+)?(?:little\s+(?:girl|boy)|young\s+child|small\s+child|"
    r"preteen|child|kid|0*(?:[4-9]|1[0-7])(?:-|\s+)year(?:-|\s+)old)\b|"
    r"\bchildlike\s+(?:body|physique|figure)\b",
    re.IGNORECASE,
)

MINOR_HARD_SCHOOL_RE = re.compile(
    r"(?:小学[一二三四五六1-6]?年级|小学生|红领巾|小学生书包|"
    r"初中[一二三1-3]?年级|初中生|国中[一二三1-3]?年级|国中生|"
    r"初[一二三](?:学生|女生|男生|生|班|年级)|"
    r"高[一二](?:学生|女生|男生|生|班)|高中[一二]年级|"
    r"\brandoseru\b|"
    r"\b(?:elementary|primary|middle|junior\s+high)\s+school\s+(?:student|girl|boy|kid)\b|"
    r"\bhigh\s+school\s+(?:freshman|sophomore|1st[- ]year|2nd[- ]year|first[- ]year|second[- ]year)\b|"
    r"\b(?:[1-8](?:st|nd|rd|th)|9th|10th)\s+grader\b)",
    re.IGNORECASE,
)

MINOR_WARN_SCHOOL_UNIFORM_RE = re.compile(
    r"(?:女子高中生|高中生|高三(?:学生|女生|男生|生)?|中学生|"
    r"校服|水手服|死库水|学生制服|百褶校服裙|学妹|"
    r"\bJK\b|\bseifuku\b|\bsukumizu\b|"
    r"\b(?:school|sailor)\s+uniform\b|"
    r"\bhigh\s+school\s+(?:student|girl|boy|senior)\b)",
    re.IGNORECASE,
)

MINOR_WARN_YOUTH_CUE_RE = re.compile(
    r"(?:童颜|稚嫩(?:的)?(?:少女|少年|脸庞|面容|身躯)|娇小少女|青涩学[妹弟]|"
    r"\bbaby[- ]?faced?\b|\bgothic\s+lolita\b)",
    re.IGNORECASE,
)

LABELLED_AGE_RE = re.compile(
    r"(?:^|[\n\r,，;；|｜(\[（【•*-])\s*"
    r"(?:age|char(?:acter)?_?age|年龄|芳龄|年方|设定年龄|实际年龄|外表年龄|生理年龄)\s*"
    r"[:：=为是]\s*0*(\d{1,3})(?:\s*(?:岁|周岁|years?\s*old|y/?o))?(?![0-9.])",
    re.IGNORECASE,
)

PROSE_AGE_CN_RE = re.compile(
    r"(?:\{\{char\}\}|她|他|少女|少年|女孩|男孩)\s*(?:今年|年仅|只有|才|刚满|芳龄)\s*"
    r"0*(\d{1,2})\s*(?:岁|周岁)(?!半?前|时|那年|以前|之前|的时候|左右时)",
)

PROSE_AGE_EN_RE = re.compile(
    r"(?:\{\{char\}\}|\b(?:she|he))\s+is\s+(?:a\s+|an\s+|only\s+|just\s+)?0*(\d{1,2})(?:-|\s+)years?(?:-|\s+)old\b",
    re.IGNORECASE,
)

ADULT_ROLE_EXEMPTION_RE = re.compile(
    r"(?:大学|研究生|博士|教授|讲师|教师|班主任|老师|教官|校医|"
    r"\buniversity\b|\bcollege\b|\bprofessor\b|\bteacher\b|\binstructor\b|\bfaculty\b)",
    re.IGNORECASE,
)

HEIGHT_LABEL_RE = re.compile(
    r"(?:height|身高|体长)\s*[:：=约为是]?\s*(\d{2,3})(?:\.\d+)?\s*(?:cm|厘米)\b",
    re.IGNORECASE,
)

HEIGHT_INLINE_CM_RE = re.compile(
    r"(?<![0-9.])(8\d|9\d|1[0-4]\d)(?:\.\d+)?\s*(?:cm|厘米)\b",
    re.IGNORECASE,
)

NON_STATURE_PREFIX_RE = re.compile(
    r"(?:胸围|腰围|臀围|三围|剑长|刀长|刃长|枪长|尾长|翼展|bust|waist|hip|blade|length)\s*[:：=约为是]?\s*$",
    re.IGNORECASE,
)


def evaluate_minor_risk_gate(card_or_bundle: dict[str, Any]) -> dict[str, Any]:
    """Evaluates character card metadata & text for underage / minor risk signals.

    Returns a structured verdict:
    - `gate_action`: `"block_load"` (default load/import refusal) | `"red_screen_warn"` (mandatory red-screen warning) | `"pass"`
    - `hard_signals`: unambiguous underage / child-coded triggers
    - Decision ladder:
      1. Sexualised minor codings (`loli` / `shota` tags, "legal loli" / "looks 12" loophole) -> `block_load`.
      2. Minor signals (age < 18, school grade, stature < 140cm) -> `block_load` ONLY when combined with
         sexual / romantic-partner context; otherwise `red_screen_warn` (non-sexual stories with minors).
      3. Ambiguous cues (uniform, youthful look, borderline height) -> `red_screen_warn`, or `block_load`
         when combined with sexual / romantic-partner context.
    - Scans every prompt-bearing field, including `mes_example`, alternate greetings and the embedded
      `character_book`, so moving text out of `description` cannot bypass the gate.
    - `reasons`: human-readable Chinese explanations for Card Doctor & UI overlays
    """
    if "card" in card_or_bundle and isinstance(card_or_bundle["card"], dict):
        card_v3 = card_or_bundle["card"]
        raw_card = card_or_bundle.get("raw_card", {})
    elif "data" in card_or_bundle and isinstance(card_or_bundle["data"], dict):
        card_v3 = card_or_bundle
        raw_card = card_or_bundle
    else:
        card_v3 = {"data": card_or_bundle if isinstance(card_or_bundle, dict) else {}}
        raw_card = card_or_bundle if isinstance(card_or_bundle, dict) else {}

    data = card_v3.get("data", {}) if isinstance(card_v3.get("data"), dict) else {}
    raw_data = raw_card.get("data", raw_card) if isinstance(raw_card, dict) else {}
    ext = data.get("extensions", {}) if isinstance(data.get("extensions"), dict) else {}

    always_block_reasons: list[str] = []
    hard_reasons: list[str] = []
    warn_reasons: list[str] = []
    signals: list[str] = []
    explicit_adult_age = False

    # 1. Structured age fields in `data`, `raw_data`, or `data.extensions`
    for src_label, mapping in (("data", data), ("raw_data", raw_data), ("extensions", ext)):
        if not isinstance(mapping, dict):
            continue
        for k in ("age", "char_age", "character_age", "apparent_age"):
            if k in mapping and mapping[k] is not None and str(mapping[k]).strip() != "":
                m_num = re.search(r"(?<!\d)(\d{1,3})(?!\d)", str(mapping[k]))
                if m_num:
                    age_val = int(m_num.group(1))
                    if 1 <= age_val < 18:
                        signals.append(f"field:{src_label}.{k}={age_val}")
                        hard_reasons.append(f"年龄字段未满 18 岁 ({k}={age_val})")
                    elif 18 <= age_val <= 99 and k != "apparent_age":
                        explicit_adult_age = True

    # 2. Tags inspection (`loli`, `shota`, `underage`, etc.)
    raw_tags = data.get("tags") or raw_data.get("tags") or []
    if isinstance(raw_tags, str):
        tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tag_list = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tag_list = []

    for tag in tag_list:
        t_low = tag.lower()
        if SEXUALIZED_MINOR_TAG_RE.search(tag):
            signals.append(f"tag:{tag}")
            always_block_reasons.append(f"命中低幼性化标签「{tag}」")
        elif MINOR_HARD_TAG_RE.search(tag):
            signals.append(f"tag:{tag}")
            hard_reasons.append(f"命中未成年标签「{tag}」")
        elif "lolita" in t_low and "gothic" not in t_low:
            signals.append(f"tag:{tag}")
            warn_reasons.append(f"包含易混淆低幼风险标签「{tag}」")

    # 3. Textual scan across character prompt fields
    text_parts: list[str] = [
        str(data.get(field, "") or "")
        for field in (
            "name", "description", "personality", "scenario", "first_mes",
            "system_prompt", "post_history_instructions", "mes_example",
        )
    ]
    for list_field in ("alternate_greetings", "group_only_greetings"):
        greetings = data.get(list_field)
        if isinstance(greetings, list):
            text_parts.extend(str(g) for g in greetings if g)
    book = data.get("character_book")
    if isinstance(book, dict) and isinstance(book.get("entries"), list):
        for entry in book["entries"]:
            if isinstance(entry, dict):
                text_parts.append(str(entry.get("content", "") or ""))
    text_blob = "\n".join(p for p in text_parts if p)

    for m in LABELLED_AGE_RE.finditer(text_blob):
        age_val = int(m.group(1))
        if 1 <= age_val < 18:
            signals.append(f"labelled_age:{age_val}")
            hard_reasons.append(f"设定文本声明年龄未满 18 岁 ({m.group(0).strip()})")
        elif 18 <= age_val <= 99:
            explicit_adult_age = True

    for rx in (PROSE_AGE_CN_RE, PROSE_AGE_EN_RE):
        for m in rx.finditer(text_blob):
            age_val = int(m.group(1))
            if 4 <= age_val < 18:
                signals.append(f"prose_age:{age_val}")
                hard_reasons.append(f"角色叙事年龄未满 18 岁 ({m.group(0).strip()})")
            elif 18 <= age_val <= 99:
                explicit_adult_age = True

    m_sex_minor_txt = SEXUALIZED_MINOR_TEXT_RE.search(text_blob)
    m_hard_txt = MINOR_HARD_TEXT_RE.search(text_blob)
    if m_sex_minor_txt:
        signals.append(f"sexualized_minor_term:{m_sex_minor_txt.group(0)}")
        always_block_reasons.append(f"包含低幼性化设定词「{m_sex_minor_txt.group(0)}」")
    elif m_hard_txt:
        signals.append(f"hard_term:{m_hard_txt.group(0)}")
        hard_reasons.append(f"包含明确未成年/低幼化设定词「{m_hard_txt.group(0)}」")

    # 4. "Appears underage" / legal-loli loophole heuristic (NEVER exempted by adult numeric age!)
    m_app = MINOR_APPEARS_UNDERAGE_RE.search(text_blob)
    if m_app:
        signals.append(f"appears_underage:{m_app.group(0)[:24]}")
        always_block_reasons.append(f"命中“外表/体型看起来未成年”启发式特征「{m_app.group(0).strip()}」")

    # 5. School grade & uniform setting
    has_adult_role = bool(ADULT_ROLE_EXEMPTION_RE.search(text_blob))
    m_school_hard = MINOR_HARD_SCHOOL_RE.search(text_blob)
    if m_school_hard:
        if has_adult_role and explicit_adult_age:
            signals.append(f"school_context_warn:{m_school_hard.group(0)}")
            warn_reasons.append(f"虽声明成年身份，但包含低龄学段语境「{m_school_hard.group(0)}」")
        else:
            signals.append(f"school_minor:{m_school_hard.group(0)}")
            hard_reasons.append(f"命中未成年学段设定「{m_school_hard.group(0)}」")
    else:
        m_school_warn = MINOR_WARN_SCHOOL_UNIFORM_RE.search(text_blob)
        if m_school_warn and not (explicit_adult_age and has_adult_role):
            signals.append(f"school_uniform:{m_school_warn.group(0)}")
            warn_reasons.append(f"检出学年/校服/高中设定「{m_school_warn.group(0)}」且未明确成年职业身份")

    # 6. Height / stature heuristic
    detected_heights: list[int] = []
    for k in ("height", "char_height"):
        raw_h = data.get(k) or ext.get(k)
        if raw_h is not None:
            mh = re.search(r"(\d{2,3})", str(raw_h))
            if mh:
                detected_heights.append(int(mh.group(1)))
    for m in HEIGHT_LABEL_RE.finditer(text_blob):
        detected_heights.append(int(m.group(1)))
    for m in HEIGHT_INLINE_CM_RE.finditer(text_blob):
        prefix = text_blob[max(0, m.start() - 16) : m.start()]
        if not NON_STATURE_PREFIX_RE.search(prefix):
            detected_heights.append(int(m.group(1)))

    for cm in detected_heights:
        if 75 <= cm < 140:
            signals.append(f"height_cm:{cm}")
            hard_reasons.append(f"角色身高设定过低 ({cm}cm < 140cm)，触发低幼体格风险阈值")
            break
        if 140 <= cm <= 148 and not explicit_adult_age:
            signals.append(f"height_borderline_cm:{cm}")
            warn_reasons.append(f"角色身高处于低龄边缘区间 ({cm}cm) 且未声明满 18 岁")
            break

    # 7. Youthful appearance cues
    m_youth = MINOR_WARN_YOUTH_CUE_RE.search(text_blob)
    if m_youth and not explicit_adult_age:
        signals.append(f"youth_cue:{m_youth.group(0)}")
        warn_reasons.append(f"检出低龄外观暗示词「{m_youth.group(0)}」且未声明满 18 岁")

    # 8. Sexual / romantic-partner context (escalates minor & ambiguous signals to refusal)
    m_sexual = MINOR_SEXUAL_CONTEXT_RE.search(text_blob) or MINOR_SEXUAL_CONTEXT_RE.search(" ".join(tag_list))
    if m_sexual:
        signals.append(f"sexual_context:{m_sexual.group(0)}")

    # Deduplicate reasons while preserving order
    dedup_always = list(dict.fromkeys(always_block_reasons))
    dedup_hard = list(dict.fromkeys(hard_reasons))
    dedup_warn = list(dict.fromkeys(warn_reasons))
    sexual_reason = f"同时存在性/恋爱语境「{m_sexual.group(0)}」" if m_sexual else ""

    if dedup_always:
        gate_action = "block_load"
        reasons = dedup_always + dedup_hard + dedup_warn
    elif dedup_hard and m_sexual:
        gate_action = "block_load"
        reasons = dedup_hard + dedup_warn + [sexual_reason]
    elif dedup_warn and m_sexual:
        gate_action = "block_load"
        reasons = ["未成年启发式特征与性/恋爱语境叠加触发拒载：" + "；".join(dedup_warn + [sexual_reason])]
    elif dedup_hard or dedup_warn:
        gate_action = "red_screen_warn"
        reasons = dedup_hard + dedup_warn
        if dedup_hard:
            reasons = reasons + ["本卡含未成年角色设定：仅允许非性、非恋爱向剧情，请确认后继续"]
    else:
        gate_action = "pass"
        reasons = []

    return {
        "gate_action": gate_action,
        "blocked": gate_action == "block_load",
        "red_screen_warn": gate_action == "red_screen_warn",
        "explicit_adult_age": explicit_adult_age,
        "signals": signals,
        "reasons": reasons,
    }


def lint_character_card(card_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Card Doctor static analyzer inspired by `terva-sh/terva` (`packages/agent/card/lint.go`)."""
    issues: list[dict[str, Any]] = []
    card = card_bundle.get("card", {})
    raw_card = card_bundle.get("raw_card", {})
    data = card.get("data", {})

    # 0. Underage / Minor Risk Gate (highest priority check)
    minor_gate = evaluate_minor_risk_gate(card_bundle)
    if minor_gate["gate_action"] == "block_load":
        issues.append({
            "code": "MINOR_RISK_BLOCKED",
            "severity": "critical",
            "gate_action": "block_load",
            "field": "minor_risk_gate",
            "signals": minor_gate["signals"],
            "message": f"未成年风险闸【默认拒绝装载】：{'；'.join(minor_gate['reasons'])}",
            "fix_hint": "本工具拒绝装载未成年或低幼化特征角色卡；请移除卡片，或将角色明确修正为年满 18 周岁的成年设定。",
        })
    elif minor_gate["gate_action"] == "red_screen_warn":
        issues.append({
            "code": "MINOR_RISK_RED_SCREEN_WARN",
            "severity": "error",
            "gate_action": "red_screen_warn",
            "field": "minor_risk_gate",
            "signals": minor_gate["signals"],
            "message": f"未成年风险闸【强制红屏警告】：{'；'.join(minor_gate['reasons'])}",
            "fix_hint": "若角色确系满 18 周岁成年人，请在 description 中补充明确成年年龄（如 Age: 20）以消除启发式告警。",
        })

    if card_bundle.get("detected_chunk") == "chara" or not card_bundle.get("has_dual_chunks", False):
        if card_bundle.get("detected_chunk") != "json":
            issues.append({
                "code": "MISSING_DUAL_CCV3_CHUNK",
                "severity": "warning",
                "field": "png_chunks",
                "message": "PNG lacks synchronized `ccv3` + `chara` dual chunks; re-export via Seppä (`st_build_export_card`).",
            })

    if card.get("_sanitized_forbidden_keys"):
        issues.append({
            "code": "FORBIDDEN_EXTENSION_KEYS",
            "severity": "error",
            "field": "data.extensions",
            "message": f"Sanitized untrusted privilege keys: {card['_sanitized_forbidden_keys']}",
        })

    sys_prompt = str(data.get("system_prompt", "")).strip()
    if sys_prompt and "{{original}}" not in sys_prompt:
        issues.append({
            "code": "SYSTEM_PROMPT_WIPES_PRESET",
            "severity": "warning",
            "field": "data.system_prompt",
            "message": "`system_prompt` is non-empty without `{{original}}`—this overwrites the user's Main Prompt preset instead of layering.",
        })

    if not str(data.get("first_mes", "")).strip():
        issues.append({
            "code": "EMPTY_FIRST_MES",
            "severity": "warning",
            "field": "data.first_mes",
            "message": "Character has an empty `first_mes` opening greeting.",
        })

    creator_notes = str(data.get("creator_notes", "")).strip()
    desc_and_sys = f"{data.get('description', '')}\n{sys_prompt}"
    if creator_notes and len(creator_notes) >= 24 and creator_notes in desc_and_sys:
        issues.append({
            "code": "CREATOR_NOTES_IN_PROMPT",
            "severity": "info",
            "field": "data.description",
            "message": "`creator_notes` OOC text appears duplicated inside active prompt fields, consuming static prefix tokens.",
        })

    mes_ex = str(data.get("mes_example", ""))
    if not mes_ex.strip() or "<start>" not in mes_ex.lower():
        issues.append({
            "code": "MISSING_LEXICAL_START_BLOCKS",
            "severity": "info",
            "field": "data.mes_example",
            "message": "`mes_example` lacks `<START>` dialogue blocks—adding 2-3 `<START>` samples strengthens Lock #2 (Lexical Convergence).",
            "fix_hint": "Add `<START>\\n{{user}}: ...\\n{{char}}: *...* \"...\"` blocks to anchor character vocabulary.",
        })
    elif len(mes_ex) > 5000:
        issues.append({
            "code": "OVERSIZED_MES_EXAMPLE",
            "severity": "info",
            "field": "data.mes_example",
            "message": f"`mes_example` is very large ({len(mes_ex)} chars) and may crowd dynamic lore budget.",
        })

    # Check unresolved legacy macros (`<BOT>`, `<USER>`, `{{Char}}` typos like `{char}`)
    combined_fields = "\n".join(
        str(data.get(k, "")) for k in ("description", "personality", "scenario", "first_mes", "mes_example", "system_prompt")
    )
    # Lookarounds keep valid `{{char}}` / `{{user}}` from matching the half-brace patterns.
    if re.search(
        r"<BOT>|<USER>|(?<!\{)\{(?:char|user)\}\}|\{\{(?:char|user)\}(?!\})|(?<!\{)\{(?:char|user)\}(?!\})",
        combined_fields,
        flags=re.IGNORECASE,
    ):
        issues.append({
            "code": "BROKEN_LEGACY_MACRO",
            "severity": "warning",
            "field": "prompt_fields",
            "message": "Detected legacy `<BOT>`/`<USER>` or malformed single-brace `{char}` macro syntax.",
        })

    # Check raw embedded character_book for `selective: false` trap
    raw_data = raw_card.get("data", raw_card) if isinstance(raw_card, dict) else {}
    raw_book = raw_data.get("character_book")
    if isinstance(raw_book, dict):
        raw_entries = raw_book.get("entries", [])
        items = raw_entries.values() if isinstance(raw_entries, dict) else (raw_entries if isinstance(raw_entries, list) else [])
        for idx, ent in enumerate(items):
            if not isinstance(ent, dict):
                continue
            sec = ent.get("secondary_keys") or ent.get("keysecondary") or []
            if sec and ent.get("selective") is False:
                issues.append({
                    "code": "EMBEDDED_SELECTIVE_GATE_TRAP",
                    "severity": "warning",
                    "field": f"character_book.entries[{idx}]",
                    "message": f"Embedded lore entry '{ent.get('comment') or ent.get('name') or idx}' defines secondary keys while `selective: false` (gate ignored by ST).",
                })

    return issues
