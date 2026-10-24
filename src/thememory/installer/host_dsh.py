"""Install TheMemory MCP into DeepSeek Harness cordis.patch.yml."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from thememory.paths import host_backups_dir

BEGIN = "# >>> thememory (managed) >>>"
END = "# <<< thememory <<<"
INSTALL_PROMPT = (
    "请执行 `thememory install deepseek-harness`，把 TheMemory 接成 MCP。"
    "不要覆盖已有的 cordis 补丁；安装器会备份并幂等合并。"
)


def dsh_home() -> Path:
    import os

    return Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))


def patch_path(profile: str | None = None) -> Path:
    if profile:
        return dsh_home() / "profiles" / profile / "cordis.patch.yml"
    return dsh_home() / "cordis.patch.yml"


def _managed_entries() -> str:
    py = sys.executable.replace("\\", "/")
    # The MCP client row is a *new* entry, not an override of an existing one:
    # a plain `- id:` row would be rejected as "entry not found" and the client
    # would never mount. `insert` is what actually adds it to the profile tree.
    return (
        f"{BEGIN}\n"
        f"- insert:\n"
        f"    - id: mcp-thememory-client\n"
        f"      name: '@deepseek-ai/dsh-mcp-client'\n"
        f"      config:\n"
        f"        serverName: thememory\n"
        f"        transport: stdio\n"
        f"        command: '{py}'\n"
        f"        args: ['-m', 'thememory.inbound.mcp_proxy']\n"
        f"        failOnStartupError: false\n"
        f"{END}\n"
    )


def _has_client_row(text: str) -> bool:
    """Whether the patch already wires an MCP client for thememory."""
    return "serverName: thememory" in text


def _client_row_in(path: Path) -> bool:
    """Whether a patch file wires an MCP client for thememory.

    The managed block is stripped first: the legacy managed row also names
    `serverName: thememory`, but it never mounts anything.
    """
    try:
        return _has_client_row(_strip_managed(path.read_text(encoding="utf-8")))
    except OSError:
        return False


def _strip_managed(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        if BEGIN in line:
            skipping = True
            continue
        if END in line:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "".join(out)


def install(profile: str | None = None) -> Path:
    path = patch_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        dest_dir = (
            host_backups_dir()
            / "deepseek-harness"
            / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest_dir / path.name)
        text = _strip_managed(path.read_text(encoding="utf-8"))
    else:
        text = ""
    # The client row must exist exactly once in the composed tree: the home
    # layer and every profile layer are stacked, and two rows with the same
    # id would collide. The home layer wins when it already has one.
    elsewhere = [patch_path()] if profile else []
    if _has_client_row(text) or any(_client_row_in(other) for other in elsewhere):
        path.write_text(text.rstrip() + "\n" if text.strip() else "[]\n", encoding="utf-8")
        _write_prompt(path)
        return path
    stripped = text.strip()
    if stripped in {"", "[]"}:
        new_text = _managed_entries()
    else:
        new_text = text.rstrip() + "\n" + _managed_entries()
    path.write_text(new_text, encoding="utf-8")
    _write_prompt(path)
    return path


def _write_prompt(path: Path) -> None:
    prompt = path.parent / "thememory-install-prompt.txt"
    prompt.write_text(INSTALL_PROMPT + "\n", encoding="utf-8")


def uninstall(profile: str | None = None) -> None:
    path = patch_path(profile)
    if not path.exists():
        return
    text = _strip_managed(path.read_text(encoding="utf-8")).strip()
    path.write_text((text + "\n") if text else "[]\n", encoding="utf-8")
