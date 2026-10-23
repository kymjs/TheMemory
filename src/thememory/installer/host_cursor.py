"""Install TheMemory into Cursor (global MCP + user hooks)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from thememory.installer.cursor_hook import CURSOR_HOOK_SOURCE
from thememory.paths import host_backups_dir


def cursor_home() -> Path:
    return Path.home() / ".cursor"


def mcp_path() -> Path:
    return cursor_home() / "mcp.json"


def hooks_json_path() -> Path:
    return cursor_home() / "hooks.json"


def hooks_script_path() -> Path:
    return cursor_home() / "hooks" / "thememory.py"


def _backup(path: Path, label: str) -> Path | None:
    if not path.exists():
        return None
    dest_dir = host_backups_dir() / "cursor" / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / label
    shutil.copy2(path, dest)
    return dest


def _hook_command() -> str:
    # User hooks run from ~/.cursor/, so relative ./hooks/... works.
    py = sys.executable
    return f'"{py}" "./hooks/thememory.py" --event'


def _write_hook_script() -> Path:
    path = hooks_script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CURSOR_HOOK_SOURCE, encoding="utf-8")
    try:
        import os

        os.chmod(path, 0o755)
    except OSError:
        pass
    return path


def _merge_hooks(data: dict) -> dict:
    """Install/replace thememory-managed hook entries; keep unrelated hooks."""
    out = dict(data) if data else {}
    out["version"] = int(out.get("version") or 1)
    hooks = dict(out.get("hooks") or {})
    base = _hook_command()
    managed = {
        "sessionStart": [{"command": f"{base} sessionStart", "timeout": 15}],
        "beforeSubmitPrompt": [{"command": f"{base} beforeSubmitPrompt", "timeout": 10}],
        "afterAgentResponse": [{"command": f"{base} afterAgentResponse", "timeout": 10}],
        "preCompact": [{"command": f"{base} preCompact", "timeout": 10}],
        "sessionEnd": [{"command": f"{base} sessionEnd", "timeout": 10}],
    }

    def _is_thememory(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        cmd = str(entry.get("command") or "")
        return "thememory.py" in cmd

    for event, entries in managed.items():
        existing = hooks.get(event) or []
        kept = [e for e in existing if not _is_thememory(e)]
        hooks[event] = kept + entries
    out["hooks"] = hooks
    return out


def _write_mcp() -> Path:
    path = mcp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    servers["thememory"] = {
        "command": sys.executable,
        "args": ["-m", "thememory.inbound.mcp_proxy"],
        "env": {"THEMEMORY_HOST": "cursor"},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_hooks() -> Path:
    path = hooks_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    merged = _merge_hooks(data)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def install() -> dict[str, str]:
    mcp_backup = _backup(mcp_path(), "mcp.json")
    hooks_backup = _backup(hooks_json_path(), "hooks.json")
    script = _write_hook_script()
    mcp = _write_mcp()
    hooks = _write_hooks()
    return {
        "mcp": str(mcp),
        "hooks": str(hooks),
        "hook_script": str(script),
        "mcp_backup": str(mcp_backup or ""),
        "hooks_backup": str(hooks_backup or ""),
    }


def uninstall() -> None:
    path = mcp_path()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or {}
        if isinstance(servers, dict) and "thememory" in servers:
            servers.pop("thememory", None)
            data["mcpServers"] = servers
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    hj = hooks_json_path()
    if hj.exists():
        data = json.loads(hj.read_text(encoding="utf-8"))
        hooks = data.get("hooks") or {}
        if isinstance(hooks, dict):
            for event, entries in list(hooks.items()):
                kept = [
                    e
                    for e in (entries or [])
                    if not (
                        isinstance(e, dict)
                        and "thememory.py" in str(e.get("command") or "")
                    )
                ]
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event, None)
            data["hooks"] = hooks
            hj.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    script = hooks_script_path()
    if script.exists():
        script.unlink()
