"""Inject thresholds, project scope, tombstone cosine, stable-slot promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.core.extraction import PROMPT_VERSION, promote_decision
from thememory.core.retrieval import (
    filter_fts_by_threshold,
    scope_visible,
    tombstone_similar,
)
from thememory.runtime.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import pack_vector


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


def test_bm25_threshold_and_keyword_bypass() -> None:
    strong = ({"id": "a", "keywords": [], "aliases": []}, -3.0)
    weak = ({"id": "b", "keywords": [], "aliases": []}, -0.5)
    alias = ({"id": "c", "keywords": ["天津"], "aliases": []}, -0.1)
    kept = filter_fts_by_threshold([strong, weak, alias], 2.0, query="天津")
    assert [item["id"] for item, _score in kept] == ["a", "c"]


def test_other_project_is_hidden(svc: MemoryService) -> None:
    svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "note",
            "scope": "project:alpha",
            "content": "alpha-only postgres note",
        }
    )
    svc.remember(
        {
            "host": "cli",
            "origin": "user_requested",
            "type": "note",
            "scope": "user",
            "content": "shared postgres note",
        }
    )
    plain = svc.search({"host": "cli", "query": "postgres"})
    assert all(item["scope"] == "user" for item in plain["items"])
    assert any("shared" in item["content"] for item in plain["items"])
    scoped = svc.search({"host": "cli", "query": "postgres", "scope": "project:alpha"})
    assert any(item["scope"] == "project:alpha" for item in scoped["items"])
    assert scope_visible({"scope": "project:beta"}, "project:alpha") is False


def test_tombstone_cosine_suppresses_near_copy() -> None:
    original = [1.0, 0.0, 0.0]
    close = [0.99, 0.14, 0.0]
    far = [0.0, 1.0, 0.0]
    prints = [(pack_vector(original), 3)]
    assert tombstone_similar(close, prints, tau=0.95) is True
    assert tombstone_similar(far, prints, tau=0.95) is False


def test_stable_slot_promotes_once() -> None:
    assert promote_decision("user_stated", 1, 2, "profile_fact", stable=True) == ("active", True)
    assert promote_decision("user_stated", 1, 2, "preference", stable=False) == ("active", False)
    assert promote_decision("user_stated", 2, 2, "preference", stable=False) == ("active", True)
    assert PROMPT_VERSION
