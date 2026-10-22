"""Smoke tests for package installability."""

from __future__ import annotations

from thememory import PROTOCOL_VERSION, __version__


def test_version() -> None:
    assert __version__ == "0.1.0"
    assert PROTOCOL_VERSION == 1
