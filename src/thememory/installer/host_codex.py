"""Codex CLI installer: MCP + hooks.json (spec §14.3)."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from thememory.installer.hook_template import install_hook_script
from thememory.paths import host_backups_dir


def codex_home() -> Path:
    return Path.home() / ".codex"


def hooks_path() -> Path:
    return codex_home() / "hooks.json"


def config_toml_path() -> Path:
    return codex_home() / "config.toml"


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest_dir = host_backups_dir() / "codex" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    shutil.copy2(path, dest)
    return dest


def install(*, allow_tools: bool = False) -> dict[str, str]:
    hook = install_hook_script()
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    hooks_backup = _backup(hooks_path())
    data: dict = {}
    if hooks_path().exists():
        try:
            data = json.loads(hooks_path().read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    hooks = data.setdefault("hooks", {})
    py = sys.executable
    cmd = f'"{py}" "{hook}" --host codex --event'
    managed = {
        "UserPromptSubmit": [{"command": f"{cmd} UserPromptSubmit"}],
        "Stop": [{"command": f"{cmd} Stop"}],
    }
    for event, entries in managed.items():
        existing = [e for e in (hooks.get(event) or []) if "thememory" not in str(e)]
        hooks[event] = existing + entries
    hooks_path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # MCP hint file (Codex uses `codex mcp add`; we also drop a snippet)
    snippet = home / "thememory-mcp-hint.txt"
    snippet.write_text(
        "Run:\n"
        f'  codex mcp add thememory -- {py} -m thememory.inbound.mcp_proxy\n'
        "Then trust hooks via /hooks in Codex.\n",
        encoding="utf-8",
    )
    return {
        "hooks": str(hooks_path()),
        "hook_script": str(hook),
        "hooks_backup": str(hooks_backup or ""),
        "mcp_hint": str(snippet),
        "note": "Codex requires manual /hooks trust",
    }


def uninstall() -> None:
    path = hooks_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    hooks = data.get("hooks") or {}
    for event, entries in list(hooks.items()):
        hooks[event] = [
            e
            for e in (entries or [])
            if "thememory" not in str(e.get("command") if isinstance(e, dict) else e)
        ]
        if not hooks[event]:
            del hooks[event]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    hint = codex_home() / "thememory-mcp-hint.txt"
    if hint.exists():
        hint.unlink()
