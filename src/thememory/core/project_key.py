"""Project identity: hash of the git remote, else the absolute path (spec §6.4)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def resolve_project_key(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text)
    if path.exists():
        remote = _git_remote(path)
        basis = remote or path.resolve().as_posix().lower()
        return _digest(basis)
    return _digest(text.lower())


def _digest(basis: str) -> str:
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _git_remote(path: Path) -> str | None:
    root = path if path.is_dir() else path.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    if not url:
        return None
    return _normalize_remote(url)


def _normalize_remote(url: str) -> str:
    text = url.strip().lower().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@"):
        text = text[4:].replace(":", "/", 1)
    for prefix in ("https://", "http://", "ssh://", "git://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.rstrip("/")
