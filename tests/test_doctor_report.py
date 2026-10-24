"""Doctor shows config provenance, recent injections, and harm rows (spec §18.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.config.loader import explain_config
from thememory.core.service import MemoryService
from thememory.paths import config_path, ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo


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


def test_config_source_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_default(tmp_path)
    monkeypatch.setenv("THEMEMORY_EXTRACTION_TRIGGER_TURNS", "11")
    report = explain_config(tmp_path)
    assert report["error"] is None
    assert report["sources"]["extraction.trigger_turns"] == "env"
    assert report["sources"]["daemon.log_level"] == "file"


def test_config_unknown_key_is_reported(tmp_path: Path) -> None:
    write_default(tmp_path)
    path = config_path(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        'log_level = "info"\n',
        'log_level = "info"\nnope = 1\n',
        1,
    )
    path.write_text(text, encoding="utf-8")
    report = explain_config(tmp_path)
    assert report["error"]
    assert "daemon.nope" in str(report["error"])
    assert report["sources"]["daemon.nope"] == "file"


def test_doctor_lists_injection_and_harm(svc: MemoryService) -> None:
    remembered = svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "note",
            "content": "用户把钥匙放在抽屉里",
        }
    )

    def _mark(conn):  # type: ignore[no-untyped-def]
        repo = MemoryRepo(conn)
        repo.ledger_add("sess-1", remembered["id"], "per_turn")
        repo.record_change(
            item_id=remembered["id"],
            op="HARM",
            actor="rule",
            reason="user_negation",
        )

    svc.db.run_write("memory", _mark)
    report = svc.doctor()
    assert report["injections"][0]["item_id"] == remembered["id"]
    assert report["injections"][0]["channel"] == "per_turn"
    assert "钥匙" in report["injections"][0]["content"]
    assert report["harm_recent"][0]["op"] == "HARM"
    assert report["harm_signals"] >= 1
    assert "sources" in report["config"]
