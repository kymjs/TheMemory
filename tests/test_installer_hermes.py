"""Hermes installer unit tests (no live hermes CLI required)."""

from __future__ import annotations

from pathlib import Path

from thememory.installer import host_hermes


def test_write_plugin_bakes_python(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(host_hermes, "hermes_home", lambda: tmp_path)
    dest = host_hermes._write_plugin(tmp_path)
    init = (dest / "__init__.py").read_text(encoding="utf-8")
    assert "THEMEMORY_PYTHON" in init
    assert "__THEMEMORY_PYTHON__" not in init
    assert (dest / "plugin.yaml").exists()


def test_patch_mcp_yaml_creates_block(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: built-in\n", encoding="utf-8")
    host_hermes._patch_mcp_yaml(tmp_path)
    text = cfg.read_text(encoding="utf-8")
    assert "mcp_servers:" in text
    assert "thememory:" in text
    assert "thememory.inbound.mcp_proxy" in text
    assert "THEMEMORY_HOST: hermes" in text
