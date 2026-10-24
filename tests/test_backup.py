from __future__ import annotations

from pathlib import Path

import pytest

from thememory.paths import backups_dir, ensure_layout, memory_db_path, turns_db_path
from thememory.storage.backup import latest_backup_stamp, prune_backups, run_backup
from thememory.storage.db import Database


def test_run_backup_and_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    Database(tmp_path).close()
    assert memory_db_path(tmp_path).exists()
    assert turns_db_path(tmp_path).exists()

    first = run_backup(tmp_path, keep=2)
    assert Path(first["memory"]).exists()
    assert Path(first["turns"]).exists()
    assert latest_backup_stamp(tmp_path) == first["stamp"]

    # create extra stamped files and prune
    bdir = backups_dir(tmp_path)
    (bdir / "memory-20200101T000000Z.db").write_bytes(b"x")
    (bdir / "turns-20200101T000000Z.db").write_bytes(b"x")
    (bdir / "memory-20200102T000000Z.db").write_bytes(b"x")
    (bdir / "turns-20200102T000000Z.db").write_bytes(b"x")
    prune_backups(tmp_path, keep=2)
    remaining = sorted(p.name for p in bdir.iterdir())
    assert "memory-20200101T000000Z.db" not in remaining
    assert "turns-20200101T000000Z.db" not in remaining
    assert len([n for n in remaining if n.startswith("memory-")]) == 2
