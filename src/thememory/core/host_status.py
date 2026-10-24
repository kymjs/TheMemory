"""Which hosts have a TheMemory install, for doctor (spec §18.1)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _contains(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    try:
        return needle in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def inspect_hosts(
    *,
    user_home: Path | None = None,
    data_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Report install state. Codex hook trust cannot be read; say so."""
    home = user_home or Path.home()
    rows: list[dict[str, Any]] = []

    claude = _contains(home / ".claude" / "settings.json", "thememory")
    rows.append({"host": "claude-code", "installed": claude, "hook_trust": "n/a"})

    codex = _contains(home / ".codex" / "hooks.json", "thememory")
    rows.append(
        {
            "host": "codex",
            "installed": codex,
            "hook_trust": "unknown" if codex else "not_installed",
            "hook_trust_reason": "codex_trust_not_detectable" if codex else None,
        }
    )

    cursor = _contains(home / ".cursor" / "mcp.json", "thememory") or _contains(
        home / ".cursor" / "hooks.json", "thememory"
    )
    rows.append({"host": "cursor", "installed": cursor, "hook_trust": "n/a"})

    if user_home is None:
        dsh_home = Path(os.environ.get("DSH_HOME", str(home / ".dsh")))
    else:
        dsh_home = home / ".dsh"
    dsh = _contains(dsh_home / "cordis.patch.yml", "thememory")
    rows.append({"host": "deepseek-harness", "installed": dsh, "hook_trust": "n/a"})

    hermes_candidates = [home / ".hermes"]
    if user_home is None:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            hermes_candidates.append(Path(local) / "hermes")
    hermes = any(
        _contains(base / "config.yaml", "thememory") or (base / "plugins" / "thememory").is_dir()
        for base in hermes_candidates
    )
    rows.append({"host": "hermes", "installed": hermes, "hook_trust": "n/a"})

    workbuddy = _contains(home / ".workbuddy" / "mcp.json", "thememory")
    rows.append({"host": "workbuddy", "installed": workbuddy, "hook_trust": "n/a"})

    doubao = bool(data_root and (Path(data_root) / "docs" / "doubao-connector.md").is_file())
    rows.append(
        {
            "host": "doubao",
            "installed": doubao,
            "hook_trust": "n/a",
            "note": "manual connector" if doubao else None,
        }
    )
    return rows
