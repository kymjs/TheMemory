"""WorkBuddy MCP merge installer (spec §14.5)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from thememory.paths import host_backups_dir


def workbuddy_home() -> Path:
    return Path.home() / ".workbuddy"


def mcp_path() -> Path:
    return workbuddy_home() / "mcp.json"


def install() -> dict[str, str]:
    path = mcp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    data: dict = {}
    if path.exists():
        dest_dir = host_backups_dir() / "workbuddy" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest_dir.mkdir(parents=True, exist_ok=True)
        backup = dest_dir / "mcp.json"
        shutil.copy2(path, backup)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    servers = data.setdefault("mcpServers", data.setdefault("mcp", {}))
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["thememory"] = {
        "command": sys.executable,
        "args": ["-m", "thememory.inbound.mcp_proxy"],
        "env": {"THEMEMORY_HOST": "workbuddy"},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"mcp": str(path), "backup": str(backup or "")}


def uninstall() -> None:
    path = mcp_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    for key in ("mcpServers", "mcp"):
        servers = data.get(key)
        if isinstance(servers, dict) and "thememory" in servers:
            del servers["thememory"]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
