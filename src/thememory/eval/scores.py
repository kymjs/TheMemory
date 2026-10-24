"""Append one JSON line per offline eval run (spec §20.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from thememory.util import utc_now


def scores_path(root: Path) -> Path:
    return root / "eval" / "scores.jsonl"


def append_score(root: Path, row: dict[str, Any]) -> Path:
    dest = scores_path(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": utc_now(), **row}
    payload.pop("details", None)
    payload.pop("scenarios", None)
    payload.pop("trials", None)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return dest
