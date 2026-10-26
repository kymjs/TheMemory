"""Custom extract prompt, parse retry, and job backoff (spec §7.4 / §7.5)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.extraction import SYSTEM_PROMPT
from thememory.core.host_status import inspect_hosts
from thememory.runtime.service import MemoryService, parse_backoff_seconds
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo
from thememory.util import utc_now


class _LLM:
    def __init__(self, *, fail: bool = False) -> None:
        self.model = "fake"
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        if self.fail:
            raise ValueError("not json")
        match = re.search(r"\[(\S+) \| user \|", user)
        assert match
        return {
            "session_summary": "记了一句",
            "ops": [
                {
                    "op": "ADD",
                    "type": "note",
                    "scope": "user",
                    "content": "用户明天要写文档",
                    "origin": "extracted",
                    "evidence_turns": [match.group(1)],
                }
            ],
        }


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


def _capture(svc: MemoryService) -> str:
    captured = svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "s-extract",
            "role": "user",
            "content": "我明天要写文档",
            "tz": "Asia/Shanghai",
        }
    )
    return str(captured["session_id"])


def test_builtin_prompt_anchors_next_wednesday() -> None:
    assert "下周三" in SYSTEM_PROMPT
    assert "2026-10-28" in SYSTEM_PROMPT


def test_prompt_file_marks_custom(svc: MemoryService, tmp_path: Path) -> None:
    prompt = tmp_path / "custom-prompt.txt"
    prompt.write_text("CUSTOM PROMPT ONLY\n", encoding="utf-8")
    svc.cfg["extraction"]["prompt_file"] = str(prompt)
    llm = _LLM()
    svc.llm = llm  # type: ignore[assignment]
    svc.run_extract(_capture(svc))
    assert llm.calls[0][0] == "CUSTOM PROMPT ONLY"
    row = svc.db.read_memory().execute(
        "SELECT prompt_version FROM changes WHERE actor='llm'"
    ).fetchone()
    assert row["prompt_version"] == "custom"


def test_missing_prompt_file_falls_back(svc: MemoryService, tmp_path: Path) -> None:
    svc.cfg["extraction"]["prompt_file"] = str(tmp_path / "missing.txt")
    llm = _LLM()
    svc.llm = llm  # type: ignore[assignment]
    svc.run_extract(_capture(svc))
    assert "下周三" in llm.calls[0][0]
    reasons = {
        row["reason"]
        for row in svc.db.read_memory().execute("SELECT reason FROM degrade_events").fetchall()
    }
    assert "prompt_file_missing" in reasons


def test_parse_failure_retries_then_backs_off(svc: MemoryService) -> None:
    llm = _LLM(fail=True)
    svc.llm = llm  # type: ignore[assignment]
    session_id = _capture(svc)
    svc.db.run_write(
        "memory",
        lambda c: MemoryRepo(c).enqueue_job("extract", {"session_id": session_id}),
    )
    svc.process_jobs()
    assert len(llm.calls) == 2
    assert "上一次输出无法解析" in llm.calls[1][1]
    row = svc.db.read_memory().execute("SELECT * FROM jobs WHERE kind='extract'").fetchone()
    assert row["status"] == "pending"
    assert row["last_error"]
    assert str(row["next_run_at"]) > utc_now()
    svc.process_jobs()
    assert len(llm.calls) == 2
    report = svc.doctor()
    pending = [job for job in report["pending_jobs"] if job["kind"] == "extract"]
    assert pending
    assert pending[0]["next_run_at"]


def test_parse_failure_stops_after_cap(svc: MemoryService) -> None:
    llm = _LLM(fail=True)
    svc.llm = llm  # type: ignore[assignment]
    session_id = _capture(svc)

    def _enq(conn):  # type: ignore[no-untyped-def]
        jid = MemoryRepo(conn).enqueue_job("extract", {"session_id": session_id})
        conn.execute("UPDATE jobs SET attempts=5 WHERE id=?", (jid,))

    svc.db.run_write("memory", _enq)
    svc.process_jobs()
    row = svc.db.read_memory().execute(
        "SELECT status, last_error FROM jobs WHERE kind='extract'"
    ).fetchone()
    assert row["status"] == "failed"
    assert "not json" in row["last_error"]


def test_backoff_grows_then_caps() -> None:
    assert parse_backoff_seconds(1) == 30
    assert parse_backoff_seconds(2) == 60
    assert parse_backoff_seconds(20) == 6 * 3600


def test_codex_hook_trust_is_unknown(tmp_path: Path) -> None:
    home = tmp_path / "user"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "hooks.json").write_text(
        '{"hooks":{"UserPromptSubmit":[{"command":"thememory"}]}}',
        encoding="utf-8",
    )
    rows = {row["host"]: row for row in inspect_hosts(user_home=home)}
    assert rows["codex"]["installed"] is True
    assert rows["codex"]["hook_trust"] == "unknown"
    assert rows["codex"]["hook_trust_reason"] == "codex_trust_not_detectable"
    assert rows["claude-code"]["installed"] is False
