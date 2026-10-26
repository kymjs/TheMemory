"""SQLite connections, migrations, and a simple write queue."""

from __future__ import annotations

import shutil
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


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Independent read-only connection (spec §3.3). Falls back to RW if file missing."""
    if not path.exists():
        return _connect(path)
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _read_sql(name: str) -> str:
    return (_MIGRATIONS / name).read_text(encoding="utf-8")


def _backup_before_migrate(path: Path, version: int) -> Path | None:
    """Copy memory.db.pre-v{N}.bak before schema bumps (spec §4)."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    dest = path.with_name(f"{path.name}.pre-v{version}.bak")
    try:
        shutil.copy2(path, dest)
    except OSError:
        return None
    return dest


def migrate_memory(conn: sqlite3.Connection, path: Path | None = None) -> None:
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
        if path is not None:
            _backup_before_migrate(path, 2)
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
        mem_path = memory_db_path(root)
        turns_path = turns_db_path(root)
        self.memory = _connect(mem_path)
        self.turns = _connect(turns_path, incremental_vacuum=True)
        self._write_lock = threading.Lock()
        migrate_memory(self.memory, mem_path)
        migrate_turns(self.turns)
        # Separate read-only handles so readers never share the write connection (spec §3.3).
        self._memory_ro = _connect_readonly(mem_path)
        self._turns_ro = _connect_readonly(turns_path)

    def close(self) -> None:
        for conn in (self._memory_ro, self._turns_ro, self.memory, self.turns):
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

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
        return self._memory_ro

    def read_turns(self) -> sqlite3.Connection:
        return self._turns_ro

    def run_write(self, which: str, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.write(which) as conn:
            return fn(conn)
