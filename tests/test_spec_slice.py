"""Project key, date search, extraction windows, R1, job recovery, incremental context."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.extraction import (
    _apply_r1_r2,
    apply_ops,
    build_user_prompt,
    should_skip_extract,
    split_windows,
)
from thememory.core.project_key import _normalize_remote, resolve_project_key
from thememory.core.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo
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


def test_project_key_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("thememory.core.project_key._git_remote", lambda _path: None)
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    assert resolve_project_key(str(left)) == resolve_project_key(str(left))
    assert resolve_project_key(str(left)) != resolve_project_key(str(right))
    assert resolve_project_key(None) is None
    assert _normalize_remote("git@github.com:Foo/Bar.git") == "github.com/foo/bar"
    assert _normalize_remote("https://github.com/Foo/Bar.git") == "github.com/foo/bar"


def test_session_stores_hashed_project(svc: MemoryService, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    session = svc.ensure_session(
        {"host": "cli", "session_ref": "p", "project_root": str(root)}
    )
    assert session["project_key"] == resolve_project_key(str(root))
    assert session["project_key"] != str(root)


def test_date_search_bypasses_text(svc: MemoryService) -> None:
    inside = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "plan",
            "content": "用户计划前往天津",
            "valid_from": "2026-10-28",
            "valid_to": "2026-11-01",
        }
    )
    svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "plan",
            "content": "用户计划留在上海",
            "valid_from": "2026-12-01",
            "valid_to": "2026-12-03",
        }
    )
    found = svc.search({"host": "cli", "date": "2026-10-29"})
    ids = [item["id"] for item in found["items"]]
    assert ids == [inside["id"]]
    assert found["items"][0]["matched_by"] == "date"


def test_r1_mixed_evidence_cannot_be_user_stated() -> None:
    turns = {
        "u": {"id": "u", "role": "user", "content": "我喜欢简洁"},
        "a": {"id": "a", "role": "assistant", "content": "用户喜欢简洁"},
    }
    mixed = {"origin": "user_stated", "quote": "我喜欢简洁", "evidence_turns": ["u", "a"]}
    assert _apply_r1_r2(mixed, turns) == "extracted"
    assistant_only = {"origin": "user_stated", "quote": "用户喜欢简洁", "evidence_turns": ["a"]}
    assert _apply_r1_r2(assistant_only, turns) == "agent_inferred"


def test_inverted_dates_are_cleared() -> None:
    seen: list[dict] = []

    def remember(op: dict, _turns: dict) -> dict:
        seen.append(op)
        return {"ok": True}

    apply_ops(
        [
            {
                "op": "ADD",
                "content": "行程",
                "origin": "extracted",
                "valid_from": "2026-11-02",
                "valid_to": "2026-10-01",
                "evidence_turns": ["u"],
            }
        ],
        turns=[{"id": "u", "role": "user", "content": "去", "local_date": "2026-10-22"}],
        remember_fn=remember,
        update_fn=lambda *_: None,
        retract_fn=lambda *_: None,
        reinforce_fn=lambda *_: None,
        tombstone_hashes=set(),
    )
    assert seen[0]["valid_from"] is None
    assert seen[0]["valid_to"] is None


def test_windows_keep_user_and_assistant_together() -> None:
    turns = [
        {"id": "u1", "role": "user", "content": "一二三四五六七八"},
        {"id": "a1", "role": "assistant", "content": "甲乙丙丁"},
        {"id": "u2", "role": "user", "content": "一二三四五六七八"},
        {"id": "a2", "role": "assistant", "content": "甲乙丙丁"},
    ]
    batches = split_windows(turns, 12)
    assert [t["id"] for t in batches[0]] == ["u1", "a1"]
    assert [t["id"] for t in batches[1]] == ["u2", "a2"]
    assert should_skip_extract({"content": "好的"}, ["好的", "谢谢"]) is True
    assert should_skip_extract({"content": "我明天去"}, ["好的"]) is False
    prompt = build_user_prompt(
        [{"id": "u1", "role": "user", "content": "去天津", "local_date": "2026-10-21", "tz": "Asia/Shanghai"}],
        [],
    )
    assert "2026-10-21" in prompt
    assert "周三" in prompt


def test_recover_running_jobs(svc: MemoryService) -> None:
    job_id = svc.db.run_write("memory", lambda c: MemoryRepo(c).enqueue_job("backup", {}))

    def _mark(conn):  # type: ignore[no-untyped-def]
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))

    svc.db.run_write("memory", _mark)
    assert svc.recover_jobs() == 1
    row = svc.db.read_memory().execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "pending"


def test_context_returns_delta_after_snapshot(svc: MemoryService) -> None:
    first = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "preference",
            "content": "用户偏好深色主题",
            "session_ref": "ctx",
        }
    )
    svc.inject_session_start({"host": "cli", "session_ref": "ctx"})
    second = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "preference",
            "content": "用户偏好等宽字体",
            "session_ref": "ctx",
        }
    )
    delta = svc.context({"host": "cli", "session_ref": "ctx"})
    assert delta["incremental"] is True
    assert "等宽字体" in delta["block"]
    assert "深色主题" not in delta["block"]
    full = svc.context({"host": "cli", "session_ref": "ctx", "force_full": True})
    assert full["incremental"] is False
    assert "深色主题" in full["block"]
    assert first["id"] != second["id"]


def test_topic_unchanged_skips(svc: MemoryService) -> None:
    class _Fake:
        model_id = "fake"

        def available(self) -> bool:
            return True

        def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
            if "同样" in texts[0]:
                return [[1.0, 0.0]]
            return [[0.0, 1.0]]

    svc.embedder = _Fake()  # type: ignore[assignment]
    session = svc.ensure_session({"host": "cli", "session_ref": "topic"})
    svc._per_turn_skip(session["id"], "同样的话题")
    svc._mark_injected(session["id"], "同样的话题")
    skipped = svc.inject_per_turn(
        {"host": "cli", "session_ref": "topic", "user_message": "同样的话题继续说"}
    )
    assert skipped["skipped_reason"] == "topic_unchanged"


def test_capture_writes_local_date(svc: MemoryService) -> None:
    svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "tz",
            "role": "user",
            "content": "记下本地日期",
            "turn_id": "ld1",
            "tz": "Asia/Shanghai",
        }
    )
    session = svc.ensure_session({"host": "cli", "session_ref": "tz"})
    rows = TurnsRepo(svc.db.read_turns()).recent_user_turns(session["id"], limit=1)
    assert rows[0]["local_date"]
    assert len(rows[0]["local_date"]) == 10
