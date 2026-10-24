"""Domain service orchestrating remember/search/capture/inject/extract."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from thememory.config.loader import explain_config, resolve_api_key
from thememory.core.extraction import (
    apply_ops,
    build_user_prompt,
    evidence_span,
    item_from_add_op,
    promote_decision,
    resolve_extract_prompt,
    should_skip_extract,
    split_windows,
)
from thememory.core.host_status import inspect_hosts
from thememory.core.project_key import resolve_project_key
from thememory.core.redaction import clean_turn, is_fully_private, screen_directive
from thememory.core.residency import read_memory_gb, residency_from_config
from thememory.core.retrieval import (
    apply_deterministic_weights,
    drop_near_duplicates,
    filter_fts_by_threshold,
    rrf_fuse,
    scope_visible,
    tombstone_similar,
    trust_tier,
    vector_search,
)
from thememory.core.slots import load_slots, normalize_slot_key, slot_is_multi, slot_keys_for_prompt
from thememory.core.snapshot import render_delta, render_recall, render_snapshot, schedule_in_window
from thememory.installer.autostart import autostart_registered, runtime_status
from thememory.installer.capabilities import host_can, host_capabilities
from thememory.outbound.embed_onnx import MODEL_DIR_NAME, OnnxEmbedder
from thememory.outbound.llm_openai import OpenAICompatLLM
from thememory.paths import models_dir
from thememory.storage.backup import latest_backup_stamp, run_backup
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo, pack_vector, unpack_vector
from thememory.storage.repos.turns import TurnsRepo
from thememory.util import (
    content_hash,
    cosine,
    local_calendar_date,
    new_id,
    normalize_text,
    prepare_fts_text,
    shrink_code_blocks,
    truncate_turn,
    utc_now,
)

logger = logging.getLogger(__name__)

# Spec §7.5: one in-call retry, then exponential backoff. Cap keeps a poison
# payload from retrying forever; the failed row stays visible in doctor.
_PARSE_BACKOFF_BASE_S = 30
_PARSE_BACKOFF_CAP_S = 6 * 3600
_PARSE_MAX_ATTEMPTS = 6


def parse_backoff_seconds(attempts: int) -> int:
    exp = min(16, max(0, int(attempts) - 1))
    delay = _PARSE_BACKOFF_BASE_S * (2**exp)
    if delay > _PARSE_BACKOFF_CAP_S:
        return _PARSE_BACKOFF_CAP_S
    return delay


_ORIGIN_RANK = {
    "user_requested": 3,
    "user_stated": 2,
    "extracted": 1,
    "agent_inferred": 1,
    "untrusted": 0,
}
_NEGATION_PHRASES = ("不对", "不是这样", "记错", "别记", "不要记", "忘掉", "取消这条", "搞错了")


def _date_bounds(date: Any, date_range: Any) -> tuple[str, str] | None:
    start = end = ""
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start, end = str(date_range[0]), str(date_range[1])
    elif isinstance(date_range, dict):
        start = str(date_range.get("start") or date_range.get("from") or "")
        end = str(date_range.get("end") or date_range.get("to") or "")
    elif date:
        start = end = str(date)
    if not start or not end:
        return None
    if start > end:
        start, end = end, start
    return start, end


def _overlaps(valid_from: Any, valid_to: Any, start: str, end: str) -> bool:
    if not valid_from or not valid_to:
        return False
    return not (str(valid_to) < start or str(valid_from) > end)


def _highlight_flags(flags: Any) -> bool:
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except json.JSONDecodeError:
            return False
    if not isinstance(flags, list):
        return False
    return any(f == "directive_pattern" or str(f).startswith("crosscheck") for f in flags)


def _uptime_seconds(started: str | None) -> int | None:
    if not started:
        return None
    try:
        started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - started_at).total_seconds()))


def _low_signal(text: str, phrases: list[str]) -> bool:
    raw = text.strip()
    if len(raw) < 2:
        return True
    folded = {p.strip().lower() for p in phrases if p and str(p).strip()}
    if raw.lower() in folded:
        return True
    for ch in raw:
        code = ord(ch)
        if ch.isalnum() or 0x4E00 <= code <= 0x9FFF:
            return False
    return True


class MemoryService:
    def __init__(self, db: Database, cfg: dict[str, Any], root: Path | None = None) -> None:
        self.db = db
        self.cfg = cfg
        self.root = root
        self._config_error: str | None = None
        self.embedder = OnnxEmbedder(
            models_dir(root) / MODEL_DIR_NAME,
            query_instruction=str(cfg.get("embedding", {}).get("query_instruction") or ""),
        )
        ext = cfg.get("llm", {}).get("extraction", {})
        self.llm = OpenAICompatLLM(
            base_url=str(ext.get("base_url") or ""),
            model=str(ext.get("model") or ""),
            api_key=resolve_api_key(ext),
            temperature=float(ext.get("temperature") or 0),
            max_tokens=int(ext.get("max_tokens") or 4096),
            timeout_s=float(ext.get("timeout_s") or 120),
            json_mode=str(ext.get("json_mode") or "auto"),
        )
        self._last_embed_mono = 0.0
        self._query_vec_cache: dict[str, list[float]] = {}

    def note_config_error(self, message: str | None) -> None:
        """Hot reload failed: keep the previous cfg and surface config_invalid."""
        self._config_error = message
        if message:
            MemoryRepo(self.db.read_memory()).add_degrade(
                "config", "config_invalid", detail=message[:300]
            )

    def reload_config(self, cfg: dict[str, Any]) -> None:
        self._config_error = None
        self.cfg = cfg
        ext = cfg.get("llm", {}).get("extraction", {})
        self.llm = OpenAICompatLLM(
            base_url=str(ext.get("base_url") or ""),
            model=str(ext.get("model") or ""),
            api_key=resolve_api_key(ext),
            temperature=float(ext.get("temperature") or 0),
            max_tokens=int(ext.get("max_tokens") or 4096),
            timeout_s=float(ext.get("timeout_s") or 120),
            json_mode=str(ext.get("json_mode") or "auto"),
        )

    def host_policy(self, host: str) -> dict[str, Any]:
        hosts = self.cfg.get("hosts", {})
        return hosts.get(host) or hosts.get("default") or {
            "write": True,
            "read": True,
            "isolated": False,
            "procedural_write": False,
        }

    def host_isolated(self, host: str, *, kind: str = "profile") -> bool:
        policy = self.host_policy(host)
        if policy.get("isolated"):
            return True
        mode = (self.cfg.get("isolation") or {}).get(kind) or "shared"
        return mode == "isolated"

    # --- tools ---

    def is_paused(self) -> bool:
        until = MemoryRepo(self.db.read_memory()).meta_get("pause_until")
        if not until:
            return False
        if until == "forever":
            return True
        return until > utc_now()

    def set_pause(self, *, until: str | None) -> dict[str, Any]:
        def _w(conn: Any) -> None:
            repo = MemoryRepo(conn)
            if until:
                repo.meta_set("pause_until", until)
            else:
                repo.meta_delete("pause_until")

        self.db.run_write("memory", _w)
        return {"status": "ok", "paused": bool(until), "pause_until": until}

    def remember(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        policy = self.host_policy(host)
        if not policy.get("write", True):
            return {"status": "failed", "reason": "policy_denied"}
        if self.is_paused():
            return {"status": "failed", "reason": "incognito", "detail": "paused"}
        content = clean_turn(
            str(params.get("content") or ""),
            redact=bool(self.cfg["privacy"]["redact_secrets"]),
            private=bool(self.cfg["privacy"]["private_tag"]),
            extra=list(self.cfg["privacy"].get("extra_patterns") or []),
        )
        if not content:
            return {"status": "failed", "reason": "op_invalid", "detail": "empty content"}
        origin = params.get("origin") or "user_requested"
        if origin not in {"user_requested", "agent_inferred"}:
            return {"status": "failed", "reason": "op_invalid", "detail": "bad origin"}
        type_ = params.get("type") or "note"
        if type_ == "procedural" and not policy.get("procedural_write", False):
            return {"status": "failed", "reason": "policy_denied", "detail": "procedural_write"}
        observer = host
        chash = content_hash(content)
        if self.embedder.files_present():
            try:
                vec = self.embedder.embed([content], is_query=False)[0]
                tau = float(self.cfg["consolidation"]["tombstone_similarity"])
                prints = MemoryRepo(self.db.read_memory()).tombstone_fingerprints()
                if tombstone_similar(vec, prints, tau=tau):
                    return {
                        "status": "ok",
                        "effect": "suppressed",
                        "reason": "tombstone_suppressed",
                        "degraded_paths": [],
                    }
            except Exception:  # noqa: BLE001
                logger.exception("tombstone vector check failed")
        session_ref = params.get("session_ref")
        limit_decision = self._apply_write_limits(
            host=host, session_ref=session_ref, type_=str(type_)
        )
        slots = load_slots(self.root)
        slot_key = normalize_slot_key(params.get("slot_key"), slots)

        def _write(conn: Any) -> dict[str, Any]:
            repo = MemoryRepo(conn)
            if chash in repo.tombstone_hashes():
                return {"status": "ok", "effect": "suppressed", "reason": "tombstone_suppressed"}
            dup = repo.find_by_hash(chash, observer=observer)
            if dup:
                return {"status": "ok", "effect": "duplicate", "id": dup["id"], "item": dup}
            flags: list[str] = []
            hold = None
            status = "active"
            promoted = origin == "user_requested"
            session = None
            if session_ref:
                session = repo.get_session_by_host(host, str(session_ref))
            if session and int(session.get("contaminated") or 0):
                flags.append("session_contaminated")
                status = "candidate"
                promoted = False
                hold = "session_contaminated"
                repo.add_degrade(
                    "contamination",
                    "session_contaminated",
                    detail="remember during contaminated session",
                    host=host,
                    session_id=session.get("id"),
                )
            if limit_decision.get("force_candidate"):
                flags.append("rate_limited")
                status = "candidate"
                promoted = False
                hold = limit_decision.get("hold_reason") or "rate_limited"
                repo.add_degrade(
                    "rate_limit",
                    str(limit_decision.get("reason") or "burst"),
                    detail=str(limit_decision.get("detail") or ""),
                    host=host,
                    session_id=session_ref,
                )
            if limit_decision.get("reject"):
                repo.add_degrade(
                    "rate_limit",
                    str(limit_decision.get("reason") or "session_max"),
                    detail=str(limit_decision.get("detail") or ""),
                    host=host,
                    session_id=session_ref,
                )
                return {
                    "status": "failed",
                    "reason": str(limit_decision.get("reason") or "session_max_writes"),
                    "detail": limit_decision.get("detail"),
                }
            if self.cfg["privacy"].get("screen_enabled") and screen_directive(
                content, list(self.cfg["privacy"].get("screen_patterns") or [])
            ):
                flags.append("directive_pattern")
                status = "candidate"
                promoted = False
                hold = "directive_pattern"
            if type_ == "procedural" and origin == "user_requested":
                status = "candidate"
                promoted = False
                hold = "procedural_unverified"
            # crosscheck (spec §17.5): warn; procedural without match stays held
            if (
                origin == "user_requested"
                and self.cfg["privacy"].get("crosscheck_enabled")
                and session
            ):
                ok_check, score = self._crosscheck(content, session["id"])
                if not ok_check:
                    flags.append("crosscheck_low")
                    repo.add_degrade(
                        "crosscheck",
                        "crosscheck_low_similarity",
                        detail=f"score={score:.3f}",
                        host=host,
                        session_id=session["id"],
                    )
                    if type_ == "procedural":
                        status = "candidate"
                        promoted = False
                        hold = hold or "procedural_unverified"
            scope = params.get("scope")
            if not scope:
                scope = "project:local" if type_ == "procedural" else "user"
            item = repo.insert_item(
                {
                    "observer": observer,
                    "scope": scope,
                    "type": type_,
                    "slot_key": slot_key,
                    "content": content,
                    "keywords": [],
                    "aliases": [],
                    "origin": origin,
                    "provenance": "owner" if origin == "user_requested" else "agent",
                    "status": status,
                    "promoted": promoted,
                    "hold_reason": hold,
                    "flags": flags,
                    "valid_from": params.get("valid_from"),
                    "valid_to": params.get("valid_to"),
                    "source_host": host,
                    "source_session": session_ref,
                    "content_hash": chash,
                }
            )
            item = self._apply_slot_policy(repo, item)
            repo.record_change(item_id=item["id"], op="ADD", actor="agent", result=item)
            repo.enqueue_job("embed", {"item_id": item["id"]})
            hold_now = item.get("hold_reason") or hold
            effect = (
                "active_promoted"
                if item.get("promoted")
                else ("held" if hold_now else "candidate")
            )
            out: dict[str, Any] = {
                "status": "ok",
                "effect": effect,
                "id": item["id"],
                "item": item,
            }
            if effect == "held":
                out["notice"] = "已记录但暂缓生效"
            return out

        result = self.db.run_write("memory", _write)
        if result.get("effect") not in {"duplicate", "suppressed"} and result.get("id"):
            self._embed_now(result["id"])
        return {**result, "degraded_paths": []}

    def _apply_write_limits(
        self, *, host: str, session_ref: str | None, type_: str
    ) -> dict[str, Any]:
        """Enforce session / procedural / burst limits (spec §17.7)."""
        if not session_ref:
            return {}
        limits = self.cfg.get("limits") or {}
        max_writes = int(limits.get("session_max_writes") or 0)
        max_proc = int(limits.get("session_max_procedural") or 0)
        burst_window = int(limits.get("burst_window_seconds") or 0)
        burst_max = int(limits.get("burst_max_writes") or 0)
        repo = MemoryRepo(self.db.read_memory())
        total = repo.count_session_writes(host=host, session_ref=str(session_ref))
        if max_writes > 0 and total >= max_writes:
            return {
                "reject": True,
                "reason": "session_max_writes",
                "detail": f"{total}>={max_writes}",
            }
        if type_ == "procedural" and max_proc > 0:
            proc = repo.count_session_writes(
                host=host, session_ref=str(session_ref), type_="procedural"
            )
            if proc >= max_proc:
                return {
                    "reject": True,
                    "reason": "session_max_procedural",
                    "detail": f"{proc}>={max_proc}",
                }
        if burst_window > 0 and burst_max > 0:
            since = (
                datetime.now(UTC) - timedelta(seconds=burst_window)
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            burst = repo.count_session_writes(
                host=host, session_ref=str(session_ref), since=since
            )
            if burst >= burst_max:
                return {
                    "force_candidate": True,
                    "hold_reason": "burst_isolated",
                    "reason": "burst_max_writes",
                    "detail": f"{burst}>={burst_max} in {burst_window}s",
                }
        return {}

    def _slot_stable(self, slot_key: str | None) -> bool:
        if not slot_key:
            return False
        slots = load_slots(self.root)
        slot = slots.get(slot_key)
        if slot is not None:
            return slot.stable
        for candidate in slots.values():
            if not candidate.template:
                continue
            prefix = candidate.key.split(".<", 1)[0] + "."
            if slot_key.startswith(prefix) and len(slot_key) > len(prefix):
                return candidate.stable
        return False

    def _apply_slot_policy(self, repo: MemoryRepo, item: dict[str, Any]) -> dict[str, Any]:
        """Single-value slots: higher origin wins; a loser stays candidate."""
        slot_key = item.get("slot_key")
        if not slot_key or slot_is_multi(str(slot_key), load_slots(self.root)):
            return item
        rows = repo.conn.execute(
            """
            SELECT id FROM memory_items
            WHERE slot_key=? AND observer=? AND status='active' AND promoted=1 AND id!=?
            """,
            (slot_key, item["observer"], item["id"]),
        ).fetchall()
        new_rank = _ORIGIN_RANK.get(str(item.get("origin")), 0)
        losers: list[str] = []
        blocked = False
        for row in rows:
            old = repo.get(str(row["id"]))
            if not old:
                continue
            old_rank = _ORIGIN_RANK.get(str(old.get("origin")), 0)
            if new_rank < old_rank or (
                new_rank == old_rank and int(item.get("seq") or 0) < int(old.get("seq") or 0)
            ):
                blocked = True
                break
            if item.get("promoted"):
                losers.append(str(row["id"]))
        if blocked or not item.get("promoted"):
            if blocked:
                repo.update_item(
                    item["id"],
                    status="candidate",
                    promoted=False,
                    hold_reason="conflict_lower_priority",
                )
                repo.record_change(
                    item_id=item["id"],
                    op="CONFLICT",
                    actor="rule",
                    reason="conflict_lower_priority",
                )
                return repo.get(item["id"]) or item
            return item
        for old_id in losers:
            repo.update_item(
                old_id, status="superseded", promoted=False, superseded_by=item["id"]
            )
            repo.record_change(
                item_id=old_id, op="SUPERSEDE", actor="rule", reason="slot_replace"
            )
        return item

    def forget(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        if not self.host_policy(host).get("write", True):
            return {"status": "failed", "reason": "policy_denied"}
        item_id = params.get("id")
        query = params.get("query")

        def _write(conn: Any) -> dict[str, Any]:
            repo = MemoryRepo(conn)
            if not item_id and query:
                hits = repo.search_fts(str(query), limit=5)
                if not hits:
                    return {"status": "ok", "effect": "none", "candidates": []}
                if len(hits) > 1:
                    return {
                        "status": "ok",
                        "effect": "ambiguous",
                        "candidates": [
                            {"id": h[0]["id"], "content": h[0]["content"]} for h in hits
                        ],
                    }
                target = hits[0][0]
            else:
                target = repo.get(str(item_id)) if item_id else None
            if not target:
                return {"status": "failed", "reason": "op_invalid", "detail": "not found"}
            vec = None
            dim = None
            model_id = None
            emb = repo.conn.execute(
                "SELECT * FROM item_embeddings WHERE item_id=?", (target["id"],)
            ).fetchone()
            if emb:
                vec, dim, model_id = emb["vector"], emb["dim"], emb["model_id"]
            repo.add_tombstone(
                target["id"], target["type"], target["content_hash"], vec, model_id, dim
            )
            repo.update_item(target["id"], content="", status="deleted", promoted=False, fts_text="")
            repo.conn.execute("DELETE FROM item_embeddings WHERE item_id=?", (target["id"],))
            repo.conn.execute(
                "DELETE FROM item_fts WHERE rowid=?",
                (target["seq"],),
            )
            repo.record_change(item_id=target["id"], op="FORGET", actor="agent")
            return {"status": "ok", "effect": "deleted", "id": target["id"]}

        return {**self.db.run_write("memory", _write), "degraded_paths": []}

    def correct(self, params: dict[str, Any]) -> dict[str, Any]:
        old_id = params.get("id")
        new_content = params.get("new_content")
        if not old_id or not new_content:
            return {"status": "failed", "reason": "op_invalid"}
        host = params.get("host") or "cli"
        remembered = self.remember(
            {
                **params,
                "content": new_content,
                "origin": params.get("origin") or "user_requested",
                "host": host,
            }
        )
        if remembered.get("status") != "ok" or not remembered.get("id"):
            return remembered

        def _write(conn: Any) -> None:
            repo = MemoryRepo(conn)
            repo.update_item(str(old_id), status="superseded", promoted=False, superseded_by=remembered["id"])
            repo.update_item(remembered["id"], supersedes_id=str(old_id))
            repo.record_change(
                item_id=remembered["id"],
                op="CORRECT",
                actor="agent",
                reason=params.get("reason"),
            )

        self.db.run_write("memory", _write)
        return {
            "status": "ok",
            "id": remembered["id"],
            "supersedes": old_id,
            "item": remembered.get("item"),
            "degraded_paths": [],
        }

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        if not self.host_policy(host).get("read", True):
            return {"status": "failed", "reason": "policy_denied"}
        query = params.get("query") or ""
        date = params.get("date")
        date_range = params.get("date_range")
        limit = int(params.get("limit") or self.cfg["retrieval"]["search_limit"])
        started = time.monotonic()
        deadline_ms = params.get("deadline_ms")
        deadline_hit = False
        degraded: list[str] = []
        repo = MemoryRepo(self.db.read_memory())
        project_raw = params.get("project_root") or params.get("project")
        project_key = resolve_project_key(str(project_raw)) if project_raw else None
        project_scope = f"project:{project_key}" if project_key else params.get("scope")
        visible_project = project_scope if str(project_scope or "").startswith("project:") else None
        injecting = params.get("channel") == "inject"
        retrieval = self.cfg["retrieval"]
        tau_cos = float(
            retrieval["tau_inject_cosine"] if injecting else retrieval["tau_search_cosine"]
        )
        tau_bm25 = float(
            retrieval["tau_inject_bm25"] if injecting else retrieval["tau_search_bm25"]
        )
        statuses = ["active"]
        if params.get("include_candidates"):
            statuses.append("candidate")
        if params.get("include_history"):
            statuses.append("superseded")
        bounds = _date_bounds(date, date_range)
        if bounds:
            # Date lookup bypasses similarity thresholds (spec §9.4).
            start, end = bounds
            needle = str(query or "").strip().casefold()
            items = []
            for item in repo.list_active(types=["event", "plan"], limit=200):
                if not _overlaps(item.get("valid_from"), item.get("valid_to"), start, end):
                    continue
                if needle:
                    blob = str(item.get("content") or "").casefold()
                    keys = " ".join(str(k) for k in (item.get("keywords") or [])).casefold()
                    if needle not in blob and needle not in keys:
                        continue
                copied = dict(item)
                copied["matched_by"] = "date"
                items.append(copied)
            items = items[:limit]
        else:
            fts_hits: list[tuple[dict[str, Any], float]] = []
            vec_hits: list[tuple[dict[str, Any], float]] = []
            if query:
                try:
                    raw_fts = repo.search_fts(str(query), limit=limit * 2, statuses=tuple(statuses))
                    fts_hits = filter_fts_by_threshold(raw_fts, tau_bm25, query=str(query))
                    # sqlite bm25 on a small index is ~1e-6, far under the uncalibrated τ.
                    # Dropping every hit would blank search until M4 recalibrates the scale.
                    if raw_fts and not fts_hits:
                        strongest = max(-float(score) for _item, score in raw_fts)
                        if strongest < tau_bm25:
                            fts_hits = raw_fts
                except Exception as exc:  # noqa: BLE001
                    degraded.append("fts")
                    repo.add_degrade("fts", "fts_unavailable", str(exc), host=host)
            over_deadline = (
                deadline_ms is not None
                and (time.monotonic() - started) * 1000 >= float(deadline_ms)
            )
            if query and not over_deadline and self.embedder.available():
                try:
                    self._last_embed_mono = time.monotonic()
                    qv = self._embed_query(str(query))
                    vec_hits = vector_search(
                        repo,
                        qv,
                        model_id=self.embedder.model_id,
                        tau=tau_cos,
                        limit=limit * 2,
                        statuses=set(statuses),
                    )
                except Exception as exc:  # noqa: BLE001
                    degraded.append("vector")
                    MemoryRepo(self.db.read_memory()).add_degrade(
                        "vector", "vector_unavailable", str(exc), host=host
                    )
            elif query and over_deadline:
                deadline_hit = True
                degraded.append("vector")
            elif query:
                degraded.append("vector")

            items = rrf_fuse(
                fts_hits,
                vec_hits,
                k=int(self.cfg["retrieval"]["rrf_k"]),
                limit=limit * 2,
            )
            items = apply_deterministic_weights(
                items,
                project_scope=visible_project,
                today=local_calendar_date(params.get("tz")),
            )
            wanted = {item["id"] for item in items}
            vectors = {
                item_id: unpack_vector(blob, dim)
                for item_id, blob, dim in repo.all_embeddings(self.embedder.model_id)
                if item_id in wanted
            }
            items = drop_near_duplicates(
                items,
                vectors,
                tau=float(self.cfg["consolidation"]["dedupe_similarity"]),
            )[:limit]
        if self.host_isolated(host):
            items = [i for i in items if i.get("observer") == host]
        items = [i for i in items if scope_visible(i, visible_project)]

        types = params.get("types")
        if types:
            items = [i for i in items if i.get("type") in types]
        allowed_status = set(statuses)
        items = [i for i in items if i.get("status") in allowed_status]

        out_items = []
        for item in items:
            out_items.append(
                {
                    "id": item["id"],
                    "content": item["content"],
                    "type": item["type"],
                    "scope": item["scope"],
                    "trust": trust_tier(item)
                    if item.get("origin") in {"user_requested", "user_stated"}
                    else "medium",
                    "observed_at": item.get("observed_at"),
                    "valid_from": item.get("valid_from"),
                    "valid_to": item.get("valid_to"),
                    "matched_by": item.get("matched_by", "fts"),
                    "source_label": item.get("source_host"),
                }
            )
        status = "degraded" if degraded else "ok"
        result: dict[str, Any] = {
            "status": status,
            "items": out_items,
            "degraded_paths": degraded,
        }
        if deadline_hit:
            result["reason"] = "deadline_partial"
        return result

    def _snapshot_view(self, session: dict[str, Any] | None, tz: str | None) -> dict[str, Any]:
        zone = (session or {}).get("tz") or tz
        key = (session or {}).get("project_key")
        return {
            "tz": zone,
            "today": local_calendar_date(zone),
            "project_scope": f"project:{key}" if key else None,
        }

    def _note_snapshot_budget(
        self, truncated: list[str], *, host: str, session_id: str | None
    ) -> None:
        if not truncated:
            return
        MemoryRepo(self.db.read_memory()).add_degrade(
            "snapshot",
            "snapshot_over_budget",
            ",".join(truncated),
            host=host,
            session_id=session_id,
        )

    def context(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        policy = self.host_policy(host)
        if not policy.get("read", True):
            return {"status": "failed", "reason": "policy_denied"}
        force_full = params.get("force_full") in (True, "true", "1", 1)
        session = self.ensure_session(params) if params.get("session_ref") else None
        repo = MemoryRepo(self.db.read_memory())
        if session and session.get("snapshot") and not force_full:
            since = repo.meta_get(f"snapshot_at:{session['id']}")
            changed = repo.items_changed_since(int(session.get("snapshot_seq") or 0), since)
            if self.host_isolated(host):
                changed = [i for i in changed if i.get("observer") == host]
            block = render_delta(changed, tz=session.get("tz"))
            return {
                "status": "ok",
                "incremental": True,
                "block": block,
                "item_count": len(changed),
                "degraded_paths": [],
                "session_id": session["id"],
            }
        items = repo.list_promoted(
            observer=host, isolated=self.host_isolated(host), limit=200
        )
        snap_cfg = self.cfg["snapshot"]
        view = self._snapshot_view(session, params.get("tz"))
        truncated: list[str] = []
        block, digest = render_snapshot(
            items,
            budgets={k: int(v) for k, v in snap_cfg.items() if k.startswith("budget_")},
            annotated=bool(snap_cfg.get("annotated", True)),
            window_days=int(snap_cfg.get("window_days") or 14),
            truncated=truncated,
            **view,
        )
        self._note_snapshot_budget(truncated, host=host, session_id=session["id"] if session else None)
        return {
            "status": "ok",
            "incremental": False,
            "block": block,
            "snapshot_hash": digest,
            "item_count": len(items),
            "degraded_paths": [],
        }

    # --- capture / inject ---

    def ensure_session(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params["host"]
        host_session = params.get("session_ref") or new_id()

        raw_project = params.get("project_root") or params.get("project")
        project_key = resolve_project_key(str(raw_project)) if raw_project else None

        def _write(conn: Any) -> dict[str, Any]:
            repo = MemoryRepo(conn)
            return repo.upsert_session(
                {
                    "host": host,
                    "host_session_id": host_session,
                    "project_key": project_key,
                    "tz": params.get("tz"),
                }
            )

        return self.db.run_write("memory", _write)

    def capture_event(self, params: dict[str, Any]) -> dict[str, Any]:
        event = params.get("event")
        session = self.ensure_session(params)
        if event == "session_start":

            def _clear(conn: Any) -> None:
                MemoryRepo(conn).clear_contamination(session["id"])

            self.db.run_write("memory", _clear)
        elif event == "tool_risk":
            host_name = str(params.get("host") or "")
            if not host_can(host_name, "auto_capture"):
                return {
                    "accepted": True,
                    "session_id": session["id"],
                    "contaminated": False,
                    "reason": "no_tool_signal",
                }
            tool = str(params.get("tool") or params.get("tool_name") or "")
            risky = [
                t.lower()
                for t in (self.cfg.get("privacy", {}).get("high_risk_tools") or [])
            ]
            name_l = tool.lower()
            if any(r in name_l for r in risky if r):

                def _mark(conn: Any) -> None:
                    repo = MemoryRepo(conn)
                    repo.mark_contaminated(session["id"])
                    repo.add_degrade(
                        "contamination",
                        "high_risk_tool",
                        detail=tool,
                        host=params.get("host"),
                        session_id=session["id"],
                    )

                self.db.run_write("memory", _mark)
                return {
                    "accepted": True,
                    "session_id": session["id"],
                    "contaminated": True,
                    "tool": tool,
                }
        elif event == "session_end":

            def _end(conn: Any) -> None:
                MemoryRepo(conn).end_session(session["id"])
                MemoryRepo(conn).clear_contamination(session["id"])

            self.db.run_write("memory", _end)
            # trigger extract on session end
            self.db.run_write(
                "memory",
                lambda c: MemoryRepo(c).enqueue_job(
                    "extract", {"session_id": session["id"], "reason": "session_end"}
                ),
            )
        return {"accepted": True, "session_id": session["id"]}

    def capture_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.is_paused() or os_environ_incognito():
            return {"accepted": False, "reason": "incognito"}
        session = self.ensure_session(params)
        if int(session.get("incognito") or 0):
            return {"accepted": False, "reason": "incognito", "session_id": session["id"]}
        raw_content = str(params.get("content") or "")
        if self.cfg["privacy"].get("private_tag", True) and is_fully_private(raw_content):
            return {"accepted": False, "reason": "incognito", "session_id": session["id"]}
        content = clean_turn(
            str(params.get("content") or ""),
            redact=bool(self.cfg["privacy"]["redact_secrets"]),
            private=bool(self.cfg["privacy"]["private_tag"]),
            extra=list(self.cfg["privacy"].get("extra_patterns") or []),
        )
        turns_cfg = self.cfg.get("turns") or {}
        content = shrink_code_blocks(
            content, keep=bool(turns_cfg.get("keep_code_blocks", False))
        )
        content = truncate_turn(content, int(turns_cfg.get("max_chars_per_turn") or 200_000))
        role = params.get("role") or "user"
        turn_id = params.get("turn_id") or new_id()
        skip = list(self.cfg.get("extraction", {}).get("skip_phrases") or [])
        if role == "user" and content in skip:
            return {
                "accepted": True,
                "deduped": False,
                "turn_id": turn_id,
                "reason": "skip_phrase",
                "session_id": session["id"],
            }
        if not content:
            # A turn without text carries nothing to extract and would only
            # pollute the log (empty prompts, hooks that lost their payload).
            return {
                "accepted": True,
                "deduped": False,
                "turn_id": turn_id,
                "reason": "empty_content",
                "session_id": session["id"],
            }

        def _write_turns(conn: Any) -> tuple[dict[str, Any], bool]:
            return TurnsRepo(conn).append(
                session_id=session["id"],
                host=params["host"],
                host_session_id=session["host_session_id"],
                host_turn_id=str(turn_id),
                role=role,
                content=content,
                tz=params.get("tz"),
                local_date=params.get("local_date") or local_calendar_date(params.get("tz")),
            )

        turn, deduped = self.db.run_write("turns", _write_turns)
        # bump session
        self.db.run_write("memory", lambda c: MemoryRepo(c).upsert_session(
            {"host": params["host"], "host_session_id": session["host_session_id"]}
        ))
        # maybe enqueue extract
        count = TurnsRepo(self.db.read_turns()).count_unextracted(session["id"])
        trigger = int(self.cfg["extraction"]["trigger_turns"])
        if count >= trigger:
            self.db.run_write(
                "memory",
                lambda c: MemoryRepo(c).enqueue_job(
                    "extract", {"session_id": session["id"], "reason": "trigger_turns"}
                ),
            )
        if role == "user":
            self._note_negation(session["id"], content)
        return {
            "accepted": True,
            "turn_id": turn["id"],
            "deduped": deduped,
            "session_id": session["id"],
        }

    def inject_session_start(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        policy = self.host_policy(host)
        if not policy.get("read", True):
            return {"status": "failed", "reason": "policy_denied", "block": ""}
        session = self.ensure_session(params)
        if session.get("snapshot") and not params.get("force"):
            return {
                "status": "ok",
                "block": session["snapshot"],
                "snapshot_hash": session.get("snapshot_hash"),
                "frozen": True,
                "degraded_paths": [],
                "session_id": session["id"],
            }
        repo = MemoryRepo(self.db.read_memory())
        items = repo.list_promoted(
            observer=host, isolated=self.host_isolated(host), limit=200
        )
        turns_repo = TurnsRepo(self.db.read_turns())
        snap_cfg = self.cfg["snapshot"]
        hours = float(snap_cfg.get("unextracted_max_age_hours") or 24)
        since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        unextracted = turns_repo.unextracted_other(
            session["id"],
            since=since,
            limit=int(snap_cfg.get("unextracted_max_turns") or 6),
        )
        summaries = repo.recent_summaries(limit=3)
        summary_text = "\n".join(
            f"- {(row.get('summary_at') or '')[:10]}：{row.get('summary')}"
            for row in summaries
            if row.get("summary")
        )
        view = self._snapshot_view(session, None)
        truncated: list[str] = []
        block, digest = render_snapshot(
            items,
            summary=summary_text or session.get("summary"),
            unextracted=unextracted,
            budgets={k: int(v) for k, v in snap_cfg.items() if k.startswith("budget_")},
            annotated=bool(snap_cfg.get("annotated", True)),
            window_days=int(snap_cfg.get("window_days") or 14),
            truncated=truncated,
            **view,
        )
        self._note_snapshot_budget(truncated, host=host, session_id=session["id"])

        def _save(conn: Any) -> None:
            r = MemoryRepo(conn)
            r.save_snapshot(session["id"], block, digest, r.max_seq())
            r.meta_set(f"snapshot_at:{session['id']}", utc_now())
            for item in items:
                r.ledger_add(session["id"], item["id"], "session_start")

        self.db.run_write("memory", _save)
        return {
            "status": "ok",
            "block": block,
            "snapshot_hash": digest,
            "degraded_paths": [],
            "session_id": session["id"],
        }

    def inject_per_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        if not self.host_policy(host).get("read", True):
            return {"status": "ok", "block": None, "item_count": 0, "skipped_reason": "policy"}
        msg = str(params.get("user_message") or "").strip()
        phrases = list(self.cfg.get("extraction", {}).get("skip_phrases") or [])
        if _low_signal(msg, phrases):
            return {"status": "ok", "block": None, "item_count": 0, "skipped_reason": "short"}
        session = self.ensure_session(params)
        skip = self._per_turn_skip(session["id"], msg)
        if skip:
            return {"status": "ok", "block": None, "item_count": 0, "skipped_reason": skip}
        search_res = self.search(
            {
                "host": host,
                "query": msg,
                "limit": int(self.cfg["retrieval"]["inject_max_items"]),
                "channel": "inject",
            }
        )
        items = search_res.get("items") or []
        # filter by inject cosine conceptually already in search; ledger dedupe
        repo = MemoryRepo(self.db.read_memory())
        fresh = []
        seen: set[str] = set()
        for it in items:
            if repo.ledger_has(session["id"], it["id"]) or it["id"] in seen:
                continue
            fresh.append(it)
            seen.add(it["id"])
        today = local_calendar_date(session.get("tz"))
        window_days = int(self.cfg.get("snapshot", {}).get("window_days") or 14)
        snap_seq = int(session.get("snapshot_seq") or 0)
        for item in repo.list_active(types=["event", "plan"], limit=50):
            if int(item.get("seq") or 0) <= snap_seq or item["id"] in seen:
                continue
            if not schedule_in_window(item, today, window_days):
                continue
            if repo.ledger_has(session["id"], item["id"]):
                continue
            fresh.append(item)
            seen.add(item["id"])
        if not fresh:
            self._remember_negative_query(msg)
            return {
                "status": search_res.get("status", "ok"),
                "block": None,
                "item_count": 0,
                "skipped_reason": "ledger_dedupe" if items else "negative",
                "degraded_paths": search_res.get("degraded_paths") or [],
            }
        block, kept = render_recall(
            fresh,
            tz=session.get("tz"),
            max_chars=int(self.cfg["retrieval"]["inject_max_chars"]),
            max_items=int(self.cfg["retrieval"]["inject_max_items"]),
        )

        def _ledger(conn: Any) -> None:
            r = MemoryRepo(conn)
            for it in kept:
                r.ledger_add(session["id"], it["id"], "per_turn")

        self.db.run_write("memory", _ledger)
        if kept:
            self._mark_injected(session["id"], msg)
        return {
            "status": search_res.get("status", "ok"),
            "block": block,
            "item_count": len(kept),
            "degraded_paths": search_res.get("degraded_paths") or [],
        }

    def inject_after_compact(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.inject_session_start(params)

    # --- embedding / extraction workers ---

    def _embed_now(self, item_id: str) -> None:
        if not self.embedder.available():
            return
        item = MemoryRepo(self.db.read_memory()).get(item_id)
        if not item or not item.get("content"):
            return
        try:
            vec = self.embedder.embed([item["content"]], is_query=False)[0]
            blob = pack_vector(vec)

            def _w(conn: Any) -> None:
                MemoryRepo(conn).upsert_embedding(
                    item_id, self.embedder.model_id, len(vec), blob
                )

            self.db.run_write("memory", _w)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed failed: %s", exc)

    def process_jobs(self, limit: int = 5) -> int:
        def _claim(conn: Any) -> list[dict[str, Any]]:
            return MemoryRepo(conn).claim_jobs(limit=limit)

        jobs = self.db.run_write("memory", _claim)
        done = 0
        for job in jobs:
            ok = True
            err = None
            try:
                if job["kind"] == "embed":
                    self._embed_now(job["payload"]["item_id"])
                elif job["kind"] == "extract":
                    extracted = self.run_extract(job["payload"]["session_id"])
                    reason = extracted.get("reason")
                    if reason == "llm_budget_exceeded":
                        jid = job["id"]
                        self.db.run_write(
                            "memory",
                            lambda c, jid=jid: MemoryRepo(c).requeue_job(
                                jid, "llm_budget_exceeded"
                            ),
                        )
                        done += 1
                        continue
                    if reason == "llm_parse_failed":
                        # claim_jobs increments attempts after the row was read.
                        attempts = int(job.get("attempts") or 0) + 1
                        detail = str(extracted.get("detail") or reason)
                        jid = job["id"]
                        if attempts >= _PARSE_MAX_ATTEMPTS:
                            self.db.run_write(
                                "memory",
                                lambda c, jid=jid, detail=detail: MemoryRepo(c).finish_job(
                                    jid, False, detail
                                ),
                            )
                        else:
                            delay = parse_backoff_seconds(attempts)
                            next_run = (
                                datetime.now(UTC) + timedelta(seconds=delay)
                            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                            self.db.run_write(
                                "memory",
                                lambda c, jid=jid, detail=detail, next_run=next_run: MemoryRepo(
                                    c
                                ).requeue_job(jid, detail, next_run_at=next_run),
                            )
                        done += 1
                        continue
                elif job["kind"] == "backup":
                    self.run_db_backup()
                elif job["kind"] == "maintenance":
                    self.run_maintenance()
                elif job["kind"] == "consolidate":
                    self.run_near_dup_consolidate()
                else:
                    ok = False
                    err = f"unknown kind {job['kind']}"
            except Exception as exc:  # noqa: BLE001
                ok = False
                err = str(exc)
                logger.exception("job failed")
            jid = job["id"]
            self.db.run_write(
                "memory", lambda c, jid=jid, ok=ok, err=err: MemoryRepo(c).finish_job(jid, ok, err)
            )
            done += 1
        return done

    def evaluate_idle_extract(self) -> int:
        """Enqueue extract for sessions idle longer than trigger_idle_hours."""
        hours = float(self.cfg.get("extraction", {}).get("trigger_idle_hours") or 0)
        if hours <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        queued = 0
        for session in MemoryRepo(self.db.read_memory()).list_open_sessions():
            if (session.get("last_event_at") or "") > cutoff:
                continue
            pending = TurnsRepo(self.db.read_turns()).count_unextracted(session["id"])
            if pending <= 0:
                continue
            sid = session["id"]

            def _enq(conn: Any, sid: str = sid) -> None:
                MemoryRepo(conn).enqueue_job(
                    "extract", {"session_id": sid, "reason": "trigger_idle"}
                )

            self.db.run_write("memory", _enq)
            queued += 1
        return queued

    def consume_inbox(self, limit: int = 20) -> int:
        """Replay hook-degraded writes from inbox (spec §6.6 / §13.2)."""

        def _claim(conn: Any) -> list[dict[str, Any]]:
            return MemoryRepo(conn).claim_inbox(limit=limit)

        rows = self.db.run_write("memory", _claim)
        done = 0
        for row in rows:
            ok = True
            try:
                payload = row.get("payload") or {}
                method = payload.get("method")
                params = payload.get("params") or {}
                if method == "capture.turn":
                    self.capture_turn(params)
                elif method == "capture.event":
                    self.capture_event(params)
                elif method == "tool.remember":
                    self.remember(params)
                else:
                    ok = False
                    logger.warning("inbox unknown method: %s", method)
            except Exception:  # noqa: BLE001
                ok = False
                logger.exception("inbox replay failed id=%s", row.get("id"))
            iid = int(row["id"])
            self.db.run_write(
                "memory",
                lambda c, iid=iid, ok=ok: MemoryRepo(c).finish_inbox(iid, ok=ok),
            )
            done += 1
        return done

    def llm_budget_allows(self) -> tuple[bool, str]:
        budget = (self.cfg.get("llm") or {}).get("budget") or {}
        if str(budget.get("on_exceed") or "pause") != "pause":
            return True, ""
        daily = int(budget.get("daily_tokens") or 0)
        monthly = int(budget.get("monthly_tokens") or 0)
        now = datetime.now(UTC)
        day_start = now.strftime("%Y-%m-%dT00:00:00.000Z")
        month_start = now.strftime("%Y-%m-01T00:00:00.000Z")
        repo = MemoryRepo(self.db.read_memory())
        if daily > 0 and repo.usage_tokens_since(day_start) >= daily:
            return False, "daily"
        if monthly > 0 and repo.usage_tokens_since(month_start) >= monthly:
            return False, "monthly"
        return True, ""

    def _log_llm_usage(self, purpose: str, prompt: str, completion: str) -> None:
        # Rough token estimate: ~1 token / 4 chars. Enough for budget accounting.
        pt = max(1, len(prompt) // 4)
        ct = max(1, len(completion) // 4)

        def _w(conn: Any) -> None:
            MemoryRepo(conn).log_usage(
                purpose=purpose,
                model=self.llm.model or "",
                prompt_tokens=pt,
                completion_tokens=ct,
                ok=True,
            )

        self.db.run_write("memory", _w)

    def _per_turn_skip(self, session_id: str, message: str) -> str | None:
        interval = int(self.cfg.get("retrieval", {}).get("inject_min_interval_turns") or 0)
        repo = MemoryRepo(self.db.read_memory())
        gap_key = f"inject_gap:{session_id}"
        last_key = f"inject_last:{session_id}"
        gap = int(repo.meta_get(gap_key) or "0") + 1
        last = int(repo.meta_get(last_key) or "0")
        norm = normalize_text(message)[:200]
        reason = None
        if self._topic_unchanged(session_id, message):
            reason = "topic_unchanged"
        elif interval > 1 and last > 0 and (gap - last) < interval:
            reason = "interval"
        elif self._negative_cached(norm):
            reason = "negative_cache"
        self.db.run_write("memory", lambda c, gap=gap: MemoryRepo(c).meta_set(gap_key, str(gap)))
        return reason

    def _topic_unchanged(self, session_id: str, message: str) -> bool:
        if not self.embedder.available():
            return False
        import json

        raw = MemoryRepo(self.db.read_memory()).meta_get(f"inject_qvec:{session_id}")
        if not raw:
            return False
        try:
            previous = json.loads(raw)
            current = self.embedder.embed([message], is_query=True)[0]
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(previous, list):
            return False
        return cosine([float(x) for x in previous], [float(x) for x in current]) >= 0.9

    def _mark_injected(self, session_id: str, message: str) -> None:
        import json

        gap = MemoryRepo(self.db.read_memory()).meta_get(f"inject_gap:{session_id}") or "0"
        vector: list[float] | None = None
        if self.embedder.available():
            try:
                vector = [round(float(x), 5) for x in self.embedder.embed([message], is_query=True)[0]]
            except Exception:  # noqa: BLE001
                vector = None

        def _w(conn: Any) -> None:
            repo = MemoryRepo(conn)
            repo.meta_set(f"inject_last:{session_id}", str(gap))
            if vector is not None:
                repo.meta_set(f"inject_qvec:{session_id}", json.dumps(vector))

        self.db.run_write("memory", _w)

    def _negative_cached(self, norm: str) -> bool:
        import json

        raw = MemoryRepo(self.db.read_memory()).meta_get("inject_negative") or "[]"
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            return False
        now = utc_now()
        return any(r.get("q") == norm and str(r.get("until") or "") > now for r in rows if isinstance(r, dict))

    def _remember_negative_query(self, message: str) -> None:
        import json

        norm = normalize_text(message)[:200]
        if not norm:
            return
        until = (datetime.now(UTC) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        def _w(conn: Any) -> None:
            repo = MemoryRepo(conn)
            try:
                rows = json.loads(repo.meta_get("inject_negative") or "[]")
            except json.JSONDecodeError:
                rows = []
            rows = [r for r in rows if isinstance(r, dict) and str(r.get("until") or "") > utc_now()]
            rows.append({"q": norm, "until": until})
            repo.meta_set("inject_negative", json.dumps(rows[-200:], ensure_ascii=False))

        self.db.run_write("memory", _w)

    def recover_jobs(self) -> int:
        """Move jobs left in running by a crashed process back to pending."""
        return int(self.db.run_write("memory", lambda c: MemoryRepo(c).recover_running_jobs()))

    def run_db_backup(self) -> dict[str, Any]:
        keep = int(self.cfg.get("backup", {}).get("keep") or 7)
        try:
            paths = run_backup(self.root, keep=keep)
            return {"status": "ok", **paths}
        except Exception as exc:  # noqa: BLE001
            MemoryRepo(self.db.read_memory()).add_degrade(
                "backup", "backup_failed", str(exc)
            )
            logger.exception("backup failed")
            return {"status": "failed", "reason": "backup_failed", "detail": str(exc)}

    def maybe_run_backup(self) -> dict[str, Any] | None:
        hours = float(self.cfg.get("backup", {}).get("interval_hours") or 0)
        if hours <= 0:
            return None
        latest = latest_backup_stamp(self.root)
        if latest:
            try:
                last = datetime.strptime(latest, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                if datetime.now(UTC) - last < timedelta(hours=hours):
                    return None
            except ValueError:
                pass
        return self.run_db_backup()

    def run_maintenance(self) -> dict[str, Any]:
        """plan→event expiry and candidate TTL cleanup (spec §7.7 / §8.4)."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        ttl_days = int(self.cfg.get("consolidation", {}).get("candidate_ttl_days") or 60)
        cutoff = (datetime.now(UTC) - timedelta(days=ttl_days)).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"

        def _run(conn: Any) -> dict[str, int]:
            repo = MemoryRepo(conn)
            return {
                "expired_plans": repo.expire_plans(today),
                "cleaned_candidates": repo.cleanup_candidates(cutoff),
            }

        return {"status": "ok", **self.db.run_write("memory", _run)}

    def _complete_extract(self, system: str, user: str) -> dict[str, Any]:
        """Call the extractor once, then once more with the parse error (spec §7.5)."""
        try:
            return self.llm.complete_json(system, user)
        except Exception as first:
            retry_user = (
                user
                + "\n\n上一次输出无法解析，错误："
                + str(first)
                + "\n请只输出一个合法 JSON 对象，不要附加说明。"
            )
            return self.llm.complete_json(system, retry_user)

    def run_extract(self, session_id: str) -> dict[str, Any]:
        if not self.llm.available():
            MemoryRepo(self.db.read_memory()).add_degrade(
                "llm", "llm_unreachable", "not configured", session_id=session_id
            )
            return {"status": "failed", "reason": "llm_unreachable"}
        allowed, budget_reason = self.llm_budget_allows()
        if not allowed:
            MemoryRepo(self.db.read_memory()).add_degrade(
                "llm", "llm_budget_exceeded", budget_reason, session_id=session_id
            )
            return {"status": "paused", "reason": "llm_budget_exceeded", "detail": budget_reason}
        session = MemoryRepo(self.db.read_memory()).get_session(session_id)
        if not session:
            return {"status": "failed", "reason": "op_invalid"}
        turns = TurnsRepo(self.db.read_turns()).pending_for_session(
            session_id, after_seq=int(session.get("extract_cursor") or 0)
        )
        if not turns:
            return {"status": "ok", "ops": 0}
        phrases = list(self.cfg.get("extraction", {}).get("skip_phrases") or [])
        skipped = [t for t in turns if should_skip_extract(t, phrases)]
        if skipped:
            self.db.run_write(
                "turns",
                lambda c, ids=[t["id"] for t in skipped]: TurnsRepo(c).mark_extracted(ids),
            )
        turns = [t for t in turns if not should_skip_extract(t, phrases)]
        if not turns:
            return {"status": "ok", "ops": 0, "skipped": len(skipped)}
        batches = split_windows(turns, int(self.cfg["extraction"]["window_tokens"]))
        total_ops = 0
        summary = None
        prior_summary = ""
        host = session["host"]
        system_prompt, prompt_version, prompt_issue = resolve_extract_prompt(self.cfg, self.root)
        if prompt_issue:
            MemoryRepo(self.db.read_memory()).add_degrade(
                "extraction", prompt_issue, "using builtin prompt", host=host, session_id=session_id
            )
        for batch in batches:
            existing = MemoryRepo(self.db.read_memory()).list_active(limit=40)
            user_prompt = build_user_prompt(
                batch,
                existing,
                slots_text=slot_keys_for_prompt(load_slots(self.root)),
                prior_summary=prior_summary,
            )
            try:
                data = self._complete_extract(system_prompt, user_prompt)
            except Exception as exc:  # noqa: BLE001
                MemoryRepo(self.db.read_memory()).add_degrade(
                    "extraction", "llm_parse_failed", str(exc), host=host, session_id=session_id
                )
                return {"status": "failed", "reason": "llm_parse_failed", "detail": str(exc)}
            summary = data.get("session_summary") or summary
            prior_summary = str(summary or prior_summary)
            self._log_llm_usage("extract", user_prompt, str(data))
            ops = data.get("ops") or []
            tomb = MemoryRepo(self.db.read_memory()).tombstone_hashes()

            def make_remember(op: dict[str, Any], turn_by_id: dict[str, dict[str, Any]]) -> Any:
                item = item_from_add_op(
                    op, host=host, observer=host, session_id=session_id, now=utc_now()
                )
                item["evidence_count"] = evidence_span(
                    [turn_by_id[eid] for eid in (op.get("evidence_turns") or []) if eid in turn_by_id]
                )
                status, promoted = promote_decision(
                    item["origin"],
                    item["evidence_count"],
                    int(self.cfg["consolidation"]["promote_repeat"]),
                    item["type"],
                    stable=self._slot_stable(item.get("slot_key")),
                )
                if "directive_pattern" in (item.get("flags") or []):
                    status, promoted = "candidate", False
                item["status"] = status
                item["promoted"] = promoted

                def _w(conn: Any) -> dict[str, Any]:
                    repo = MemoryRepo(conn)
                    if repo.find_by_hash(item["content_hash"], observer=host):
                        return {"duplicate": True}
                    created = repo.insert_item(item)
                    created = self._apply_slot_policy(repo, created)
                    protect: list[str] = []
                    for eid in op.get("evidence_turns") or []:
                        turn = turn_by_id[eid]
                        repo.conn.execute(
                            """
                            INSERT OR IGNORE INTO item_evidence(item_id, turn_id, role, quote)
                            VALUES (?,?,?,?)
                            """,
                            (created["id"], eid, turn["role"], op.get("quote")),
                        )
                        protect.append(str(eid))
                    created["protect_turn_ids"] = protect
                    repo.record_change(
                        item_id=created["id"],
                        op="ADD",
                        actor="llm",
                        proposal=op,
                        prompt_version=prompt_version,
                    )
                    repo.enqueue_job("embed", {"item_id": created["id"]})
                    return created

                created = self.db.run_write("memory", _w)
                protect_ids = list(created.get("protect_turn_ids") or [])
                if protect_ids and self.cfg.get("turns", {}).get("protect_evidence", True):
                    self.db.run_write(
                        "turns",
                        lambda c, ids=protect_ids: TurnsRepo(c).mark_protected(ids),
                    )
                if created.get("id"):
                    self._embed_now(created["id"])
                return created

            def make_update(op: dict[str, Any], turn_by_id: dict[str, dict[str, Any]]) -> Any:
                tid = op.get("target_id")
                content = op.get("content")
                if not tid or not content:
                    return None

                def _w(conn: Any) -> Any:
                    repo = MemoryRepo(conn)
                    return repo.update_item(
                        str(tid),
                        content=str(content),
                        fts_text=prepare_fts_text(str(content)),
                        content_hash=content_hash(str(content)),
                    )

                return self.db.run_write("memory", _w)

            def make_retract(op: dict[str, Any]) -> Any:
                tid = op.get("target_id")
                if not tid:
                    return None

                def _w(conn: Any) -> Any:
                    return MemoryRepo(conn).update_item(str(tid), status="retracted", promoted=False)

                return self.db.run_write("memory", _w)

            def make_reinforce(op: dict[str, Any], turn_by_id: dict[str, dict[str, Any]]) -> Any:
                tid = op.get("target_id")
                if not tid:
                    return None

                def _w(conn: Any) -> Any:
                    repo = MemoryRepo(conn)
                    item = repo.get(str(tid))
                    if not item:
                        return None
                    previous = [
                        dict(row)
                        for row in TurnsRepo(self.db.read_turns()).fetch_by_ids(
                            [ev["turn_id"] for ev in repo.list_evidence(str(tid))]
                        )
                    ]
                    incoming = [
                        turn_by_id[eid]
                        for eid in (op.get("evidence_turns") or [])
                        if eid in turn_by_id
                    ]
                    count = evidence_span(previous + incoming)
                    status, promoted = promote_decision(
                        item["origin"],
                        count,
                        int(self.cfg["consolidation"]["promote_repeat"]),
                        item["type"],
                        stable=self._slot_stable(item.get("slot_key")),
                    )
                    return repo.update_item(
                        str(tid),
                        evidence_count=count,
                        status=status,
                        promoted=promoted,
                        last_reinforced_at=utc_now(),
                    )

                return self.db.run_write("memory", _w)

            known_ids = MemoryRepo(self.db.read_memory()).known_item_ids()
            applied = apply_ops(
                ops,
                turns=batch,
                remember_fn=make_remember,
                update_fn=make_update,
                retract_fn=make_retract,
                reinforce_fn=make_reinforce,
                tombstone_hashes=tomb,
                screen_extra=list(self.cfg["privacy"].get("screen_patterns") or []),
                known_ids=known_ids,
                slots=load_slots(self.root),
            )
            total_ops += sum(1 for a in applied if a.get("ok"))
            self.db.run_write(
                "turns",
                lambda c, ids=[t["id"] for t in batch]: TurnsRepo(c).mark_extracted(ids),
            )

        if summary:

            def _sum(conn: Any) -> None:
                MemoryRepo(conn).set_summary(session_id, str(summary)[:300])

            self.db.run_write("memory", _sum)
        return {"status": "ok", "ops": total_ops, "summary": summary}

    def _note_negation(self, session_id: str, content: str) -> None:
        if not any(phrase in content for phrase in _NEGATION_PHRASES):
            return

        def _w(conn: Any) -> None:
            repo = MemoryRepo(conn)
            item_id = repo.latest_ledger_item(session_id)
            if not item_id:
                return
            repo.record_change(
                item_id=item_id,
                op="HARM",
                actor="rule",
                reason="user_negation",
            )

        self.db.run_write("memory", _w)

    def tick_residency(self) -> dict[str, str]:
        available, total = read_memory_gb()
        action, reason = residency_from_config(
            self.cfg, available_gb=available, total_gb=total
        )
        mode = str((self.cfg.get("embedding") or {}).get("resident") or "auto").lower()
        idle_min = float((self.cfg.get("embedding") or {}).get("idle_unload_minutes") or 10)
        idle = self._last_embed_mono <= 0 or (time.monotonic() - self._last_embed_mono) >= idle_min * 60
        if action == "pin":
            self.embedder.available()
        elif action == "unload" or (
            action == "on_demand" and mode in {"false", "0", "no"} and idle and self.embedder.loaded()
        ):
            self.embedder.unload()
            if action != "unload":
                reason = "resident=false：空闲后已卸载"
        self.db.run_write(
            "memory", lambda c, reason=reason: MemoryRepo(c).meta_set("residency_reason", reason)
        )
        return {"action": action, "reason": reason}

    def doctor(self) -> dict[str, Any]:
        from thememory import __version__
        from thememory.paths import bin_dir

        repo = MemoryRepo(self.db.read_memory())
        turns = TurnsRepo(self.db.read_turns())
        raw_cap = (self.cfg.get("turns") or {}).get("max_size_gb")
        max_gb = 5.0 if raw_cap is None else float(raw_cap)
        used = turns.used_bytes()
        mem_ver = self.db.read_memory().execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        turns_ver = self.db.read_turns().execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        budget = (self.cfg.get("llm") or {}).get("budget") or {}
        daily = int(budget.get("daily_tokens") or 0)
        day_start = datetime.now(UTC).strftime("%Y-%m-%dT00:00:00.000Z")
        used_tokens = repo.usage_tokens_since(day_start)
        ext = (self.cfg.get("llm") or {}).get("extraction") or {}
        available_gb, total_gb = read_memory_gb()
        action, reason = residency_from_config(
            self.cfg, available_gb=available_gb, total_gb=total_gb
        )
        recent = self.db.read_memory().execute(
            """
            SELECT id, content, source_host, source_session, flags, origin, status
            FROM memory_items ORDER BY seq DESC LIMIT 5
            """
        ).fetchall()
        last_call = self.db.read_memory().execute(
            """
            SELECT ts, purpose, model, ok
            FROM usage_log ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        runtime = bin_dir(self.root) / "runtime.json"
        return {
            "version": __version__,
            "embedder_available": self.embedder.files_present(),
            "embedder_loaded": self.embedder.loaded(),
            "embedder_error": self.embedder.load_error,
            "residency": {"action": action, "reason": reason},
            "llm_available": self.llm.available(),
            "extraction_disclosure": (
                "提取会把对话发往所配置的 LLM 服务；记忆不跨设备、不做云同步。"
                if ext.get("base_url") and ext.get("model")
                else "未配置提取 LLM，对话不会外发。记忆不跨设备、不做云同步。"
            ),
            "llm": {
                "base_url": ext.get("base_url") or "",
                "model": ext.get("model") or "",
                "daily_tokens": daily,
                "daily_used": used_tokens,
                "daily_remaining": max(0, daily - used_tokens) if daily else None,
                "last_call": dict(last_call) if last_call else None,
            },
            "paused": self.is_paused(),
            "pause_until": repo.meta_get("pause_until"),
            "schema": {
                "memory": int(mem_ver["v"] or 0),
                "turns": int(turns_ver["v"] or 0),
            },
            "turns_bytes": used,
            "turns_max_bytes": int(max_gb * (1024**3)),
            "turns_usage_ratio": (used / (max_gb * (1024**3))) if max_gb else 0,
            "latest_backup": latest_backup_stamp(self.root),
            "runtime_json": runtime.exists(),
            "runtime": runtime_status(self.root),
            "daemon_started_at": repo.meta_get("daemon_started_at"),
            "uptime_s": _uptime_seconds(repo.meta_get("daemon_started_at")),
            "config_error": self._config_error,
            "adapter_capabilities": host_capabilities(),
            "harm_signals": repo.count_harm_signals(),
            "harm_recent": repo.recent_harm(5),
            "injections": repo.recent_injections(10),
            "config": explain_config(self.root),
            "last_probe": repo.meta_get("last_probe_json"),
            "degrades": repo.recent_degrades(10),
            "degrade_counts": repo.degrade_counts(),
            "recent_writes": [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "host": row["source_host"],
                    "session": row["source_session"],
                    "origin": row["origin"],
                    "status": row["status"],
                    "flags": row["flags"],
                    "highlight": _highlight_flags(row["flags"]),
                }
                for row in recent
            ],
            "pending_jobs": [
                dict(r)
                for r in self.db.read_memory()
                .execute(
                    """
                    SELECT id, kind, status, attempts, last_error, next_run_at
                    FROM jobs
                    WHERE status IN ('pending', 'failed')
                    ORDER BY id DESC LIMIT 20
                    """
                )
                .fetchall()
            ],
            "hosts": inspect_hosts(data_root=self.root),
            "autostart": autostart_registered(),
            "slots": sorted(load_slots(self.root).keys()),
        }

    def run_capability_probe(self) -> dict[str, Any]:
        from thememory.core.llm_probe import run_capability_probe

        result = run_capability_probe(self.llm)

        def _log(conn: Any) -> None:
            import json

            repo = MemoryRepo(conn)
            repo.meta_set("last_probe_json", json.dumps(result, ensure_ascii=False)[:8000])
            repo.conn.execute(
                """
                INSERT INTO usage_log(ts, purpose, model, prompt_tokens, completion_tokens, ok, latency_ms)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    utc_now(),
                    "probe",
                    self.llm.model or "",
                    None,
                    None,
                    1 if result.get("grade") == "pass" else 0,
                    None,
                ),
            )

        self.db.run_write("memory", _log)
        return result

    def _crosscheck(self, content: str, session_id: str) -> tuple[bool, float]:
        method = str(self.cfg["privacy"].get("crosscheck_method") or "semantic")
        min_sim = float(self.cfg["privacy"].get("crosscheck_min_similarity") or 0.55)
        min_ov = float(self.cfg["privacy"].get("crosscheck_min_overlap") or 0.25)
        turns = TurnsRepo(self.db.read_turns()).recent_user_turns(session_id, limit=5)
        if not turns:
            return True, 1.0
        if method == "semantic" and self.embedder.available():
            try:
                texts = [content] + [str(t.get("content") or "") for t in turns]
                vecs = self.embedder.embed(texts, is_query=False)
                import numpy as np

                q = np.asarray(vecs[0], dtype=np.float32)
                best = 0.0
                for v in vecs[1:]:
                    best = max(best, float(np.dot(q, np.asarray(v, dtype=np.float32))))
                return best >= min_sim, best
            except Exception:  # noqa: BLE001
                method = "lexical"
        # lexical bigram overlap
        def bigrams(s: str) -> set[str]:
            s = normalize_text(s)
            return {s[i : i + 2] for i in range(max(0, len(s) - 1))} or ({s} if s else set())

        c = bigrams(content)
        if not c:
            return True, 1.0
        best = 0.0
        for t in turns:
            other = bigrams(str(t.get("content") or ""))
            if not other:
                continue
            best = max(best, len(c & other) / len(c))
        return best >= min_ov, best

    def crosscheck_overlap(self, content: str, session_id: str) -> float:
        _, score = self._crosscheck(content, session_id)
        return score

    def list_memory(self, params: dict[str, Any]) -> dict[str, Any]:
        items = MemoryRepo(self.db.read_memory()).list_items(
            type_=params.get("type"),
            scope=params.get("scope"),
            status=params.get("status"),
            since=params.get("since"),
            limit=int(params.get("limit") or 50),
        )
        for item in items:
            item["highlight"] = _highlight_flags(item.get("flags"))
        return {"status": "ok", "items": items}

    def show_memory(self, params: dict[str, Any]) -> dict[str, Any]:
        repo = MemoryRepo(self.db.read_memory())
        item = repo.get(str(params.get("id") or ""))
        if not item:
            return {"status": "failed", "reason": "op_invalid"}
        return {"status": "ok", "item": item, "evidence": repo.list_evidence(item["id"])}

    def _embed_query(self, text: str) -> list[float]:
        key = normalize_text(text)[:500]
        cached = self._query_vec_cache.get(key)
        if cached is not None:
            return cached
        vec = self.embedder.embed([text], is_query=True)[0]
        if len(self._query_vec_cache) >= 64:
            self._query_vec_cache.pop(next(iter(self._query_vec_cache)))
        self._query_vec_cache[key] = vec
        return vec

    def memory_log(self, params: dict[str, Any]) -> dict[str, Any]:
        rows = MemoryRepo(self.db.read_memory()).recent_changes(
            since=params.get("since"), limit=int(params.get("limit") or 50)
        )
        return {"status": "ok", "changes": rows}

    def search_turns(self, params: dict[str, Any]) -> dict[str, Any]:
        q = str(params.get("query") or "")
        if not q:
            return {"status": "failed", "reason": "op_invalid"}
        hits = TurnsRepo(self.db.read_turns()).search_fts(q, limit=int(params.get("limit") or 20))
        return {"status": "ok", "turns": hits}

    def purge(self, params: dict[str, Any]) -> dict[str, Any]:
        """Physical purge — CLI only; requires confirm token."""
        if params.get("confirm") != "PURGE":
            return {"status": "failed", "reason": "op_invalid", "detail": "confirm=PURGE required"}
        scope = params.get("scope") or "turns"
        if scope == "turns":

            def _p(conn: Any) -> int:
                cur = conn.execute(
                    "UPDATE turns SET content=NULL, evicted=1 WHERE content IS NOT NULL"
                )
                return int(cur.rowcount)

            n = self.db.run_write("turns", _p)
            return {"status": "ok", "purged_turns": n}
        return {"status": "failed", "reason": "op_invalid", "detail": "unknown scope"}

    def maybe_evict_turns(self) -> dict[str, Any]:
        raw_cap = (self.cfg.get("turns") or {}).get("max_size_gb")
        max_gb = 5.0 if raw_cap is None else float(raw_cap)
        target = int(max_gb * (1024**3) * 0.9)
        repo = TurnsRepo(self.db.read_turns())
        if repo.used_bytes() <= int(max_gb * (1024**3)):
            return {"status": "ok", "evicted": 0}

        def _e(conn: Any) -> int:
            return TurnsRepo(conn).evict_oldest(target_bytes=target)

        n = self.db.run_write("turns", _e)
        cap = int(max_gb * (1024**3))
        used = TurnsRepo(self.db.read_turns()).used_bytes()
        if used > cap:
            MemoryRepo(self.db.read_memory()).add_degrade(
                "turns",
                "turns_backlog",
                f"used={used}",
            )
        return {"status": "ok", "evicted": n, "still_over": used > cap}

    def run_near_dup_consolidate(self, *, force: bool = False) -> dict[str, Any]:
        """Near-dup merge. LLM proposes canonical text when available; else rules."""
        if not self.embedder.available():
            return {"status": "degraded", "reason": "vector_unavailable", "merged": 0}
        cons = self.cfg.get("consolidation") or {}
        cooldown_h = float(cons.get("cooldown_hours") or 0)
        min_new = int(cons.get("min_new_items") or 0)
        meta = MemoryRepo(self.db.read_memory())
        last = meta.meta_get("last_consolidate_at")
        if not force and last and cooldown_h > 0:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if datetime.now(UTC) - last_dt < timedelta(hours=cooldown_h):
                    return {"status": "ok", "merged": 0, "skipped": "cooldown"}
            except ValueError:
                pass
        if not force and min_new > 0 and last:
            newer = meta.conn.execute(
                "SELECT COUNT(*) AS c FROM memory_items WHERE created_at > ? AND status='active'",
                (last,),
            ).fetchone()
            if int(newer["c"]) < min_new:
                return {"status": "ok", "merged": 0, "skipped": "min_new_items"}
        tau = float(cons.get("dedupe_similarity") or 0.92)
        repo = MemoryRepo(self.db.read_memory())
        active = repo.list_active(limit=500)
        by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in active:
            key = (item.get("type") or "", item.get("scope") or "", item.get("observer") or "")
            by_key.setdefault(key, []).append(item)
        merged = 0
        import numpy as np

        for group in by_key.values():
            if len(group) < 2:
                continue
            ids = [i["id"] for i in group]
            vecs: dict[str, np.ndarray] = {}
            for iid, blob, dim in repo.all_embeddings(self.embedder.model_id):
                if iid in ids:
                    vecs[iid] = np.asarray(unpack_vector(blob, dim), dtype=np.float32)
            for i, a in enumerate(group):
                va = vecs.get(a["id"])
                if va is None:
                    continue
                for b in group[i + 1 :]:
                    vb = vecs.get(b["id"])
                    if vb is None or va.shape != vb.shape:
                        continue
                    if float(np.dot(va, vb)) < tau:
                        continue
                    # keep higher seq; user_requested is never discarded as the loser
                    winner, loser = (a, b) if int(a["seq"]) >= int(b["seq"]) else (b, a)
                    if loser.get("origin") == "user_requested" and winner.get("origin") != "user_requested":
                        winner, loser = loser, winner
                    merged_content, actor = self._propose_merge(winner, loser)
                    if merged_content is None:
                        continue

                    def _merge(
                        conn: Any,
                        winner_id: str = winner["id"],
                        loser_id: str = loser["id"],
                        content: str = merged_content,
                        actor: str = actor,
                    ) -> None:
                        r = MemoryRepo(conn)
                        if content and content != winner.get("content"):
                            r.update_item(winner_id, content=content)
                        r.update_item(
                            loser_id,
                            status="superseded",
                            promoted=False,
                            superseded_by=winner_id,
                        )
                        r.record_change(
                            item_id=loser_id,
                            op="MERGE",
                            actor=actor,
                            reason="near_dup",
                            result={"winner": winner_id, "content": content},
                        )

                    self.db.run_write("memory", _merge)
                    merged += 1
        self.db.run_write(
            "memory",
            lambda c: MemoryRepo(c).meta_set("last_consolidate_at", utc_now()),
        )
        return {"status": "ok", "merged": merged, "actor": "llm" if self.llm.available() else "rule"}

    def _propose_merge(
        self, winner: dict[str, Any], loser: dict[str, Any]
    ) -> tuple[str | None, str]:
        """Return (canonical content, actor). None content means skip this pair."""
        if not self.llm.available():
            return str(winner.get("content") or ""), "rule"
        prompt = (
            "判断下面两条记忆是否应合并为一条陈述式记忆。"
            "user_requested 条目不得丢弃。只输出 JSON："
            '{"merge": true|false, "content": "合并后的一句话或空"}。\n'
            f"A id={winner.get('id')} origin={winner.get('origin')}: {winner.get('content')}\n"
            f"B id={loser.get('id')} origin={loser.get('origin')}: {loser.get('content')}"
        )
        try:
            data = self.llm.complete_json(
                "你是记忆整合器。只输出 JSON 对象。",
                prompt,
            )
        except Exception:  # noqa: BLE001
            return str(winner.get("content") or ""), "rule"
        if not isinstance(data, dict) or not data.get("merge"):
            return None, "llm"
        content = str(data.get("content") or winner.get("content") or "").strip()
        if not content:
            return None, "llm"
        return content[:300], "llm"


def os_environ_incognito() -> bool:
    import os

    return os.environ.get("THEMEMORY_INCOGNITO", "").strip() in {"1", "true", "yes"}
