"""Shared helpers: IDs, hashing, text normalization, time."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    """UUIDv7 when available (3.13+), else time-ordered UUIDv4 surrogate."""
    try:
        return str(uuid.uuid7())  # type: ignore[attr-defined]
    except AttributeError:
        # time-prefixed uuid4 for ordering approximation on 3.12
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        return f"{ts}-{uuid.uuid4()}"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.strip().lower()
    text = re.sub(r"\s+", "", text)
    return text


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> float:
    """Chinese chars ×1.0 plus English words ×1.3 (spec §7.3)."""
    cjk = 0
    rest: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            cjk += 1
        else:
            rest.append(ch)
    words = re.findall(r"[A-Za-z0-9_]+", "".join(rest))
    return float(cjk) + len(words) * 1.3


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def local_calendar_date(tz: str | None) -> str:
    """User-local YYYY-MM-DD. Falls back to the machine zone, then UTC."""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo
        if zone is None:
            zone = UTC
        return datetime.now(zone).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.now(UTC).strftime("%Y-%m-%d")


def display_date(observed_at: str | None, tz: str | None = None) -> str:
    """Stable local date for snapshot lines. Same inputs always yield the same string."""
    raw = observed_at or ""
    if len(raw) < 10:
        return raw
    if not tz:
        return raw[:10]
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return raw[:10]


_FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def shrink_code_blocks(text: str, *, keep: bool = False, head_chars: int = 200) -> str:
    """Replace fenced code with a short placeholder unless keep_code_blocks (spec §6.2)."""
    if keep or "```" not in text:
        return text

    def _repl(match: re.Match[str]) -> str:
        lang = (match.group(1) or "").strip() or "text"
        body = match.group(2) or ""
        lines = body.count("\n") + (1 if body else 0)
        head = body[:head_chars]
        return f"[CODE BLOCK lang={lang} lines={lines}]\n{head}"

    return _FENCE.sub(_repl, text)


def truncate_turn(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[TRUNCATED]"


def prepare_fts_text(*parts: str) -> str:
    """Chinese-friendly FTS: keep raw text + character bigrams + unigrams."""
    joined = " ".join(p for p in parts if p)
    joined = unicodedata.normalize("NFKC", joined)
    chars = [c for c in joined if not c.isspace()]
    grams: list[str] = []
    for c in chars:
        grams.append(c)
    for i in range(len(chars) - 1):
        grams.append(chars[i] + chars[i + 1])
    # also keep original tokens for latin
    tokens = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", joined)
    return " ".join(tokens + grams)


def json_list(value: Any) -> str:
    import json

    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def parse_json_list(raw: str | None) -> list[Any]:
    import json

    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []
