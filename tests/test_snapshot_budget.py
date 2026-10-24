"""Snapshot fill order under a character budget, and turns eviction guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.service import MemoryService
from thememory.core.snapshot import render_snapshot
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.turns import TurnsRepo


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("THEMEMORY_INCOGNITO", raising=False)
    ensure_layout(tmp_path)
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["hosts"]["cli"] = {
        "write": True,
        "read": True,
        "isolated": False,
        "procedural_write": True,
    }
    return MemoryService(Database(tmp_path), cfg, root=tmp_path)


def test_budget_keeps_slot_and_higher_origin() -> None:
    items = [
        {
            "seq": 1,
            "type": "note",
            "scope": "user",
            "origin": "agent_inferred",
            "content": "低优先级的一条很长的推断说明文字",
            "observed_at": "2026-10-01T00:00:00.000Z",
        },
        {
            "seq": 2,
            "type": "preference",
            "scope": "user",
            "origin": "user_stated",
            "slot_key": "preference.theme",
            "content": "深色",
            "observed_at": "2026-10-02T00:00:00.000Z",
            "evidence_count": 1,
        },
        {
            "seq": 3,
            "type": "preference",
            "scope": "user",
            "origin": "user_requested",
            "content": "简洁",
            "observed_at": "2026-10-03T00:00:00.000Z",
            "evidence_count": 4,
            "last_reinforced_at": "2026-10-20T00:00:00.000Z",
        },
    ]
    truncated: list[str] = []
    text, _digest = render_snapshot(
        items,
        today="2026-10-24",
        tz="Asia/Shanghai",
        budgets={"budget_profile": 80, "budget_summary": 0, "budget_unextracted": 0},
        truncated=truncated,
    )
    assert "深色" in text
    assert "简洁" in text
    assert "低优先级" not in text
    assert truncated == ["用户画像"]


def test_evict_skips_unextracted_and_protected(svc: MemoryService) -> None:
    kept = svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "ev",
            "role": "user",
            "content": "还没提取不能删",
            "turn_id": "keep-raw",
        }
    )
    old = svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "ev",
            "role": "user",
            "content": "已经提取可以清掉",
            "turn_id": "drop-me",
        }
    )
    locked = svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "ev",
            "role": "user",
            "content": "证据轮次要留着",
            "turn_id": "protect-me",
        }
    )

    def _mark(conn):  # type: ignore[no-untyped-def]
        repo = TurnsRepo(conn)
        conn.execute("UPDATE turns SET extracted=1 WHERE id=?", (old["turn_id"],))
        conn.execute("UPDATE turns SET extracted=1 WHERE id=?", (locked["turn_id"],))
        repo.mark_protected([locked["turn_id"]])
        return repo.evict_oldest(target_bytes=1)

    svc.db.run_write("turns", _mark)
    rows = {
        row["id"]: row
        for row in svc.db.read_turns().execute("SELECT id, content, evicted FROM turns").fetchall()
    }
    assert rows[kept["turn_id"]]["content"] == "还没提取不能删"
    assert rows[locked["turn_id"]]["content"] == "证据轮次要留着"
    assert rows[old["turn_id"]]["content"] is None
    assert rows[old["turn_id"]]["evicted"] == 1

    svc.cfg["turns"]["max_size_gb"] = 0
    result = svc.maybe_evict_turns()
    assert result["still_over"] is True
    reasons = {
        row["reason"]
        for row in svc.db.read_memory().execute("SELECT reason FROM degrade_events").fetchall()
    }
    assert "turns_backlog" in reasons
