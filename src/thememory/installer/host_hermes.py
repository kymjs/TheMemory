"""Install TheMemory into Hermes Agent (MCP + Memory Provider plugin)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from thememory.paths import host_backups_dir

PLUGIN_SRC = Path(__file__).with_name("hermes_plugin")


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidate = Path(local) / "hermes"
        if (candidate / "config.yaml").exists():
            return candidate.resolve()
    classic = Path.home() / ".hermes"
    if (classic / "config.yaml").exists():
        return classic.resolve()
    if local:
        return (Path(local) / "hermes").resolve()
    return classic.resolve()


def config_path(home: Path | None = None) -> Path:
    return (home or hermes_home()) / "config.yaml"


def plugin_dir(home: Path | None = None) -> Path:
    return (home or hermes_home()) / "plugins" / "thememory"


def backup_config(home: Path | None = None) -> Path | None:
    path = config_path(home)
    if not path.exists():
        return None
    dest_dir = host_backups_dir() / "hermes" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "config.yaml"
    shutil.copy2(path, dest)
    return dest


def _write_plugin(home: Path) -> Path:
    dest = plugin_dir(home)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py"):
        src = PLUGIN_SRC / name
        text = src.read_text(encoding="utf-8")
        if name == "__init__.py":
            text = text.replace("__THEMEMORY_PYTHON__", sys.executable.replace("\\", "\\\\"))
        (dest / name).write_text(text, encoding="utf-8")
    return dest


def _run_hermes(args: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    hermes = shutil.which("hermes")
    if not hermes:
        raise FileNotFoundError("hermes CLI not found on PATH")
    return subprocess.run(
        [hermes, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _ensure_mcp(home: Path) -> str:
    """Register stdio MCP pointing at this interpreter's thememory proxy.

    Prefer writing ``config.yaml`` directly: ``hermes mcp add`` can report success
    while Hermes is mid-update and leave no durable entry.
    """
    _run_hermes(["mcp", "remove", "thememory"], timeout=60.0)
    _patch_mcp_yaml(home)
    return "mcp_servers.thememory written to config.yaml"


def _mcp_entry_lines() -> list[str]:
    # YAML double-quoted path so Windows backslashes are escaped.
    cmd = json_dumps_str(sys.executable)
    return [
        "  thememory:",
        f"    command: {cmd}",
        '    args: ["-m", "thememory.inbound.mcp_proxy"]',
        "    env:",
        "      THEMEMORY_HOST: hermes",
    ]


def json_dumps_str(value: str) -> str:
    import json

    return json.dumps(value)


def _strip_thememory_mcp_block(text: str) -> str:
    """Remove a prior managed thememory entry under mcp_servers (best-effort)."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("  thememory:"):
            i += 1
            while i < len(lines) and (lines[i].startswith("    ") or lines[i].strip() == ""):
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _patch_mcp_yaml(home: Path) -> None:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _strip_thememory_mcp_block(text)
    entry = "\n".join(_mcp_entry_lines())
    lines = text.splitlines()
    # Prefer an active (uncommented) mcp_servers: key.
    for idx, line in enumerate(lines):
        if line.strip() == "mcp_servers:":
            lines.insert(idx + 1, entry)
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
            return
    # No active mcp_servers section — append one.
    path.write_text(text.rstrip() + "\n\nmcp_servers:\n" + entry + "\n", encoding="utf-8")


def _set_memory_provider() -> str:
    proc = _run_hermes(["config", "set", "memory.provider", "thememory"], timeout=60.0)
    if proc.returncode != 0:
        raise RuntimeError(
            f"hermes config set memory.provider failed: {(proc.stderr or proc.stdout or '')[:300]}"
        )
    return "memory.provider=thememory"


def install(*, home: Path | None = None) -> dict[str, str]:
    root = home or hermes_home()
    root.mkdir(parents=True, exist_ok=True)
    backup = backup_config(root)
    plugin = _write_plugin(root)
    mcp_msg = _ensure_mcp(root)
    mem_msg = _set_memory_provider()
    return {
        "hermes_home": str(root),
        "config": str(config_path(root)),
        "plugin": str(plugin),
        "backup": str(backup or ""),
        "mcp": mcp_msg,
        "memory": mem_msg,
    }


def uninstall(*, home: Path | None = None) -> None:
    root = home or hermes_home()
    _run_hermes(["mcp", "remove", "thememory"], timeout=60.0)
    _run_hermes(["config", "set", "memory.provider", "built-in"], timeout=60.0)
    dest = plugin_dir(root)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
