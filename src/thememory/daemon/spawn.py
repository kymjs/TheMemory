"""Spawn thememoryd detached with no terminal / console attached."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def spawn_daemon(*, home: Path | None = None, env: dict[str, str] | None = None) -> subprocess.Popen[Any]:
    """Start thememoryd in the background without user-visible terminal noise.

    - Windows: CREATE_NO_WINDOW + hidden STARTUPINFO (no conhost flash).
    - POSIX: start_new_session (setsid) so the child is not tied to the caller's
      controlling terminal / SIGHUP — same idea as nohup.
    Stdin/stdout/stderr are always redirected to DEVNULL.
    """
    run_env = env if env is not None else os.environ.copy()
    if home is not None:
        run_env["THEMEMORY_HOME"] = str(home)

    kwargs: dict[str, Any] = {
        "env": run_env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW suppresses the console for python.exe children;
        # DETACHED_PROCESS alone still often flashes conhost briefly.
        # getattr keeps unit tests on POSIX able to monkeypatch platform=win32.
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        create_new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = create_new_group | create_no_window
        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_cls is not None:
            si = startupinfo_cls()
            si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = si
    else:
        kwargs["start_new_session"] = True

    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "thememory.daemon.main"],
        **kwargs,
    )
