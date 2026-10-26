from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.runtime.service import MemoryService
from thememory.installer.host_import import import_host_memories
from thememory.paths import ensure_layout
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo


def test_import_claude_markdown_as_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    write_default(tmp_path)
    mem = tmp_path / "CLAUDE.md"
    mem.write_text(
        "# Memory\n\n- 用户偏好使用深色主题\n- 该项目使用 pytest\n",
        encoding="utf-8",
    )
    # Point discovery at our temp file by using project_root
    cfg = load(tmp_path)
    cfg["limits"] = {
        "session_max_writes": 100,
        "session_max_procedural": 50,
        "burst_window_seconds": 1,
        "burst_max_writes": 100,
    }
    svc = MemoryService(Database(tmp_path), cfg, root=tmp_path)
    result = import_host_memories(
        host="claude-code",
        project_root=tmp_path,
        root=tmp_path,
        service=svc,
    )
    assert result["status"] == "ok"
    assert result["imported"] >= 2
    items = MemoryRepo(svc.db.read_memory()).list_items(status="candidate", limit=20)
    assert any(i.get("provenance") == "system" for i in items)
    assert any("host_import" in (i.get("flags") or []) for i in items)
