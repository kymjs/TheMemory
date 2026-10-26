"""Tests for silent daemon spawn helper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from thememory.daemon.spawn import spawn_daemon


def test_spawn_daemon_windows_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_daemon(home=tmp_path)

    assert captured["cmd"] == [sys.executable, "-m", "thememory.daemon.main"]
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["env"]["THEMEMORY_HOME"] == str(tmp_path)
    assert "start_new_session" not in kwargs
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    create_new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert kwargs["creationflags"] & create_no_window
    assert kwargs["creationflags"] & create_new_group
    assert not (kwargs["creationflags"] & getattr(subprocess, "DETACHED_PROCESS", 0x8))
    si = kwargs.get("startupinfo")
    if si is not None:
        assert si.dwFlags & getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        assert si.wShowWindow == getattr(subprocess, "SW_HIDE", 0)


def test_spawn_daemon_posix_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_popen(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    spawn_daemon()

    kwargs = captured["kwargs"]
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
    assert "startupinfo" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
