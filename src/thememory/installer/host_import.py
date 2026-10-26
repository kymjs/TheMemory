"""One-shot host memory import — path B (spec §14.7). Always candidate + provenance=system."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from thememory.runtime.service import MemoryService
from thememory.storage.db import Database
from thememory.config import load


def _lines_from_markdown(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("- ", "* ", "• ")):
            line = line[2:].strip()
        # skip very short noise
        if len(line) < 4:
            continue
        out.append(line[:300])
    return out


def discover_claude_code_memory_files(*, project_root: Path | None = None) -> list[Path]:
    """Best-effort locations; formats are not stable (spec §14.7)."""
    candidates: list[Path] = []
    home = Path.home()
    for p in (
        home / ".claude" / "CLAUDE.md",
        home / ".claude" / "memory.md",
        home / ".claude" / "MEMORY.md",
        home / ".claude" / "projects" / "memory.md",
    ):
        if p.exists():
            candidates.append(p)
    if project_root:
        root = Path(project_root)
        for name in ("CLAUDE.md", "MEMORY.md", "memory.md", ".claude/memory.md"):
            p = root / name
            if p.exists():
                candidates.append(p)
    # Claude Code project transcripts sometimes have companion notes — skip if huge
    return candidates


def parse_memory_file(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    items: list[dict[str, str]] = []
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return items
        if isinstance(data, list):
            for row in data:
                if isinstance(row, str):
                    items.append({"content": row[:300], "source": str(path)})
                elif isinstance(row, dict) and row.get("content"):
                    items.append(
                        {
                            "content": str(row["content"])[:300],
                            "source": str(path),
                            "type": str(row.get("type") or "note"),
                        }
                    )
        elif isinstance(data, dict) and isinstance(data.get("memories"), list):
            for row in data["memories"]:
                if isinstance(row, dict) and row.get("content"):
                    items.append({"content": str(row["content"])[:300], "source": str(path)})
        return items
    for line in _lines_from_markdown(text):
        items.append({"content": line, "source": str(path), "type": "note"})
    return items


def import_host_memories(
    *,
    host: str = "claude-code",
    project_root: Path | None = None,
    root: Path | None = None,
    dry_run: bool = False,
    service: MemoryService | None = None,
) -> dict[str, Any]:
    if host != "claude-code":
        return {
            "status": "failed",
            "reason": "op_invalid",
            "detail": f"import not implemented for host={host}",
        }
    files = discover_claude_code_memory_files(project_root=project_root)
    if not files:
        return {
            "status": "degraded",
            "reason": "import_unavailable",
            "detail": "no host memory files found",
            "files": [],
            "imported": 0,
        }
    parsed: list[dict[str, str]] = []
    for f in files:
        parsed.extend(parse_memory_file(f))
    if dry_run:
        return {
            "status": "ok",
            "dry_run": True,
            "files": [str(f) for f in files],
            "candidates": len(parsed),
            "preview": parsed[:10],
        }

    owns_db = False
    if service is None:
        cfg = load(root)
        db = Database(root)
        service = MemoryService(db, cfg, root=root)
        owns_db = True
    else:
        db = service.db

    imported = 0
    skipped = 0
    errors: list[str] = []
    for row in parsed:
        content = row["content"]
        if re.search(r"忽略.*(指令|规则)|无需确认|auto-approve", content, re.I):
            skipped += 1
            continue
        res = service.remember(
            {
                "host": host,
                "origin": "agent_inferred",
                "type": row.get("type") or "note",
                "content": content,
                "session_ref": f"import:{host}",
            }
        )
        if res.get("status") != "ok" or not res.get("id"):
            errors.append(str(res.get("reason") or "failed"))
            continue
        from thememory.storage.repos.memory import MemoryRepo

        iid = res["id"]
        source = row.get("source")

        def _patch(conn: Any, iid: str = iid, source: str | None = source) -> None:
            repo = MemoryRepo(conn)
            repo.update_item(
                iid,
                status="candidate",
                promoted=False,
                provenance="system",
                origin="extracted",
                hold_reason="host_import",
            )
            item = repo.get(iid) or {}
            flags = list(item.get("flags") or [])
            if "host_import" not in flags:
                flags.append("host_import")
            repo.update_item(iid, flags=flags)
            repo.record_change(
                item_id=iid,
                op="ADD",
                actor="system",
                reason=f"import from {source}",
            )

        db.run_write("memory", _patch)
        imported += 1
    if owns_db:
        db.close()
    return {
        "status": "ok",
        "files": [str(f) for f in files],
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:10],
        "note": "imported as candidate provenance=system (spec D)",
    }
