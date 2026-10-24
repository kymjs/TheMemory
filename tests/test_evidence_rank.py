"""Evidence quotes, op checks, distinct-day promotion, query vector cache, recency."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.extraction import apply_ops, evidence_span
from thememory.core.retrieval import recency_factor
from thememory.core.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database


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


def test_show_includes_quote(svc: MemoryService) -> None:
    remembered = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "preference",
            "content": "用户偏好深色主题",
        }
    )

    def _ev(conn):  # type: ignore[no-untyped-def]
        conn.execute(
            "INSERT INTO item_evidence(item_id, turn_id, role, quote) VALUES (?,?,?,?)",
            (remembered["id"], "t1", "user", "我喜欢深色"),
        )

    svc.db.run_write("memory", _ev)
    shown = svc.show_memory({"id": remembered["id"]})
    assert shown["evidence"][0]["quote"] == "我喜欢深色"


def test_apply_ops_truncates_and_rejects_unknown_target() -> None:
    seen: list[dict] = []

    def remember(op: dict, _turns: dict) -> dict:
        seen.append(op)
        return {"ok": True}

    turns = [{"id": "u", "role": "user", "content": "去", "local_date": "2026-10-22", "session_id": "s"}]
    long = "记" * 400
    apply_ops(
        [
            {"op": "ADD", "content": long, "origin": "extracted", "evidence_turns": ["u"]},
            {
                "op": "RETRACT",
                "target_id": "missing",
                "evidence_turns": ["u"],
            },
        ],
        turns=turns,
        remember_fn=remember,
        update_fn=lambda *_: None,
        retract_fn=lambda *_: None,
        reinforce_fn=lambda *_: None,
        tombstone_hashes=set(),
        known_ids={"real"},
    )
    assert len(seen) == 1
    assert len(seen[0]["content"]) == 300


def test_same_day_does_not_repeat() -> None:
    turns = [
        {"id": "a", "role": "user", "local_date": "2026-10-22", "session_id": "s"},
        {"id": "b", "role": "user", "local_date": "2026-10-22", "session_id": "s"},
        {"id": "c", "role": "assistant", "local_date": "2026-10-23", "session_id": "s"},
        {"id": "d", "role": "user", "local_date": "2026-10-23", "session_id": "s"},
    ]
    assert evidence_span(turns) == 2


def test_query_vector_is_cached(svc: MemoryService) -> None:
    calls = {"n": 0}

    class _Fake:
        model_id = "fake"

        def available(self) -> bool:
            return True

        def files_present(self) -> bool:
            return False

        def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
            calls["n"] += 1
            return [[1.0, 0.0]]

        def loaded(self) -> bool:
            return True

    svc.embedder = _Fake()  # type: ignore[assignment]
    svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "note",
            "content": "postgres fifteen",
        }
    )
    calls["n"] = 0
    svc.search({"host": "cli", "query": "postgres fifteen"})
    svc.search({"host": "cli", "query": "postgres fifteen"})
    assert calls["n"] == 1


def test_ongoing_plan_outranks_old_note() -> None:
    assert recency_factor(
        {"type": "plan", "valid_from": "2026-10-20", "valid_to": "2026-10-30"},
        "2026-10-24",
    ) == 1.3
    old = recency_factor({"type": "note", "observed_at": "2020-01-01T00:00:00.000Z"}, "2026-10-24")
    assert old < 0.7
