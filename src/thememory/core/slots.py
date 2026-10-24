"""Slot registry: built-in keys + optional ~/.thememory/slots.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from thememory.paths import slots_path

# Spec §8.1 — initial built-in registry
BUILTIN_SLOTS: list[dict[str, Any]] = [
    {"key": "identity.name", "name": "称呼", "multi": False, "default_scope": "user", "stable": True, "types": ["profile_fact"]},
    {"key": "identity.language", "name": "使用语言", "multi": False, "default_scope": "user", "stable": True, "types": ["profile_fact"]},
    {"key": "identity.timezone", "name": "时区", "multi": False, "default_scope": "user", "stable": True, "types": ["profile_fact"]},
    {"key": "identity.occupation", "name": "职业", "multi": False, "default_scope": "user", "stable": True, "types": ["profile_fact"]},
    {"key": "identity.residence", "name": "常住地", "multi": False, "default_scope": "user", "stable": True, "types": ["profile_fact"]},
    {"key": "identity.org", "name": "组织", "multi": False, "default_scope": "user", "stable": True, "types": ["profile_fact"]},
    {"key": "pref.answer_style", "name": "回答风格", "multi": False, "default_scope": "user", "stable": False, "types": ["preference"]},
    {"key": "pref.output_format", "name": "输出格式", "multi": False, "default_scope": "user", "stable": False, "types": ["preference"]},
    {"key": "pref.tech_stack", "name": "技术栈偏好", "multi": True, "default_scope": "user", "stable": False, "types": ["preference"]},
    {"key": "pref.tools", "name": "常用工具", "multi": True, "default_scope": "user", "stable": False, "types": ["preference"]},
    {"key": "pref.code_style", "name": "代码风格", "multi": True, "default_scope": "user", "stable": False, "types": ["preference"]},
    {"key": "constraint.avoid", "name": "禁忌", "multi": True, "default_scope": "user", "stable": True, "types": ["preference"]},
    {"key": "goal.current", "name": "当前目标", "multi": True, "default_scope": "user", "stable": False, "types": ["note"]},
    {"key": "plan.trip.<dest>", "name": "出行计划", "multi": True, "default_scope": "user", "stable": True, "types": ["plan"], "template": True},
    {"key": "plan.schedule.<id>", "name": "日程", "multi": True, "default_scope": "user", "stable": True, "types": ["plan"], "template": True},
    {"key": "project.convention", "name": "项目约定", "multi": True, "default_scope": "project", "stable": False, "types": ["procedural"]},
    {"key": "project.stack", "name": "项目技术栈", "multi": True, "default_scope": "project", "stable": False, "types": ["note"]},
    {"key": "project.goal", "name": "项目目标", "multi": True, "default_scope": "project", "stable": False, "types": ["note"]},
]


@dataclass(frozen=True)
class Slot:
    key: str
    name: str
    multi: bool
    default_scope: str
    stable: bool
    types: tuple[str, ...]
    template: bool = False


def _parse_slot(raw: dict[str, Any]) -> Slot:
    key = str(raw["key"]).strip().lower()
    if not key or " " in key:
        raise ValueError(f"invalid slot key: {key!r}")
    return Slot(
        key=key,
        name=str(raw.get("name") or key),
        multi=bool(raw.get("multi", False)),
        default_scope=str(raw.get("default_scope") or "user"),
        stable=bool(raw.get("stable", False)),
        types=tuple(raw.get("types") or ["note"]),
        template=bool(raw.get("template")) or ".<" in key,
    )


def load_slots(root: Path | None = None) -> dict[str, Slot]:
    slots = {_parse_slot(s).key: _parse_slot(s) for s in BUILTIN_SLOTS}
    path = slots_path(root)
    if not path.exists():
        return slots
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    for raw in data.get("slot") or []:
        if not isinstance(raw, dict):
            raise ValueError("slots.toml [[slot]] must be tables")
        slot = _parse_slot(raw)
        slots[slot.key] = slot
    return slots


def slot_keys_for_prompt(slots: dict[str, Slot]) -> str:
    lines = []
    for key, slot in sorted(slots.items()):
        lines.append(
            f"- {key} ({slot.name}; multi={slot.multi}; stable={slot.stable}; types={','.join(slot.types)})"
        )
    return "\n".join(lines)


def slot_is_multi(key: str, slots: dict[str, Slot]) -> bool:
    """Single-value slots conflict; multi-value slots coexist (spec §7.8 / §8.1)."""
    slot = slots.get(key)
    if slot is not None:
        return slot.multi
    for candidate in slots.values():
        if not candidate.template:
            continue
        prefix = candidate.key.split(".<", 1)[0] + "."
        if key.startswith(prefix) and len(key) > len(prefix):
            return candidate.multi
    return True


def normalize_slot_key(key: str | None, slots: dict[str, Slot]) -> str | None:
    """Return key if registered (or matches a template prefix); else None."""
    if not key:
        return None
    k = key.strip().lower()
    if k in slots:
        return k
    for slot in slots.values():
        if not slot.template:
            continue
        # plan.trip.<dest> → prefix plan.trip.
        prefix = slot.key.split(".<", 1)[0] + "."
        if k.startswith(prefix) and len(k) > len(prefix):
            return k
    return None
