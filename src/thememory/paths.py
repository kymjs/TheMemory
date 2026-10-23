"""Filesystem layout under ~/.thememory/."""

from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    override = os.environ.get("THEMEMORY_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".thememory").resolve()


def config_path(root: Path | None = None) -> Path:
    return (root or home_dir()) / "config.toml"


def slots_path(root: Path | None = None) -> Path:
    return (root or home_dir()) / "slots.toml"


def memory_db_path(root: Path | None = None) -> Path:
    return (root or home_dir()) / "memory.db"


def turns_db_path(root: Path | None = None) -> Path:
    return (root or home_dir()) / "turns.db"


def models_dir(root: Path | None = None) -> Path:
    return (root or home_dir()) / "models"


def bin_dir(root: Path | None = None) -> Path:
    return (root or home_dir()) / "bin"


def run_dir(root: Path | None = None) -> Path:
    return (root or home_dir()) / "run"


def logs_dir(root: Path | None = None) -> Path:
    return (root or home_dir()) / "logs"


def host_backups_dir(root: Path | None = None) -> Path:
    return (root or home_dir()) / "host_backups"


def sock_path(root: Path | None = None) -> Path:
    return run_dir(root) / "thememory.sock"


def endpoint_path(root: Path | None = None) -> Path:
    return run_dir(root) / "endpoint.json"


def lock_path(root: Path | None = None) -> Path:
    return run_dir(root) / "thememoryd.lock"


def pid_path(root: Path | None = None) -> Path:
    return run_dir(root) / "thememoryd.pid"


def ensure_layout(root: Path | None = None) -> Path:
    """Create data directories with restrictive permissions where supported."""
    base = root or home_dir()
    for path in (
        base,
        models_dir(base),
        bin_dir(base),
        run_dir(base),
        logs_dir(base),
        host_backups_dir(base),
        base / "prompts",
    ):
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return base
