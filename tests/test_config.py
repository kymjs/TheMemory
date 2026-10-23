from __future__ import annotations

from pathlib import Path

import pytest

from thememory.config.loader import ConfigError, load, set_path, validate, write_default


def test_write_and_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    write_default(tmp_path)
    cfg = load(tmp_path)
    assert cfg["daemon"]["log_level"] == "info"
    set_path(cfg, "extraction.trigger_turns", 5)
    validate(cfg)
    assert cfg["extraction"]["trigger_turns"] == 5


def test_unknown_key_rejected(tmp_path: Path) -> None:
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["daemon"]["nope"] = 1
    with pytest.raises(ConfigError):
        validate(cfg)
