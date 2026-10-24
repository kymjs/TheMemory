from __future__ import annotations

from pathlib import Path

import pytest

from thememory.installer.autostart import write_stable_entry
from thememory.paths import ensure_layout


def test_write_stable_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    ensure_layout(tmp_path)
    paths = write_stable_entry(tmp_path)
    assert Path(paths["entry"]).exists()
    assert Path(paths["runtime"]).exists()
