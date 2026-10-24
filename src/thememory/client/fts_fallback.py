"""Read-only FTS when the daemon is down (spec §6.6 step 3). Stdlib sqlite3 only."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def search_memory_fts(db_path: Path, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    text = (query or "").strip()
    if not text or not db_path.exists():
        return []
    quoted = '"' + text.replace('"', " ") + '"'
    conn = sqlite3.connect(str(db_path), timeout=1.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """
            SELECT m.id, m.content, m.type, m.scope
            FROM item_fts
            JOIN memory_items m ON m.seq = item_fts.rowid
            WHERE item_fts MATCH ? AND m.status = 'active'
            LIMIT ?
            """,
            (quoted, limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


def recent_promoted(db_path: Path, *, limit: int = 5) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path), timeout=1.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """
            SELECT id, content, type, scope FROM memory_items
            WHERE status = 'active' AND promoted = 1
            ORDER BY seq DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(row) for row in rows]


def fallback_block(db_path: Path, query: str | None = None, *, limit: int = 5) -> str:
    rows = search_memory_fts(db_path, query or "", limit=limit) if query else []
    if not rows:
        rows = recent_promoted(db_path, limit=limit)
    if not rows:
        return ""
    lines = ["[TheMemory 降级检索，仅关键词，无向量]"]
    for row in rows:
        content = str(row.get("content") or "").replace("\n", " ")
        if content:
            lines.append(f"- {content}")
    return "\n".join(lines) if len(lines) > 1 else ""
