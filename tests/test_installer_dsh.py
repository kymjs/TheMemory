from __future__ import annotations

from pathlib import Path

import pytest

from thememory.installer import host_dsh


def test_dsh_install_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path / "tm"))
    path = host_dsh.install()
    text = path.read_text(encoding="utf-8")
    assert "mcp-thememory" in text
    assert "thememory.inbound.mcp_proxy" in text
    host_dsh.install()
    text2 = path.read_text(encoding="utf-8")
    assert text2.count("mcp-thememory") == 1
    host_dsh.uninstall()
    assert "mcp-thememory" not in path.read_text(encoding="utf-8")
