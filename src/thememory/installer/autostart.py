"""Register / unregister OS login autostart for thememoryd (spec §16.3)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from thememory.paths import bin_dir, ensure_layout, home_dir, logs_dir


def runtime_json_path(root: Path | None = None) -> Path:
    return bin_dir(root) / "runtime.json"


def runtime_status(root: Path | None = None) -> dict[str, Any]:
    """Whether the stable entry can still find an interpreter (spec §18.1)."""
    path = runtime_json_path(root)
    if not path.exists():
        return {"present": False, "valid": False, "reason": "missing"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": True, "valid": False, "reason": "invalid_json"}
    py = str(data.get("python") or "")
    if not py or not Path(py).exists():
        return {"present": True, "valid": False, "reason": "interpreter_missing"}
    return {"present": True, "valid": True, "reason": "ok"}


def stable_entry_path(root: Path | None = None) -> Path:
    if sys.platform == "win32":
        return bin_dir(root) / "thememoryd.cmd"
    return bin_dir(root) / "thememoryd"


def write_stable_entry(root: Path | None = None) -> dict[str, str]:
    """Write ~/.thememory/bin/thememoryd (+ runtime.json) pointing at current interpreter."""
    ensure_layout(root)
    base = root or home_dir()
    runtime = {
        "python": sys.executable,
        "module": "thememory.daemon.main",
        "home": str(base),
    }
    rt = runtime_json_path(base)
    rt.write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    entry = stable_entry_path(base)
    if sys.platform == "win32":
        entry.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" -m thememory.daemon.main --home "{base}"\r\n',
            encoding="utf-8",
        )
    else:
        entry.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" -m thememory.daemon.main --home "{base}"\n',
            encoding="utf-8",
        )
        try:
            os.chmod(entry, 0o755)
        except OSError:
            pass
    return {"entry": str(entry), "runtime": str(rt)}


def repair_install(root: Path | None = None) -> dict[str, Any]:
    """Rewrite runtime.json. Re-register login autostart only if it already exists."""
    entry = write_stable_entry(root)
    reregistered = False
    if autostart_registered():
        install_autostart(root)
        reregistered = True
    return {
        "entry": entry.get("entry", ""),
        "autostart_reregistered": reregistered,
        "runtime": runtime_status(root),
    }


def install_autostart(root: Path | None = None) -> dict[str, str]:
    paths = write_stable_entry(root)
    entry = Path(paths["entry"])
    base = root or home_dir()
    if sys.platform == "win32":
        return {**paths, **_install_windows(entry)}
    if sys.platform == "darwin":
        return {**paths, **_install_macos(entry, base)}
    return {**paths, **_install_linux(entry, base)}


def autostart_registered() -> bool:
    """Whether the login entry exists. Read-only; does not register anything."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                winreg.QueryValueEx(key, "TheMemory")
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / "com.thememory.daemon.plist").is_file()
    unit = Path.home() / ".config" / "systemd" / "user" / "thememory.service"
    desktop = Path.home() / ".config" / "autostart" / "thememory.desktop"
    return unit.is_file() or desktop.is_file()


def uninstall_autostart(root: Path | None = None) -> None:
    if sys.platform == "win32":
        _uninstall_windows()
    elif sys.platform == "darwin":
        _uninstall_macos()
    else:
        _uninstall_linux()


def _install_windows(entry: Path) -> dict[str, str]:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "TheMemory", 0, winreg.REG_SZ, f'"{entry}"')
    return {"mechanism": "HKCU\\...\\Run", "name": "TheMemory"}


def _uninstall_windows() -> None:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "TheMemory")
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _install_macos(entry: Path, base: Path) -> dict[str, str]:
    label = "com.thememory.daemon"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist = plist_dir / f"{label}.plist"
    log = logs_dir(base) / "thememoryd.launchd.log"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{entry}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    plist.write_text(body, encoding="utf-8")
    return {"mechanism": "launchd", "plist": str(plist)}


def _uninstall_macos() -> None:
    plist = Path.home() / "Library" / "LaunchAgents" / "com.thememory.daemon.plist"
    if plist.exists():
        plist.unlink()


def _install_linux(entry: Path, base: Path) -> dict[str, str]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "thememory.service"
    log = logs_dir(base) / "thememoryd.systemd.log"
    unit.write_text(
        f"""[Unit]
Description=TheMemory daemon
After=default.target

[Service]
ExecStart={entry}
Restart=on-failure
StandardOutput=append:{log}
StandardError=append:{log}

[Install]
WantedBy=default.target
""",
        encoding="utf-8",
    )
    # XDG fallback desktop entry
    auto = Path.home() / ".config" / "autostart"
    auto.mkdir(parents=True, exist_ok=True)
    desktop = auto / "thememory.desktop"
    desktop.write_text(
        f"""[Desktop Entry]
Type=Application
Name=TheMemory
Exec={entry}
X-GNOME-Autostart-enabled=true
""",
        encoding="utf-8",
    )
    return {"mechanism": "systemd-user+xdg", "unit": str(unit), "desktop": str(desktop)}


def _uninstall_linux() -> None:
    for path in (
        Path.home() / ".config" / "systemd" / "user" / "thememory.service",
        Path.home() / ".config" / "autostart" / "thememory.desktop",
    ):
        if path.exists():
            path.unlink()
