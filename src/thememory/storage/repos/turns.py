"""Repository for turns.db."""

from __future__ import annotations

import sqlite3
from typing import Any

from thememory.util import content_hash, new_id, utc_now


class TurnsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append(
        self,
        *,
        session_id: str,
        host: str,
        host_session_id: str,
        host_turn_id: str,
        role: str,
        content: str,
        tz: str | None = None,
        local_date: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Return (turn, deduped)."""
        chash = content_hash(content)
        existing = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE host=? AND host_session_id=? AND host_turn_id=? AND role=? AND content_hash=?
            """,
            (host, host_session_id, host_turn_id, role, chash),
        ).fetchone()
        if existing:
            return dict(existing), True
        tid = new_id()
        now = utc_now()
        size = len(content.encode("utf-8"))
        self.conn.execute(
            """
            INSERT INTO turns(
              id, session_id, host, host_session_id, host_turn_id, role, content,
              content_hash, created_at, tz, local_date, size_bytes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                tid,
                session_id,
                host,
                host_session_id,
                host_turn_id,
                role,
                content,
                chash,
                now,
                tz,
                local_date,
                size,
            ),
        )
        row = self.conn.execute("SELECT seq FROM turns WHERE id=?", (tid,)).fetchone()
        self.conn.execute(
            "INSERT INTO turns_fts(rowid, content) VALUES (?, ?)",
            (row["seq"], content),
        )
        turn = dict(self.conn.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone())
        return turn, False

    def pending_for_session(self, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id=? AND extracted=0 AND seq>? AND evicted=0
            ORDER BY seq ASC
            """,
            (session_id, after_seq),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_user_turns(self, session_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id=? AND role='user' AND content IS NOT NULL
            ORDER BY seq DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_extracted(self, turn_ids: list[str]) -> None:
        if not turn_ids:
            return
        placeholders = ",".join("?" * len(turn_ids))
        self.conn.execute(
            f"UPDATE turns SET extracted=1 WHERE id IN ({placeholders})",
            turn_ids,
        )

    def fetch_by_ids(self, turn_ids: list[str]) -> list[dict[str, Any]]:
        if not turn_ids:
            return []
        marks = ",".join("?" * len(turn_ids))
        rows = self.conn.execute(
            f"SELECT * FROM turns WHERE id IN ({marks})",
            turn_ids,
        ).fetchall()
        return [dict(r) for r in rows]

    def count_unextracted(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM turns WHERE session_id=? AND extracted=0",
            (session_id,),
        ).fetchone()
        return int(row["c"])

    def unextracted_other(
        self, session_id: str, *, since: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        """User turns still unextracted in other sessions (spec §10.4)."""
        rows = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id != ? AND extracted = 0 AND role = 'user'
              AND content IS NOT NULL AND evicted = 0 AND created_at >= ?
            ORDER BY seq DESC LIMIT ?
            """,
            (session_id, since, limit),
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def unextracted_preview(self, session_id: str, limit: int = 6) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id=? AND extracted=0 AND role='user' AND content IS NOT NULL
            ORDER BY seq DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def search_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT t.*, bm25(turns_fts) AS score
            FROM turns_fts
            JOIN turns t ON t.seq = turns_fts.rowid
            WHERE turns_fts MATCH ? AND t.content IS NOT NULL
            ORDER BY score
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_protected(self, turn_ids: list[str]) -> int:
        if not turn_ids:
            return 0
        placeholders = ",".join("?" * len(turn_ids))
        cur = self.conn.execute(
            f"UPDATE turns SET protected=1 WHERE id IN ({placeholders})",
            turn_ids,
        )
        return int(cur.rowcount or 0)

    def used_bytes(self) -> int:
        try:
            page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
            freelist = self.conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
            return int((page_count - freelist) * page_size)
        except Exception:  # noqa: BLE001
            return 0

    def evict_oldest(self, *, target_bytes: int) -> int:
        """Null out oldest unprotected extracted turns until under target. Returns count."""
        n = 0
        while self.used_bytes() > target_bytes:
            row = self.conn.execute(
                """
                SELECT seq, id FROM turns
                WHERE content IS NOT NULL AND extracted=1 AND protected=0 AND evicted=0
                ORDER BY seq ASC LIMIT 1
                """
            ).fetchone()
            if not row:
                break
            self.conn.execute(
                "UPDATE turns SET content=NULL, evicted=1 WHERE id=?",
                (row["id"],),
            )
            try:
                self.conn.execute(
                    "INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', ?, NULL)",
                    (row["seq"],),
                )
            except sqlite3.Error:
                pass
            n += 1
            if n > 5000:
                break
        try:
            self.conn.execute("PRAGMA incremental_vacuum(64)")
        except sqlite3.Error:
            pass
        return n
