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
