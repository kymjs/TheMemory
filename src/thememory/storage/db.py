"""SQLite connections, migrations, and a simple write queue."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from thememory.paths import ensure_layout, memory_db_path, turns_db_path
from thememory.util import utc_now

T = TypeVar("T")

SCHEMA_VERSION = 2
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def _connect(path: Path, *, incremental_vacuum: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if fresh and incremental_vacuum:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _read_sql(name: str) -> str:
    return (_MIGRATIONS / name).read_text(encoding="utf-8")


def migrate_memory(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        conn.executescript(_read_sql("001_initial.sql"))
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )
        return
    ver = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"] or 0
    if ver < 2:
        # Idempotent column adds for DBs created at v1
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "contaminated" not in cols:
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN contaminated INTEGER NOT NULL DEFAULT 0"
            )
        if "contaminated_at" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN contaminated_at TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_meta (
              key   TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (2, utc_now()),
        )


def migrate_turns(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        conn.executescript(_read_sql("001_turns.sql"))
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )


class Database:
    """Holds memory + turns connections and serializes writes."""

    def __init__(self, root: Path | None = None) -> None:
        ensure_layout(root)
        self.root = root
        self.memory = _connect(memory_db_path(root))
        self.turns = _connect(turns_db_path(root), incremental_vacuum=True)
        self._write_lock = threading.Lock()
        migrate_memory(self.memory)
        migrate_turns(self.turns)

    def close(self) -> None:
        self.memory.close()
        self.turns.close()

    @contextmanager
    def write(self, which: str = "memory") -> Iterator[sqlite3.Connection]:
        conn = self.memory if which == "memory" else self.turns
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def read_memory(self) -> sqlite3.Connection:
        return self.memory

    def read_turns(self) -> sqlite3.Connection:
        return self.turns

    def run_write(self, which: str, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.write(which) as conn:
            return fn(conn)
