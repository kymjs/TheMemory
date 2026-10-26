"""FTS fallback, slot conflicts, deadline, residency, negation, doctor."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.client.fts_fallback import fallback_block, search_memory_fts
from thememory.config import load, write_default
from thememory.core.residency import decide_residency
from thememory.runtime.service import MemoryService
from thememory.installer.hook_template import HOOK_SOURCE
from thememory.paths import ensure_layout, memory_db_path
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


def test_hook_source_compiles() -> None:
    compile(HOOK_SOURCE, "hook.py", "exec")
    assert "_fts_block" in HOOK_SOURCE


def test_fts_fallback_reads_active_items(svc: MemoryService, tmp_path: Path) -> None:
    svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "note",
            "content": "postgres fifteen",
        }
    )
    rows = search_memory_fts(memory_db_path(tmp_path), "postgres")
    assert rows and "postgres" in rows[0]["content"]
    assert "降级检索" in fallback_block(memory_db_path(tmp_path), "")


def test_single_slot_conflict(svc: MemoryService) -> None:
    first = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "profile_fact",
            "slot_key": "identity.name",
            "content": "用户称呼张涛",
        }
    )
    lower = svc.remember(
        {
            "host": "cli",
            "origin": "agent_inferred",
            "type": "profile_fact",
            "slot_key": "identity.name",
            "content": "用户称呼李四",
        }
    )
    assert first["effect"] == "active_promoted"
    assert lower["effect"] == "held"
    assert lower["item"]["status"] == "candidate"
    winner = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "profile_fact",
            "slot_key": "identity.name",
            "content": "用户称呼主人",
        }
    )
    assert winner["effect"] == "active_promoted"
    old = MemoryRepo(svc.db.read_memory()).get(first["id"])
    assert old is not None
    assert old["status"] == "superseded"


def test_multi_slot_coexists(svc: MemoryService) -> None:
    svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "preference",
            "slot_key": "pref.tools",
            "content": "用户常用 git",
        }
    )
    second = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "preference",
            "slot_key": "pref.tools",
            "content": "用户常用 uv",
        }
    )
    assert second["item"]["status"] == "active"
    active = MemoryRepo(svc.db.read_memory()).list_active(limit=10)
    assert sum(1 for item in active if item.get("slot_key") == "pref.tools") == 2


def test_deadline_skips_vector(svc: MemoryService) -> None:
    class _Boom:
        model_id = "fake"

        def available(self) -> bool:
            raise AssertionError("vector path should be skipped")

        def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
            raise AssertionError("embed should be skipped")

    svc.embedder = _Boom()  # type: ignore[assignment]
    found = svc.search({"host": "cli", "query": "postgres", "deadline_ms": 0})
    assert found["reason"] == "deadline_partial"
    assert "vector" in found["degraded_paths"]


def test_negation_after_inject_counts(svc: MemoryService) -> None:
    remembered = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "preference",
            "content": "用户偏好深色主题",
            "session_ref": "harm",
        }
    )
    before = MemoryRepo(svc.db.read_memory()).count_harm_signals()
    svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "harm",
            "role": "user",
            "content": "还没注入",
            "turn_id": "n0",
        }
    )
    assert MemoryRepo(svc.db.read_memory()).count_harm_signals() == before
    svc.inject_session_start({"host": "cli", "session_ref": "harm"})
    svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "harm",
            "role": "user",
            "content": "这条记错了",
            "turn_id": "n1",
        }
    )
    assert MemoryRepo(svc.db.read_memory()).count_harm_signals() == before + 1
    assert remembered["id"]


def test_residency_thresholds() -> None:
    action, reason = decide_residency(
        mode="auto",
        available_gb=8,
        total_gb=16,
        footprint_gb=0.5,
        high_min_gb=2,
    )
    assert action == "pin"
    assert "常驻" in reason
    action, reason = decide_residency(
        mode="auto",
        available_gb=0.2,
        total_gb=16,
        footprint_gb=0.5,
        low_multiple=2,
        high_min_gb=2,
    )
    assert action == "unload"
    action, reason = decide_residency(
        mode="auto",
        available_gb=8,
        total_gb=1,
        footprint_gb=0.5,
        min_total_gb=4,
    )
    assert action == "on_demand"
    assert "下限" in reason


def test_doctor_reports_schema_and_residency(svc: MemoryService) -> None:
    report = svc.doctor()
    assert report["schema"]["memory"] >= 1
    assert report["residency"]["reason"]
    assert "daily_used" in report["llm"]


def test_mark_protected(svc: MemoryService) -> None:
    captured = svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "prot",
            "role": "user",
            "content": "留下证据",
            "turn_id": "p1",
        }
    )
    svc.db.run_write(
        "turns", lambda c: TurnsRepo(c).mark_protected([captured["turn_id"]])
    )
    row = svc.db.read_turns().execute(
        "SELECT protected FROM turns WHERE id=?", (captured["turn_id"],)
    ).fetchone()
    assert int(row["protected"]) == 1
