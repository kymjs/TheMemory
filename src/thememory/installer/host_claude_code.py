"""Install TheMemory into Claude Code via settings.json hooks + claude mcp add."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from thememory.installer.hook_template import install_hook_script
from thememory.paths import host_backups_dir

MARKER_BEGIN = "# >>> thememory (managed) >>>"
MARKER_END = "# <<< thememory <<<"


def claude_home() -> Path:
    return Path.home() / ".claude"


def settings_path() -> Path:
    return claude_home() / "settings.json"


def backup_settings() -> Path | None:
    path = settings_path()
    if not path.exists():
        return None
    dest_dir = host_backups_dir() / "claude-code" / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "settings.json"
    shutil.copy2(path, dest)
    return dest


def _hook_command(hook_py: Path, event: str) -> str:
    # Use absolute interpreter + hook path
    return f'"{sys.executable}" "{hook_py}" --host claude-code --event {event}'


def install(*, allow_tools: bool = False) -> dict[str, str]:
    hook_py = install_hook_script()
    backup = backup_settings()
    path = settings_path()
    data: dict = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.setdefault("hooks", {})
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd"):
        entry = {
            "hooks": [
                {
                    "type": "command",
                    "command": _hook_command(hook_py, event),
                    "timeout": 10,
                }
            ]
        }
        # replace existing thememory-managed entries
        existing = hooks.get(event) or []
        filtered = []
        for block in existing:
            cmds = block.get("hooks") or []
            if any("thememory" in str(c.get("command", "")) or "hook.py" in str(c.get("command", "")) for c in cmds):
                continue
            filtered.append(block)
        filtered.append(entry)
        hooks[event] = filtered
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # MCP: prefer writing ~/.claude.json directly (Windows-safe), then try CLI.
    claude_json = Path.home() / ".claude.json"
    blob: dict = {}
    if claude_json.exists():
        blob = json.loads(claude_json.read_text(encoding="utf-8"))
    servers = blob.setdefault("mcpServers", {})
    servers["thememory"] = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "thememory.inbound.mcp_proxy"],
        "env": {},
    }
    claude_json.write_text(json.dumps(blob, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        # shell=True + cmd avoids PowerShell eating `-m`
        subprocess.run(
            f'claude mcp add thememory -s user -- "{sys.executable}" -m thememory.inbound.mcp_proxy',
            check=False,
            capture_output=True,
            text=True,
            shell=True,
        )
    except FileNotFoundError:
        pass
    return {"settings": str(path), "hook": str(hook_py), "backup": str(backup or "")}


def uninstall() -> None:
    path = settings_path()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        hooks = data.get("hooks") or {}
        for event, blocks in list(hooks.items()):
            kept = []
            for block in blocks:
                cmds = block.get("hooks") or []
                if any("hook.py" in str(c.get("command", "")) for c in cmds):
                    continue
                kept.append(block)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        subprocess.run(["claude", "mcp", "remove", "thememory"], check=False, capture_output=True)
    except FileNotFoundError:
        pass
