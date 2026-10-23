"""Cursor installer unit tests."""

from __future__ import annotations

import json
from pathlib import Path

from thememory.installer import host_cursor


def test_install_writes_mcp_and_hooks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(host_cursor, "cursor_home", lambda: tmp_path)
    monkeypatch.setattr(host_cursor, "host_backups_dir", lambda: tmp_path / "backups")
    result = host_cursor.install()
    mcp = json.loads(Path(result["mcp"]).read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["thememory"]["args"] == ["-m", "thememory.inbound.mcp_proxy"]
    assert mcp["mcpServers"]["thememory"]["env"]["THEMEMORY_HOST"] == "cursor"
    hooks = json.loads(Path(result["hooks"]).read_text(encoding="utf-8"))
    assert "sessionStart" in hooks["hooks"]
    assert any("thememory.py" in str(e.get("command")) for e in hooks["hooks"]["sessionStart"])
    assert Path(result["hook_script"]).exists()
    assert "additional_context" in Path(result["hook_script"]).read_text(encoding="utf-8")


def test_uninstall_removes_managed_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(host_cursor, "cursor_home", lambda: tmp_path)
    monkeypatch.setattr(host_cursor, "host_backups_dir", lambda: tmp_path / "backups")
    host_cursor.install()
    host_cursor.uninstall()
    mcp = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert "thememory" not in (mcp.get("mcpServers") or {})
    hooks = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
    for entries in (hooks.get("hooks") or {}).values():
        assert not any("thememory.py" in str(e.get("command")) for e in entries)
    assert not (tmp_path / "hooks" / "thememory.py").exists()
