from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.runtime.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["limits"] = {
        "session_max_writes": 3,
        "session_max_procedural": 1,
        "burst_window_seconds": 60,
        "burst_max_writes": 2,
    }
    cfg["hosts"]["cli"] = {
        "write": True,
        "read": True,
        "isolated": False,
        "procedural_write": True,
    }
    return MemoryService(Database(tmp_path), cfg, root=tmp_path)


def test_session_max_writes(service: MemoryService) -> None:
    params = {
        "host": "cli",
        "session_ref": "s1",
        "origin": "user_requested",
        "type": "note",
    }
    assert service.remember({**params, "content": "a1"})["status"] == "ok"
    assert service.remember({**params, "content": "a2"})["status"] == "ok"
    assert service.remember({**params, "content": "a3"})["status"] == "ok"
    denied = service.remember({**params, "content": "a4"})
    assert denied["status"] == "failed"
    assert denied["reason"] == "session_max_writes"


def test_burst_isolates_as_candidate(service: MemoryService) -> None:
    params = {
        "host": "cli",
        "session_ref": "s2",
        "origin": "user_requested",
        "type": "preference",
    }
    r1 = service.remember({**params, "content": "偏好一"})
    r2 = service.remember({**params, "content": "偏好二"})
    r3 = service.remember({**params, "content": "偏好三"})
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"
    assert r3["status"] == "ok"
    assert r3["effect"] == "held"
    assert r3["item"]["status"] == "candidate"
    assert "rate_limited" in (r3["item"].get("flags") or [])


def test_procedural_session_cap(service: MemoryService) -> None:
    params = {
        "host": "cli",
        "session_ref": "s3",
        "origin": "user_requested",
        "type": "procedural",
    }
    first = service.remember({**params, "content": "提交前跑 lint"})
    assert first["status"] == "ok"
    second = service.remember({**params, "content": "提交前跑测试"})
    assert second["status"] == "failed"
    assert second["reason"] == "session_max_procedural"
