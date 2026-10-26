from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.extraction import build_user_prompt
from thememory.runtime.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo
from thememory.storage.repos.turns import TurnsRepo
from thememory.util import shrink_code_blocks, truncate_turn


def test_shrink_and_truncate() -> None:
    text = "前\n```python\nprint(1)\nprint(2)\n```\n后"
    shrunk = shrink_code_blocks(text)
    assert "print(2)" not in shrunk or "CODE BLOCK" in shrunk
    assert "[CODE BLOCK lang=python" in shrunk
    assert truncate_turn("abcdef", 3).endswith("[TRUNCATED]")


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryService:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["turns"]["max_chars_per_turn"] = 80
    cfg["turns"]["keep_code_blocks"] = False
    cfg["retrieval"]["inject_min_interval_turns"] = 2
    cfg["llm"]["budget"]["daily_tokens"] = 20
    cfg["hosts"]["cli"] = {
        "write": True,
        "read": True,
        "isolated": False,
        "procedural_write": True,
    }
    return MemoryService(Database(tmp_path), cfg, root=tmp_path)


def test_capture_replaces_code_block(svc: MemoryService) -> None:
    svc.capture_turn(
        {
            "host": "cli",
            "session_ref": "code",
            "role": "user",
            "content": "看这段\n```py\nprint('secret-line')\n```\n",
            "turn_id": "cb1",
        }
    )
    sid = svc.ensure_session({"host": "cli", "session_ref": "code"})["id"]
    rows = TurnsRepo(svc.db.read_turns()).recent_user_turns(sid, limit=1)
    assert "[CODE BLOCK" in rows[0]["content"]
    assert "```" not in rows[0]["content"]


def test_budget_blocks_extract(svc: MemoryService) -> None:
    MemoryRepo(svc.db.read_memory())
    svc.db.run_write(
        "memory",
        lambda c: MemoryRepo(c).log_usage(
            purpose="extract",
            model="x",
            prompt_tokens=100,
            completion_tokens=10,
            ok=True,
        ),
    )
    allowed, reason = svc.llm_budget_allows()
    assert allowed is False
    assert reason == "daily"


def test_prior_summary_in_prompt() -> None:
    text = build_user_prompt([], [], prior_summary="上一窗讨论了天津")
    assert "上一窗口摘要" in text
    assert "天津" in text


def test_inject_interval(svc: MemoryService) -> None:
    session = svc.ensure_session({"host": "cli", "session_ref": "gap"})
    assert svc._per_turn_skip(session["id"], "第一次注入") is None
    svc._mark_injected(session["id"], "第一次注入")
    second = svc.inject_per_turn(
        {"host": "cli", "session_ref": "gap", "user_message": "再问一句别的话题"}
    )
    assert second.get("skipped_reason") == "interval"
