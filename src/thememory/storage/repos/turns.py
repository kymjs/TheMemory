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

    def count_unextracted(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM turns WHERE session_id=? AND extracted=0",
            (session_id,),
        ).fetchone()
        return int(row["c"])

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
