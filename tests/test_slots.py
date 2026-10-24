from __future__ import annotations

from pathlib import Path

from thememory.core.slots import load_slots, normalize_slot_key


def test_builtin_and_template_slots(tmp_path: Path) -> None:
    slots = load_slots(tmp_path)
    assert "identity.name" in slots
    assert normalize_slot_key("identity.name", slots) == "identity.name"
    assert normalize_slot_key("plan.trip.天津", slots) == "plan.trip.天津"
    assert normalize_slot_key("unknown.slot", slots) is None


def test_user_slots_toml(tmp_path: Path) -> None:
    (tmp_path / "slots.toml").write_text(
        """
[[slot]]
key = "pref.music"
name = "音乐"
multi = true
default_scope = "user"
stable = false
types = ["preference"]
""",
        encoding="utf-8",
    )
    slots = load_slots(tmp_path)
    assert "pref.music" in slots
    assert slots["pref.music"].multi is True
