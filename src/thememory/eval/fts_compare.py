"""Compare Chinese FTS schemes: bigram+unigram vs raw trigram tokenize (spec §9.2)."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from thememory.util import prepare_fts_text

# Small self-built Chinese queries (M2). Expected docs are content snippets.
_SAMPLES: list[dict[str, Any]] = [
    {"id": 1, "content": "用户计划前往天津出差五天", "keywords": ["天津", "出差"], "q": "天津"},
    {"id": 2, "content": "用户偏好简洁代码优先的回答", "keywords": ["简洁", "代码"], "q": "简洁"},
    {"id": 3, "content": "该项目使用 PostgreSQL 15", "keywords": ["PostgreSQL"], "q": "postgres"},
    {"id": 4, "content": "用户常住上海", "keywords": ["上海"], "q": "上海"},
    {"id": 5, "content": "提交前需运行静态检查 lint", "keywords": ["lint"], "q": "静态"},
]


def _build_db(mode: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE docs(id INTEGER PRIMARY KEY, fts_text TEXT NOT NULL)")
    if mode == "bigram":
        conn.execute(
            "CREATE VIRTUAL TABLE docs_fts USING fts5(fts_text, content='docs', content_rowid='id', tokenize='unicode61')"
        )
        for row in _SAMPLES:
            text = prepare_fts_text(row["content"], " ".join(row["keywords"]))
            conn.execute("INSERT INTO docs(id, fts_text) VALUES (?, ?)", (row["id"], text))
            conn.execute(
                "INSERT INTO docs_fts(rowid, fts_text) VALUES (?, ?)", (row["id"], text)
            )
    else:
        conn.execute(
            "CREATE VIRTUAL TABLE docs_fts USING fts5(fts_text, content='docs', content_rowid='id', tokenize='trigram')"
        )
        for row in _SAMPLES:
            text = row["content"] + " " + " ".join(row["keywords"])
            conn.execute("INSERT INTO docs(id, fts_text) VALUES (?, ?)", (row["id"], text))
            conn.execute(
                "INSERT INTO docs_fts(rowid, fts_text) VALUES (?, ?)", (row["id"], text)
            )
    return conn


def _hits(conn: sqlite3.Connection, query: str, *, mode: str) -> list[int]:
    if mode == "bigram":
        q = prepare_fts_text(query)
    else:
        q = query
    try:
        rows = conn.execute(
            "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? LIMIT 5",
            (q,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [int(r[0]) for r in rows]


def compare_fts_schemes() -> dict[str, Any]:
    """Return recall for each scheme; recommend the winner for schema notes."""
    results: dict[str, Any] = {"samples": len(_SAMPLES), "modes": {}}
    for mode in ("bigram", "trigram"):
        conn = _build_db(mode)
        hit = 0
        short_hit = 0
        details = []
        for row in _SAMPLES:
            found = _hits(conn, row["q"], mode=mode)
            ok = row["id"] in found
            hit += int(ok)
            if len(row["q"]) <= 2:
                short_hit += int(ok)
            details.append({"id": row["id"], "q": row["q"], "hit": ok, "found": found})
        results["modes"][mode] = {
            "recall": hit / len(_SAMPLES),
            "short_query_recall": short_hit / max(1, sum(1 for r in _SAMPLES if len(r["q"]) <= 2)),
            "details": details,
        }
        conn.close()
    bigram = results["modes"]["bigram"]["recall"]
    trigram = results["modes"]["trigram"]["recall"]
    winner = "bigram" if bigram >= trigram else "trigram"
    results["winner"] = winner
    results["note"] = (
        f"M2 FTS compare: bigram recall={bigram:.2f}, trigram recall={trigram:.2f}; "
        f"selected={winner} (unicode61 + prepare_fts_text)."
    )
    return results


def write_schema_note(root: Path | None = None) -> Path:
    from thememory.paths import home_dir

    base = root or home_dir()
    path = base / "fts_scheme.txt"
    report = compare_fts_schemes()
    path.write_text(report["note"] + "\n", encoding="utf-8")
    return path
