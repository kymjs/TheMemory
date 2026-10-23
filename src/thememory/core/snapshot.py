"""Render frozen profile snapshots within budgets."""

from __future__ import annotations

import hashlib
from typing import Any


def render_snapshot(
    items: list[dict[str, Any]],
    *,
    summary: str | None = None,
    unextracted: list[dict[str, Any]] | None = None,
    budgets: dict[str, int] | None = None,
    annotated: bool = True,
) -> tuple[str, str]:
    budgets = budgets or {}
    sections: list[str] = ["# TheMemory 用户画像"]
    profile = [i for i in items if i.get("type") in {"profile_fact", "preference", "pattern", "note"}]
    schedule = [i for i in items if i.get("type") in {"event", "plan"}]
    procedural = [i for i in items if i.get("type") == "procedural"]
    project = [i for i in items if str(i.get("scope", "")).startswith("project:")]

    def _block(title: str, rows: list[dict[str, Any]], budget: int) -> str | None:
        if not rows or budget <= 0:
            return None
        lines = [f"## {title}"]
        used = 0
        for item in rows:
            line = _format_item(item, annotated=annotated)
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else None

    for title, rows, key in (
        ("画像与偏好", profile, "budget_profile"),
        ("近期安排", schedule, "budget_schedule"),
        ("项目相关", project, "budget_project"),
        ("程序性记忆", procedural, "budget_procedural"),
    ):
        block = _block(title, rows, int(budgets.get(key, 1000)))
        if block:
            sections.append(block)

    if summary and budgets.get("budget_summary", 0) > 0:
        sections.append("## 近期会话摘要\n" + summary[: int(budgets["budget_summary"])])

    if unextracted:
        budget = int(budgets.get("budget_unextracted", 500))
        lines = ["## 尚未整理的近期对话"]
        used = 0
        for turn in unextracted:
            line = f"- ({turn.get('role')}) {str(turn.get('content') or '')[:200]}"
            if used + len(line) > budget:
                break
            lines.append(line)
            used += len(line)
        if len(lines) > 1:
            sections.append("\n".join(lines))

    text = "\n\n".join(sections).strip() + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return text, digest


def _format_item(item: dict[str, Any], *, annotated: bool) -> str:
    content = str(item.get("content") or "").replace("\n", " ")
    if not annotated:
        return f"- {content}"
    obs = (item.get("observed_at") or "")[:10]
    trust = _trust_label(item)
    return f"- [{obs}|{trust}] {content}"


def _trust_label(item: dict[str, Any]) -> str:
    origin = item.get("origin")
    if origin == "user_requested":
        return "high"
    if origin == "user_stated":
        return "high"
    if origin == "extracted":
        return "medium"
    return "low"
