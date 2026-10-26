"""Storage migration and remember roundtrip."""

from __future__ import annotations

from pathlib import Path

from thememory.config import load, write_default
from thememory.runtime.service import MemoryService
from thememory.storage.db import Database


def test_migrate_and_remember(tmp_path: Path) -> None:
    write_default(tmp_path)
    cfg = load(tmp_path)
    db = Database(tmp_path)
    svc = MemoryService(db, cfg, root=tmp_path)
    res = svc.remember(
        {
            "host": "cli",
            "content": "用户偏好简洁回答",
            "origin": "user_requested",
            "type": "preference",
        }
    )
    assert res["status"] == "ok"
    assert res["effect"] == "active_promoted"
    found = svc.search({"host": "cli", "query": "简洁"})
    assert found["status"] in {"ok", "degraded"}
    assert any("简洁" in i["content"] for i in found["items"])
    ctx = svc.context({"host": "cli"})
    assert "简洁" in ctx["block"]
    db.close()


def test_forget_tombstone(tmp_path: Path) -> None:
    write_default(tmp_path)
    cfg = load(tmp_path)
    db = Database(tmp_path)
    svc = MemoryService(db, cfg, root=tmp_path)
    res = svc.remember(
        {"host": "cli", "content": "临时秘密偏好xyz", "origin": "user_requested"}
    )
    item_id = res["id"]
    forgotten = svc.forget({"host": "cli", "id": item_id})
    assert forgotten["effect"] == "deleted"
    again = svc.remember(
        {"host": "cli", "content": "临时秘密偏好xyz", "origin": "user_requested"}
    )
    assert again.get("effect") == "suppressed"
    db.close()
