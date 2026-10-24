"""Host-agnostic adapter conformance cases (spec §20.2), exercised on the core service."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.service import MemoryService
from thememory.installer.capabilities import HOST_CAPABILITIES
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo
from thememory.storage.repos.turns import TurnsRepo

HOSTS = list(HOST_CAPABILITIES)


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("THEMEMORY_INCOGNITO", raising=False)
    ensure_layout(tmp_path)
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["limits"] = {
        "session_max_writes": 100,
        "session_max_procedural": 50,
        "burst_window_seconds": 1,
        "burst_max_writes": 100,
    }
    cfg["hosts"]["cli"] = {
        "write": True,
        "read": True,
        "isolated": False,
        "procedural_write": True,
    }
    return MemoryService(Database(tmp_path), cfg, root=tmp_path)


@pytest.mark.parametrize("host", HOSTS)
def test_capture_idempotent(svc: MemoryService, host: str) -> None:
    params = {
        "host": host,
        "session_ref": "c1",
        "role": "user",
        "content": "记住我喜欢简洁",
        "turn_id": f"turn-1-{host}",
    }
    first = svc.capture_turn(params)
    second = svc.capture_turn(params)
    assert first["accepted"] is True
    assert second["deduped"] is True
    assert first["turn_id"] == second["turn_id"]


@pytest.mark.parametrize("host", HOSTS)
def test_private_fragment_stripped(svc: MemoryService, host: str) -> None:
    svc.capture_turn(
        {
            "host": host,
            "session_ref": "c2",
            "role": "user",
            "content": "可见 <private>密钥 sk-abcdefghijklmnopqrstuv</private> 结束",
            "turn_id": f"turn-2-{host}",
        }
    )
    rows = TurnsRepo(svc.db.read_turns()).recent_user_turns(
        svc.ensure_session({"host": host, "session_ref": "c2"})["id"],
        limit=1,
    )
    text = rows[0]["content"]
    assert "密钥" not in text
    assert "sk-abcdefghijklmnopqrstuv" not in text
    assert "可见" in text


@pytest.mark.parametrize("host", HOSTS)
def test_incognito_drops_turn(
    svc: MemoryService, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("THEMEMORY_INCOGNITO", "1")
    res = svc.capture_turn(
        {
            "host": host,
            "session_ref": "c3",
            "role": "user",
            "content": "不要记这条",
            "turn_id": f"turn-3-{host}",
        }
    )
    assert res["accepted"] is False
    assert res["reason"] == "incognito"


@pytest.mark.parametrize("host", HOSTS)
def test_snapshot_frozen_and_forget_absent(svc: MemoryService, host: str) -> None:
    remembered = svc.remember(
        {
            "host": host,
            "origin": "user_requested",
            "type": "preference",
            "content": "用户偏好深色主题",
            "session_ref": "c4",
        }
    )
    first = svc.inject_session_start({"host": host, "session_ref": "snap"})
    assert "深色主题" in (first.get("block") or "")
    second = svc.inject_session_start({"host": host, "session_ref": "snap"})
    assert second.get("frozen") is True
    assert second["snapshot_hash"] == first["snapshot_hash"]
    svc.forget({"host": host, "id": remembered["id"]})
    fresh = svc.inject_session_start({"host": host, "session_ref": "snap-2"})
    assert "深色主题" not in (fresh.get("block") or "")


@pytest.mark.parametrize("host", HOSTS)
def test_ledger_does_not_reinject(svc: MemoryService, host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    remembered = svc.remember(
        {
            "host": host,
            "origin": "user_requested",
            "type": "preference",
            "content": "用户偏好深色主题",
            "session_ref": "led",
        }
    )
    session = svc.ensure_session({"host": host, "session_ref": "led"})
    item_id = str(remembered["id"])
    svc.db.run_write(
        "memory",
        lambda conn: MemoryRepo(conn).ledger_add(session["id"], item_id, "per_turn"),
    )

    def _search(_params: dict) -> dict:
        return {
            "status": "ok",
            "items": [{"id": item_id, "content": "用户偏好深色主题", "type": "preference"}],
            "degraded_paths": [],
        }

    monkeypatch.setattr(svc, "search", _search)
    again = svc.inject_per_turn(
        {"host": host, "session_ref": "led", "user_message": "深色主题怎么配"}
    )
    assert again["item_count"] == 0
    assert again["skipped_reason"] == "ledger_dedupe"
