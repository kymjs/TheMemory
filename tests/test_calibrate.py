from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config import load, write_default
from thememory.eval.calibrate import calibrate_tau
from thememory.installer.host_doubao import install as install_doubao
from thememory.paths import ensure_layout


def test_calibrate_picks_a_tau(tmp_path: Path) -> None:
    result = calibrate_tau(root=tmp_path, taus=(0.2, 0.9), write=False, k=3)
    assert result["status"] == "ok"
    assert result["best_tau"] in {0.2, 0.9}
    assert len(result["trials"]) == 2


def test_calibrate_writes_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    write_default(tmp_path)
    result = calibrate_tau(root=tmp_path, taus=(0.4,), write=True, k=2)
    assert result["written"] is True
    cfg = load(tmp_path)
    assert cfg["retrieval"]["tau_search_cosine"] == 0.4


def test_doubao_instructions_file(tmp_path: Path) -> None:
    info = install_doubao(tmp_path)
    text = Path(info["instructions"]).read_text(encoding="utf-8")
    assert "STDIO" in text
    assert "thememory.inbound.mcp_proxy" in text
