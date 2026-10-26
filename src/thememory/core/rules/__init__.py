"""Deterministic domain rules (promotion, conflict, provenance, screening)."""

from thememory.core.extraction import (
    _apply_r1_r2,
    promote_decision,
)
from thememory.core.redaction import screen_directive
from thememory.core.retrieval import trust_tier
from thememory.core.slots import load_slots, normalize_slot_key, slot_is_multi

__all__ = [
    "promote_decision",
    "screen_directive",
    "trust_tier",
    "load_slots",
    "normalize_slot_key",
    "slot_is_multi",
    "apply_r1_r2",
]


def apply_r1_r2(op: dict, turns: dict) -> str:
    return _apply_r1_r2(op, turns)
