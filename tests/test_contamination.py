from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["hosts"]["cli"] = {
        "write": True,
        "read": True,
        "isolated": False,
        "procedural_write": True,
    }
    cfg["limits"] = {
        "session_max_writes": 100,
        "session_max_procedural": 50,
        "burst_window_seconds": 1,
        "burst_max_writes": 100,
    }
    return MemoryService(Database(tmp_path), cfg, root=tmp_path)


def test_tool_risk_marks_session_and_downgrades_remember(service: MemoryService) -> None:
    service.capture_event(
        {"host": "claude-code", "session_ref": "s-risk", "event": "session_start"}
    )
    marked = service.capture_event(
        {
            "host": "claude-code",
            "session_ref": "s-risk",
            "event": "tool_risk",
            "tool": "WebFetch",
        }
    )
    assert marked.get("contaminated") is True
    remembered = service.remember(
        {
            "host": "claude-code",
            "session_ref": "s-risk",
            "origin": "user_requested",
            "type": "preference",
            "content": "用户喜欢简洁回答",
        }
    )
    assert remembered["status"] == "ok"
    assert remembered["effect"] == "held"
    assert remembered["item"]["status"] == "candidate"
    assert "session_contaminated" in (remembered["item"].get("flags") or [])


def test_mcp_host_has_no_contamination_signal(service: MemoryService) -> None:
    marked = service.capture_event(
        {
            "host": "deepseek-harness",
            "session_ref": "s-mcp",
            "event": "tool_risk",
            "tool": "WebFetch",
        }
    )
    assert marked.get("contaminated") is False
    assert marked.get("reason") == "no_tool_signal"


def test_pause_blocks_capture_and_remember(service: MemoryService) -> None:
    service.set_pause(until="forever")
    assert service.is_paused()
    cap = service.capture_turn(
        {
            "host": "cli",
            "session_ref": "s-pause",
            "role": "user",
            "content": "hello",
            "turn_id": "t1",
        }
    )
    assert cap.get("accepted") is False
    rem = service.remember(
        {
            "host": "cli",
            "session_ref": "s-pause",
            "origin": "user_requested",
            "content": "should fail",
        }
    )
    assert rem["status"] == "failed"
    assert rem["reason"] == "incognito"
    service.set_pause(until=None)
    assert not service.is_paused()
