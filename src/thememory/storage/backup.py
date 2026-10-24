"""Periodic SQLite online backup for memory.db and turns.db (spec §13.6)."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from thememory.paths import backups_dir, memory_db_path, turns_db_path

logger = logging.getLogger(__name__)

_STAMP_RE = re.compile(r"^(memory|turns)-(\d{8}T\d{6}Z)\.db$")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _copy_db(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise FileNotFoundError(str(src))
    src_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def prune_backups(root: Path | None, *, keep: int) -> list[Path]:
    """Keep the newest `keep` stamps for each of memory/turns; delete older."""
    base = backups_dir(root)
    if not base.exists() or keep < 1:
        return []
    by_kind: dict[str, list[tuple[str, Path]]] = {"memory": [], "turns": []}
    for path in base.iterdir():
        m = _STAMP_RE.match(path.name)
        if not m:
            continue
        by_kind[m.group(1)].append((m.group(2), path))
    removed: list[Path] = []
    for kind, items in by_kind.items():
        items.sort(key=lambda x: x[0], reverse=True)
        for _, path in items[keep:]:
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                logger.warning("failed to prune backup %s: %s", path, exc)
    return removed


def run_backup(root: Path | None, *, keep: int = 7) -> dict[str, str]:
    """Snapshot memory.db and turns.db; prune to `keep` stamps. Returns paths."""
    stamp = _stamp()
    out_dir = backups_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    memory_dest = out_dir / f"memory-{stamp}.db"
    turns_dest = out_dir / f"turns-{stamp}.db"
    _copy_db(memory_db_path(root), memory_dest)
    _copy_db(turns_db_path(root), turns_dest)
    prune_backups(root, keep=keep)
    return {"memory": str(memory_dest), "turns": str(turns_dest), "stamp": stamp}


def latest_backup_stamp(root: Path | None) -> str | None:
    base = backups_dir(root)
    if not base.exists():
        return None
    stamps: list[str] = []
    for path in base.iterdir():
        m = _STAMP_RE.match(path.name)
        if m:
            stamps.append(m.group(2))
    return max(stamps) if stamps else None
