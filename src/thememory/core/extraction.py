"""LLM extraction prompt + op application (R1–R4 + promotion)."""

from __future__ import annotations

import json
import logging
from typing import Any

from thememory.core.redaction import screen_directive
from thememory.util import content_hash, normalize_text, prepare_fts_text

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是 TheMemory 的记忆提取器。根据对话轮次，输出 JSON：
{
  "session_summary": "不超过300字的会话摘要",
  "ops": [ ... ]
}
ops 中每项 op 为 ADD / UPDATE / RETRACT / REINFORCE。
ADD 字段：type, slot_key(可空), scope(user|project), content, keywords[], aliases[],
valid_from, valid_to, origin(user_stated|agent_inferred|extracted), quote, evidence_turns[]。
UPDATE/RETRACT/REINFORCE 需 target_id 与 evidence_turns。
只提取稳定、可复用的用户事实/偏好/计划；不要记临时指令、密钥、他人隐私。
必须输出合法 JSON 对象。"""


def build_user_prompt(turns: list[dict[str, Any]], existing: list[dict[str, Any]]) -> str:
    lines = ["## 已有活跃记忆（可能被 UPDATE/REINFORCE）"]
    for item in existing[:40]:
        lines.append(f"- id={item['id']} type={item['type']} slot={item.get('slot_key')} :: {item['content']}")
    lines.append("\n## 本批对话")
    for t in turns:
        lines.append(f"[{t['id']}] ({t['role']}) {t.get('content') or ''}")
    return "\n".join(lines)


def apply_ops(
    ops: list[dict[str, Any]],
    *,
    turns: list[dict[str, Any]],
    remember_fn: Any,
    update_fn: Any,
    retract_fn: Any,
    reinforce_fn: Any,
    tombstone_hashes: set[str],
    screen_extra: list[str] | None = None,
) -> list[dict[str, Any]]:
    turn_by_id = {t["id"]: t for t in turns}
    results: list[dict[str, Any]] = []
    for op in ops:
        try:
            kind = op.get("op")
            evidence = op.get("evidence_turns") or []
            if not evidence or any(eid not in turn_by_id for eid in evidence):
                results.append({"op": kind, "ok": False, "reason": "evidence_missing"})
                continue
            origin = _apply_r1_r2(op, turn_by_id)
            op = dict(op)
            op["origin"] = origin
            content = str(op.get("content") or "")
            if content and content_hash(content) in tombstone_hashes:
                results.append({"op": kind, "ok": False, "reason": "tombstone_suppressed"})
                continue
            if content and screen_directive(content, screen_extra):
                op["flags"] = list(op.get("flags") or []) + ["directive_pattern"]
                op["status"] = "candidate"
            if kind == "ADD":
                results.append({"op": kind, "ok": True, "result": remember_fn(op, turn_by_id)})
            elif kind == "UPDATE":
                results.append({"op": kind, "ok": True, "result": update_fn(op, turn_by_id)})
            elif kind == "RETRACT":
                results.append({"op": kind, "ok": True, "result": retract_fn(op)})
            elif kind == "REINFORCE":
                results.append({"op": kind, "ok": True, "result": reinforce_fn(op, turn_by_id)})
            else:
                results.append({"op": kind, "ok": False, "reason": "op_invalid"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("apply op failed")
            results.append({"op": op.get("op"), "ok": False, "reason": str(exc)})
    return results


def _apply_r1_r2(op: dict[str, Any], turn_by_id: dict[str, dict[str, Any]]) -> str:
    evidence = op.get("evidence_turns") or []
    roles = {turn_by_id[eid]["role"] for eid in evidence if eid in turn_by_id}
    declared = op.get("origin") or "extracted"
    if "user" not in roles:
        # R1: cannot be user_stated
        if declared == "user_stated":
            return "agent_inferred"
        return declared if declared in {"agent_inferred", "extracted"} else "agent_inferred"
    if declared == "user_stated":
        quote = normalize_text(str(op.get("quote") or ""))
        if not quote:
            return "extracted"
        # R2: quote must appear in a user turn
        for eid in evidence:
            turn = turn_by_id.get(eid)
            if not turn or turn["role"] != "user":
                continue
            if quote and quote in normalize_text(str(turn.get("content") or "")):
                return "user_stated"
        return "extracted"
    return declared if declared in {"user_stated", "agent_inferred", "extracted"} else "extracted"


def promote_decision(origin: str, evidence_count: int, promote_repeat: int, type_: str) -> tuple[str, bool]:
    """Return (status, promoted)."""
    if origin == "user_requested":
        return "active", True
    if origin == "user_stated":
        if type_ in {"event", "plan"}:
            return "active", True
        if evidence_count >= 1:
            # stable slots promote at 1; others need promote_repeat — simplified: 1 for stated
            return "active", True
        return "active", False
    if origin in {"extracted", "agent_inferred"}:
        if evidence_count >= promote_repeat:
            return "active", True
        return "candidate", False
    return "candidate", False


def item_from_add_op(
    op: dict[str, Any],
    *,
    host: str,
    observer: str,
    session_id: str | None,
    now: str,
) -> dict[str, Any]:
    content = str(op["content"]).strip()
    type_ = op.get("type") or "note"
    origin = op.get("origin") or "extracted"
    status = op.get("status") or "candidate"
    promoted = False
    if origin == "user_stated" and "directive_pattern" not in (op.get("flags") or []):
        status, promoted = "active", True
        if type_ == "procedural":
            status, promoted = "candidate", False
            op = dict(op)
            op["hold_reason"] = "procedural_unverified"
    return {
        "observer": observer,
        "scope": op.get("scope") or ("project:unknown" if type_ == "procedural" else "user"),
        "type": type_,
        "slot_key": op.get("slot_key"),
        "content": content,
        "source_quote": op.get("quote"),
        "keywords": op.get("keywords") or [],
        "aliases": op.get("aliases") or [],
        "fts_text": prepare_fts_text(content, " ".join(op.get("keywords") or [])),
        "origin": origin,
        "provenance": "owner" if origin in {"user_stated", "user_requested"} else "agent",
        "status": status,
        "promoted": promoted,
        "hold_reason": op.get("hold_reason"),
        "flags": op.get("flags") or [],
        "evidence_count": 1 if origin == "user_stated" else 0,
        "observed_at": now,
        "valid_from": op.get("valid_from"),
        "valid_to": op.get("valid_to"),
        "source_host": host,
        "source_session": session_id,
        "content_hash": content_hash(content),
    }
