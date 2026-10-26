"""Repository for memory_items, embeddings, tombstones, ledger, jobs."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from thememory.core.vectors import pack_vector, unpack_vector
from thememory.util import content_hash, json_list, new_id, parse_json_list, prepare_fts_text, utc_now


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["keywords"] = parse_json_list(d.get("keywords"))
    d["aliases"] = parse_json_list(d.get("aliases"))
    d["flags"] = parse_json_list(d.get("flags"))
    d["promoted"] = bool(d.get("promoted"))
    return d


class MemoryRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def find_by_hash(self, chash: str, observer: str | None = None) -> dict[str, Any] | None:
        if observer:
            row = self.conn.execute(
                "SELECT * FROM memory_items WHERE content_hash = ? AND observer = ? "
                "AND status NOT IN ('deleted','retracted') LIMIT 1",
                (chash, observer),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM memory_items WHERE content_hash = ? "
                "AND status NOT IN ('deleted','retracted') LIMIT 1",
                (chash,),
            ).fetchone()
        return _row_to_item(row) if row else None

    def insert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        item_id = item.get("id") or new_id()
        content = item["content"]
        fts = item.get("fts_text") or prepare_fts_text(
            content,
            " ".join(item.get("keywords") or []),
            " ".join(item.get("aliases") or []),
        )
        chash = item.get("content_hash") or content_hash(content)
        self.conn.execute(
            """
            INSERT INTO memory_items (
              id, peer_id, observer, scope, type, slot_key, content, source_quote,
              keywords, aliases, fts_text, origin, provenance, status, promoted,
              hold_reason, flags, evidence_count, last_reinforced_at, observed_at,
              valid_from, valid_to, supersedes_id, superseded_by, content_hash,
              source_host, source_session, created_at, updated_at
            ) VALUES (
              ?,?,?,?,?,?,?,?,
              ?,?,?,?,?,?,?,
              ?,?,?,?,?,
              ?,?,?,?,?,
              ?,?,?,?
            )
            """,
            (
                item_id,
                item.get("peer_id", "local"),
                item["observer"],
                item.get("scope", "user"),
                item["type"],
                item.get("slot_key"),
                content,
                item.get("source_quote"),
                json_list(item.get("keywords") or []),
                json_list(item.get("aliases") or []),
                fts,
                item["origin"],
                item.get("provenance", "owner"),
                item.get("status", "active"),
                1 if item.get("promoted") else 0,
                item.get("hold_reason"),
                json_list(item.get("flags") or []),
                int(item.get("evidence_count") or 0),
                item.get("last_reinforced_at"),
                item.get("observed_at") or now,
                item.get("valid_from"),
                item.get("valid_to"),
                item.get("supersedes_id"),
                item.get("superseded_by"),
                chash,
                item["source_host"],
                item.get("source_session"),
                now,
                now,
            ),
        )
        # FTS external content sync
        row = self.conn.execute("SELECT seq FROM memory_items WHERE id = ?", (item_id,)).fetchone()
        self.conn.execute(
            "INSERT INTO item_fts(rowid, fts_text) VALUES (?, ?)",
            (row["seq"], fts),
        )
        return self.get(item_id)  # type: ignore[return-value]

    def update_item(self, item_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get(item_id)
        allowed = {
            "content",
            "status",
            "promoted",
            "hold_reason",
            "flags",
            "evidence_count",
            "last_reinforced_at",
            "superseded_by",
            "supersedes_id",
            "slot_key",
            "type",
            "keywords",
            "aliases",
            "fts_text",
            "valid_from",
            "valid_to",
            "origin",
            "provenance",
            "content_hash",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"flags", "keywords", "aliases"} and not isinstance(value, str):
                value = json_list(value)
            if key == "promoted":
                value = 1 if value else 0
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(utc_now())
        vals.append(item_id)
        self.conn.execute(f"UPDATE memory_items SET {', '.join(sets)} WHERE id = ?", vals)
        if "content" in fields or "fts_text" in fields or "keywords" in fields:
            item = self.get(item_id)
            if item:
                fts = fields.get("fts_text") or prepare_fts_text(
                    item["content"],
                    " ".join(item.get("keywords") or []),
                    " ".join(item.get("aliases") or []),
                )
                self.conn.execute(
                    "UPDATE memory_items SET fts_text = ? WHERE id = ?", (fts, item_id)
                )
                self.conn.execute("DELETE FROM item_fts WHERE rowid = ?", (item["seq"],))
                self.conn.execute(
                    "INSERT INTO item_fts(rowid, fts_text) VALUES (?, ?)",
                    (item["seq"], fts),
                )
        return self.get(item_id)

    def list_promoted(
        self,
        *,
        observer: str | None = None,
        isolated: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM memory_items WHERE promoted = 1 AND status = 'active' "
        )
        args: list[Any] = []
        if isolated and observer:
            sql += "AND observer = ? "
            args.append(observer)
        sql += "ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        return [_row_to_item(r) for r in self.conn.execute(sql, args).fetchall()]

    def list_active(
        self,
        *,
        types: list[str] | None = None,
        scope: str | None = None,
        observer: str | None = None,
        isolated: bool = False,
        include_candidates: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        statuses = "('active','candidate')" if include_candidates else "('active')"
        sql = f"SELECT * FROM memory_items WHERE status IN {statuses} "
        args: list[Any] = []
        if types:
            placeholders = ",".join("?" * len(types))
            sql += f"AND type IN ({placeholders}) "
            args.extend(types)
        if scope:
            sql += "AND scope = ? "
            args.append(scope)
        if isolated and observer:
            sql += "AND observer = ? "
            args.append(observer)
        sql += "ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        return [_row_to_item(r) for r in self.conn.execute(sql, args).fetchall()]

    def search_fts(
        self,
        query: str,
        limit: int = 20,
        *,
        statuses: tuple[str, ...] = ("active",),
    ) -> list[tuple[dict[str, Any], float]]:
        if not query.strip():
            return []
        # escape FTS special chars simply by quoting
        q = '"' + query.replace('"', " ") + '"'
        marks = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"""
            SELECT m.*, bm25(item_fts) AS score
            FROM item_fts
            JOIN memory_items m ON m.seq = item_fts.rowid
            WHERE item_fts MATCH ? AND m.status IN ({marks})
            ORDER BY score
            LIMIT ?
            """,
            (q, *statuses, limit),
        ).fetchall()
        out: list[tuple[dict[str, Any], float]] = []
        for row in rows:
            item = _row_to_item(row)
            score = float(row["score"])
            out.append((item, score))
        return out

    def upsert_embedding(
        self, item_id: str, model_id: str, dim: int, vector: bytes
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO item_embeddings(item_id, model_id, dim, vector, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
              model_id=excluded.model_id, dim=excluded.dim,
              vector=excluded.vector, created_at=excluded.created_at
            """,
            (item_id, model_id, dim, vector, utc_now()),
        )

    def all_embeddings(self, model_id: str | None = None) -> list[tuple[str, bytes, int]]:
        if model_id:
            rows = self.conn.execute(
                "SELECT item_id, vector, dim FROM item_embeddings WHERE model_id = ?",
                (model_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT item_id, vector, dim FROM item_embeddings"
            ).fetchall()
        return [(r["item_id"], r["vector"], r["dim"]) for r in rows]

    def add_tombstone(
        self,
        item_id: str,
        type_: str,
        chash: str,
        vector: bytes | None = None,
        model_id: str | None = None,
        dim: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO tombstones(
              item_id, type, deleted_at, content_hash, model_id, dim, fp_vector
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (item_id, type_, utc_now(), chash, model_id, dim, vector),
        )

    def list_evidence(self, item_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT item_id, turn_id, role, quote FROM item_evidence WHERE item_id=? ORDER BY turn_id",
            (item_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def known_item_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT id FROM memory_items").fetchall()
        return {str(r["id"]) for r in rows}

    def tombstone_hashes(self) -> set[str]:
        rows = self.conn.execute("SELECT content_hash FROM tombstones").fetchall()
        return {r["content_hash"] for r in rows}

    def tombstone_fingerprints(self) -> list[tuple[bytes, int]]:
        rows = self.conn.execute(
            "SELECT fp_vector, dim FROM tombstones WHERE fp_vector IS NOT NULL AND dim IS NOT NULL"
        ).fetchall()
        return [(r["fp_vector"], int(r["dim"])) for r in rows]

    def record_change(
        self,
        *,
        item_id: str | None,
        op: str,
        actor: str,
        proposal: Any = None,
        result: Any = None,
        reason: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO changes(ts, item_id, op, actor, prompt_version, proposal, result, reason)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                utc_now(),
                item_id,
                op,
                actor,
                prompt_version,
                json.dumps(proposal, ensure_ascii=False) if proposal is not None else None,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                reason,
            ),
        )

    def add_degrade(
        self,
        component: str,
        reason: str,
        detail: str | None = None,
        host: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO degrade_events(ts, component, reason, detail, host, session_id)
            VALUES (?,?,?,?,?,?)
            """,
            (utc_now(), component, reason, detail, host, session_id),
        )

    def recent_degrades(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM degrade_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def enqueue_job(self, kind: str, payload: dict[str, Any]) -> int:
        now = utc_now()
        cur = self.conn.execute(
            """
            INSERT INTO jobs(kind, payload, status, attempts, created_at, updated_at)
            VALUES (?,?, 'pending', 0, ?, ?)
            """,
            (kind, json.dumps(payload, ensure_ascii=False), now, now),
        )
        return int(cur.lastrowid)

    def claim_jobs(self, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        now = utc_now()
        sql = (
            "SELECT * FROM jobs WHERE status = 'pending' "
            "AND (next_run_at IS NULL OR next_run_at <= ?) "
        )
        args: list[Any] = [now]
        if kind:
            sql += "AND kind = ? "
            args.append(kind)
        sql += "ORDER BY id ASC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            self.conn.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (utc_now(), row["id"]),
            )
            d = dict(row)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def requeue_job(
        self,
        job_id: int,
        error: str | None = None,
        *,
        next_run_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE jobs
            SET status='pending', last_error=?, next_run_at=?, updated_at=?
            WHERE id=?
            """,
            (error, next_run_at, utc_now(), job_id),
        )

    def usage_tokens_since(self, since: str) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)),0) AS n
            FROM usage_log WHERE ts >= ?
            """,
            (since,),
        ).fetchone()
        return int(row["n"])

    def log_usage(
        self,
        *,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        ok: bool,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO usage_log(ts, purpose, model, prompt_tokens, completion_tokens, ok, latency_ms)
            VALUES (?,?,?,?,?,?,?)
            """,
            (utc_now(), purpose, model, prompt_tokens, completion_tokens, 1 if ok else 0, None),
        )

    def finish_job(self, job_id: int, ok: bool, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, last_error=?, updated_at=? WHERE id=?",
            ("done" if ok else "failed", error, utc_now(), job_id),
        )

    def ledger_add(self, session_id: str, item_id: str, channel: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO injection_ledger(session_id, item_id, channel, injected_at)
            VALUES (?,?,?,?)
            """,
            (session_id, item_id, channel, utc_now()),
        )

    def ledger_has(self, session_id: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM injection_ledger WHERE session_id=? AND item_id=?",
            (session_id, item_id),
        ).fetchone()
        return row is not None

    def upsert_session(self, session: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        existing = self.conn.execute(
            "SELECT * FROM sessions WHERE host=? AND host_session_id=?",
            (session["host"], session["host_session_id"]),
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE sessions SET last_event_at=?, project_key=COALESCE(?, project_key) WHERE id=?",
                (now, session.get("project_key"), existing["id"]),
            )
            return dict(
                self.conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (existing["id"],)
                ).fetchone()
            )
        sid = session.get("id") or new_id()
        self.conn.execute(
            """
            INSERT INTO sessions(
              id, host, host_session_id, project_key, started_at, last_event_at,
              tz, incognito
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                sid,
                session["host"],
                session["host_session_id"],
                session.get("project_key"),
                now,
                now,
                session.get("tz"),
                1 if session.get("incognito") else 0,
            ),
        )
        return dict(self.conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def get_session_by_host(self, host: str, host_session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE host=? AND host_session_id=?",
            (host, host_session_id),
        ).fetchone()
        return dict(row) if row else None

    def save_snapshot(self, session_id: str, block: str, snap_hash: str, seq: int) -> None:
        self.conn.execute(
            """
            UPDATE sessions SET snapshot=?, snapshot_hash=?, snapshot_seq=?, last_event_at=?
            WHERE id=?
            """,
            (block, snap_hash, seq, utc_now(), session_id),
        )

    def end_session(self, session_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET ended_at=?, last_event_at=? WHERE id=?",
            (utc_now(), utc_now(), session_id),
        )

    def set_summary(self, session_id: str, summary: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET summary=?, summary_at=? WHERE id=?",
            (summary, utc_now(), session_id),
        )

    def max_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq),0) AS m FROM memory_items").fetchone()
        return int(row["m"])

    def count_session_writes(
        self,
        *,
        host: str,
        session_ref: str,
        since: str | None = None,
        type_: str | None = None,
    ) -> int:
        sql = """
            SELECT COUNT(*) AS c FROM memory_items
            WHERE source_host=? AND source_session=? AND status != 'deleted'
        """
        args: list[Any] = [host, session_ref]
        if since:
            sql += " AND created_at >= ?"
            args.append(since)
        if type_:
            sql += " AND type = ?"
            args.append(type_)
        row = self.conn.execute(sql, args).fetchone()
        return int(row["c"])

    def list_open_sessions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY last_event_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def enqueue_inbox(
        self, *, host: str, idem_key: str, payload: dict[str, Any]
    ) -> bool:
        """Insert inbox row; return False if idem_key already exists."""
        try:
            self.conn.execute(
                """
                INSERT INTO inbox(received_at, host, idem_key, payload, status)
                VALUES (?,?,?,?,'pending')
                """,
                (utc_now(), host, idem_key, json.dumps(payload, ensure_ascii=False)),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def claim_inbox(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT * FROM inbox WHERE status='pending' ORDER BY id ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            self.conn.execute(
                "UPDATE inbox SET status='running' WHERE id=?",
                (row["id"],),
            )
            d = dict(row)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def finish_inbox(self, inbox_id: int, *, ok: bool) -> None:
        self.conn.execute(
            "UPDATE inbox SET status=? WHERE id=?",
            ("done" if ok else "failed", inbox_id),
        )

    def expire_plans(self, today: str) -> int:
        """Turn plan items with valid_to < today into event. Returns count."""
        rows = self.conn.execute(
            """
            SELECT id FROM memory_items
            WHERE type='plan' AND status='active' AND valid_to IS NOT NULL AND valid_to < ?
            """,
            (today,),
        ).fetchall()
        n = 0
        for row in rows:
            self.update_item(row["id"], type="event")
            self.record_change(item_id=row["id"], op="EXPIRE", actor="rule", reason="plan_past_valid_to")
            n += 1
        return n

    def cleanup_candidates(self, cutoff: str) -> int:
        """Retract candidates older than cutoff (ISO ts). Returns count."""
        rows = self.conn.execute(
            """
            SELECT id FROM memory_items
            WHERE status='candidate' AND created_at < ?
            """,
            (cutoff,),
        ).fetchall()
        n = 0
        for row in rows:
            self.update_item(row["id"], status="retracted", promoted=False)
            self.record_change(
                item_id=row["id"], op="RETRACT", actor="rule", reason="candidate_ttl"
            )
            n += 1
        return n

    def mark_contaminated(self, session_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET contaminated=1, contaminated_at=? WHERE id=?",
            (utc_now(), session_id),
        )

    def clear_contamination(self, session_id: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET contaminated=0, contaminated_at=NULL WHERE id=?",
            (session_id,),
        )

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM daemon_meta WHERE key=?", (key,)
        ).fetchone()
        return str(row["value"]) if row else None

    def meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO daemon_meta(key, value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def meta_delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM daemon_meta WHERE key=?", (key,))

    def list_items(
        self,
        *,
        type_: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memory_items WHERE 1=1"
        args: list[Any] = []
        if type_:
            sql += " AND type=?"
            args.append(type_)
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if status:
            sql += " AND status=?"
            args.append(status)
        if since:
            sql += " AND created_at>=?"
            args.append(since)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        return [_row_to_item(r) for r in self.conn.execute(sql, args).fetchall()]

    def recent_changes(self, *, since: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM changes WHERE 1=1"
        args: list[Any] = []
        if since:
            sql += " AND ts>=?"
            args.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def recover_running_jobs(self) -> int:
        cur = self.conn.execute(
            "UPDATE jobs SET status='pending', last_error=?, updated_at=? WHERE status='running'",
            ("recovered_after_restart", utc_now()),
        )
        return int(cur.rowcount or 0)

    def items_changed_since(self, seq: int, updated_after: str | None) -> list[dict[str, Any]]:
        if updated_after:
            rows = self.conn.execute(
                """
                SELECT * FROM memory_items
                WHERE seq > ? OR updated_at > ?
                ORDER BY seq ASC
                """,
                (seq, updated_after),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memory_items WHERE seq > ? ORDER BY seq ASC",
                (seq,),
            ).fetchall()
        return [_row_to_item(r) for r in rows]

    def recent_summaries(self, limit: int = 3) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, summary, summary_at FROM sessions
            WHERE summary IS NOT NULL AND summary != ''
            ORDER BY summary_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_ledger_item(self, session_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT item_id FROM injection_ledger
            WHERE session_id=?
            ORDER BY injected_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return str(row["item_id"]) if row else None

    def degrade_counts(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT component, reason, COUNT(*) AS n
            FROM degrade_events
            GROUP BY component, reason
            ORDER BY n DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_injections(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT l.session_id, l.item_id, l.channel, l.injected_at,
                   substr(m.content, 1, 120) AS content
            FROM injection_ledger l
            LEFT JOIN memory_items m ON m.id = l.item_id
            ORDER BY l.injected_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_harm(self, limit: int = 5) -> list[dict[str, Any]]:
        """Harm after injection in the same session (spec §18.3), plus HARM op rows."""
        rows = self.conn.execute(
            """
            SELECT c.ts, c.item_id, c.op, c.actor, c.reason, l.session_id, l.injected_at
            FROM changes c
            LEFT JOIN injection_ledger l
              ON l.item_id = c.item_id
             AND l.injected_at <= c.ts
            WHERE c.op IN ('CORRECT', 'FORGET', 'RETRACT', 'HARM')
              AND (
                c.op = 'HARM'
                OR l.item_id IS NOT NULL
              )
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_harm_signals(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM (
              SELECT c.id
              FROM changes c
              LEFT JOIN injection_ledger l
                ON l.item_id = c.item_id
               AND l.injected_at <= c.ts
              WHERE c.op IN ('CORRECT','FORGET','RETRACT','HARM')
                AND (c.op = 'HARM' OR l.item_id IS NOT NULL)
            )
            """
        ).fetchone()
        return int(row["c"])
