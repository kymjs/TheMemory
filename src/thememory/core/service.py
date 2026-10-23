"""Domain service orchestrating remember/search/capture/inject/extract."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from thememory.config.loader import resolve_api_key
from thememory.core.extraction import (
    SYSTEM_PROMPT,
    apply_ops,
    build_user_prompt,
    item_from_add_op,
    promote_decision,
)
from thememory.core.redaction import clean_turn, screen_directive
from thememory.core.retrieval import rrf_fuse, vector_search
from thememory.core.snapshot import render_snapshot
from thememory.outbound.embed_onnx import MODEL_DIR_NAME, OnnxEmbedder
from thememory.outbound.llm_openai import OpenAICompatLLM
from thememory.paths import models_dir
from thememory.storage.db import Database
from thememory.storage.repos.memory import MemoryRepo, pack_vector
from thememory.storage.repos.turns import TurnsRepo
from thememory.util import content_hash, new_id, normalize_text, prepare_fts_text, utc_now

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, db: Database, cfg: dict[str, Any], root: Path | None = None) -> None:
        self.db = db
        self.cfg = cfg
        self.root = root
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

    def reload_config(self, cfg: dict[str, Any]) -> None:
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

    # --- tools ---

    def remember(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        policy = self.host_policy(host)
        if not policy.get("write", True):
            return {"status": "failed", "reason": "policy_denied"}
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
            scope = params.get("scope")
            if not scope:
                scope = "project:local" if type_ == "procedural" else "user"
            item = repo.insert_item(
                {
                    "observer": observer,
                    "scope": scope,
                    "type": type_,
                    "slot_key": params.get("slot_key"),
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
                    "source_session": params.get("session_ref"),
                    "content_hash": chash,
                }
            )
            # slot supersede
            if item.get("slot_key") and promoted:
                self._supersede_slot(repo, item)
            repo.record_change(item_id=item["id"], op="ADD", actor="agent", result=item)
            repo.enqueue_job("embed", {"item_id": item["id"]})
            effect = "active_promoted" if promoted else ("held" if hold else "candidate")
            return {"status": "ok", "effect": effect, "id": item["id"], "item": item}

        result = self.db.run_write("memory", _write)
        if result.get("effect") not in {"duplicate", "suppressed"} and result.get("id"):
            self._embed_now(result["id"])
        return {**result, "degraded_paths": []}

    def _supersede_slot(self, repo: MemoryRepo, item: dict[str, Any]) -> None:
        rows = repo.conn.execute(
            """
            SELECT id FROM memory_items
            WHERE slot_key=? AND observer=? AND status='active' AND id!=? AND promoted=1
            """,
            (item["slot_key"], item["observer"], item["id"]),
        ).fetchall()
        for row in rows:
            repo.update_item(row["id"], status="superseded", promoted=False, superseded_by=item["id"])
            repo.record_change(
                item_id=row["id"], op="SUPERSEDE", actor="rule", reason="slot_replace"
            )

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
        degraded: list[str] = []
        repo = MemoryRepo(self.db.read_memory())
        fts_hits: list[tuple[dict[str, Any], float]] = []
        vec_hits: list[tuple[dict[str, Any], float]] = []
        if query:
            try:
                fts_hits = repo.search_fts(str(query), limit=limit * 2)
            except Exception as exc:  # noqa: BLE001
                degraded.append("fts")
                repo.add_degrade("fts", "fts_unavailable", str(exc), host=host)
        if query and self.embedder.available():
            try:
                qv = self.embedder.embed([str(query)], is_query=True)[0]
                vec_hits = vector_search(
                    repo,
                    qv,
                    model_id=self.embedder.model_id,
                    tau=float(self.cfg["retrieval"]["tau_search_cosine"]),
                    limit=limit * 2,
                )
            except Exception as exc:  # noqa: BLE001
                degraded.append("vector")
                MemoryRepo(self.db.read_memory()).add_degrade(
                    "vector", "vector_unavailable", str(exc), host=host
                )
        elif query:
            degraded.append("vector")

        items = rrf_fuse(
            fts_hits,
            vec_hits,
            k=int(self.cfg["retrieval"]["rrf_k"]),
            limit=limit,
        )
        # date filter
        if date or date_range:
            start = end = None
            if date:
                start = end = str(date)
            if date_range and isinstance(date_range, (list, tuple)) and len(date_range) == 2:
                start, end = str(date_range[0]), str(date_range[1])
            filtered = []
            for item in items:
                vf, vt = item.get("valid_from"), item.get("valid_to")
                if vf and vt and start and end:
                    if vt < start or vf > end:
                        continue
                    item = dict(item)
                    item["matched_by"] = "date" if not query else item.get("matched_by", "date")
                filtered.append(item)
            if not items and (date or date_range):
                # direct date scan
                rows = repo.list_active(types=["event", "plan"], limit=100)
                for item in rows:
                    vf, vt = item.get("valid_from"), item.get("valid_to")
                    if vf and vt and start and end and not (vt < start or vf > end):
                        item = dict(item)
                        item["matched_by"] = "date"
                        filtered.append(item)
            items = filtered[:limit]

        types = params.get("types")
        if types:
            items = [i for i in items if i.get("type") in types]
        if not params.get("include_candidates"):
            items = [i for i in items if i.get("status") == "active"]

        out_items = []
        for item in items:
            out_items.append(
                {
                    "id": item["id"],
                    "content": item["content"],
                    "type": item["type"],
                    "scope": item["scope"],
                    "trust": "high"
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
        return {"status": status, "items": out_items, "degraded_paths": degraded}

    def context(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params.get("host") or "cli"
        policy = self.host_policy(host)
        if not policy.get("read", True):
            return {"status": "failed", "reason": "policy_denied"}
        repo = MemoryRepo(self.db.read_memory())
        items = repo.list_promoted(
            observer=host, isolated=bool(policy.get("isolated")), limit=200
        )
        snap_cfg = self.cfg["snapshot"]
        block, digest = render_snapshot(
            items,
            budgets={k: int(v) for k, v in snap_cfg.items() if k.startswith("budget_")},
            annotated=bool(snap_cfg.get("annotated", True)),
        )
        return {
            "status": "ok",
            "block": block,
            "snapshot_hash": digest,
            "item_count": len(items),
            "degraded_paths": [],
        }

    # --- capture / inject ---

    def ensure_session(self, params: dict[str, Any]) -> dict[str, Any]:
        host = params["host"]
        host_session = params.get("session_ref") or new_id()

        def _write(conn: Any) -> dict[str, Any]:
            repo = MemoryRepo(conn)
            return repo.upsert_session(
                {
                    "host": host,
                    "host_session_id": host_session,
                    "project_key": params.get("project"),
                    "tz": params.get("tz"),
                }
            )

        return self.db.run_write("memory", _write)

    def capture_event(self, params: dict[str, Any]) -> dict[str, Any]:
        event = params.get("event")
        session = self.ensure_session(params)
        if event == "session_end":

            def _end(conn: Any) -> None:
                MemoryRepo(conn).end_session(session["id"])

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
        session = self.ensure_session(params)
        content = clean_turn(
            str(params.get("content") or ""),
            redact=bool(self.cfg["privacy"]["redact_secrets"]),
            private=bool(self.cfg["privacy"]["private_tag"]),
            extra=list(self.cfg["privacy"].get("extra_patterns") or []),
        )
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
        repo = MemoryRepo(self.db.read_memory())
        items = repo.list_promoted(
            observer=host, isolated=bool(policy.get("isolated")), limit=200
        )
        turns_repo = TurnsRepo(self.db.read_turns())
        unextracted = turns_repo.unextracted_preview(
            session["id"], limit=int(self.cfg["snapshot"]["unextracted_max_turns"])
        )
        snap_cfg = self.cfg["snapshot"]
        block, digest = render_snapshot(
            items,
            summary=session.get("summary"),
            unextracted=unextracted,
            budgets={k: int(v) for k, v in snap_cfg.items() if k.startswith("budget_")},
            annotated=bool(snap_cfg.get("annotated", True)),
        )

        def _save(conn: Any) -> None:
            r = MemoryRepo(conn)
            r.save_snapshot(session["id"], block, digest, r.max_seq())
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
        if len(msg) < 2:
            return {"status": "ok", "block": None, "item_count": 0, "skipped_reason": "short"}
        session = self.ensure_session(params)
        search_res = self.search(
            {
                "host": host,
                "query": msg,
                "limit": int(self.cfg["retrieval"]["inject_max_items"]),
            }
        )
        items = search_res.get("items") or []
        # filter by inject cosine conceptually already in search; ledger dedupe
        repo = MemoryRepo(self.db.read_memory())
        fresh = []
        for it in items:
            if not repo.ledger_has(session["id"], it["id"]):
                fresh.append(it)
        if not fresh:
            return {
                "status": search_res.get("status", "ok"),
                "block": None,
                "item_count": 0,
                "skipped_reason": "ledger_dedupe",
                "degraded_paths": search_res.get("degraded_paths") or [],
            }
        max_chars = int(self.cfg["retrieval"]["inject_max_chars"])
        lines = ["[TheMemory 相关记忆]"]
        used = 0
        kept = []
        for it in fresh:
            line = f"- {it['content']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            kept.append(it)
            used += len(line)

        def _ledger(conn: Any) -> None:
            r = MemoryRepo(conn)
            for it in kept:
                r.ledger_add(session["id"], it["id"], "per_turn")

        self.db.run_write("memory", _ledger)
        block = "\n".join(lines) if kept else None
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
                    self.run_extract(job["payload"]["session_id"])
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

    def run_extract(self, session_id: str) -> dict[str, Any]:
        if not self.llm.available():
            MemoryRepo(self.db.read_memory()).add_degrade(
                "llm", "llm_unreachable", "not configured", session_id=session_id
            )
            return {"status": "failed", "reason": "llm_unreachable"}
        session = MemoryRepo(self.db.read_memory()).get_session(session_id)
        if not session:
            return {"status": "failed", "reason": "op_invalid"}
        turns = TurnsRepo(self.db.read_turns()).pending_for_session(
            session_id, after_seq=int(session.get("extract_cursor") or 0)
        )
        if not turns:
            return {"status": "ok", "ops": 0}
        # window split by approximate chars
        window = int(self.cfg["extraction"]["window_tokens"]) * 4
        batches: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        size = 0
        for t in turns:
            c = len(str(t.get("content") or ""))
            if cur and size + c > window:
                batches.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += c
        if cur:
            batches.append(cur)
        total_ops = 0
        summary = None
        host = session["host"]
        for batch in batches:
            existing = MemoryRepo(self.db.read_memory()).list_active(limit=40)
            user_prompt = build_user_prompt(batch, existing)
            try:
                data = self.llm.complete_json(SYSTEM_PROMPT, user_prompt)
            except Exception as exc:  # noqa: BLE001
                MemoryRepo(self.db.read_memory()).add_degrade(
                    "extraction", "llm_parse_failed", str(exc), host=host, session_id=session_id
                )
                return {"status": "failed", "reason": "llm_parse_failed", "detail": str(exc)}
            summary = data.get("session_summary") or summary
            ops = data.get("ops") or []
            tomb = MemoryRepo(self.db.read_memory()).tombstone_hashes()

            def make_remember(op: dict[str, Any], turn_by_id: dict[str, dict[str, Any]]) -> Any:
                item = item_from_add_op(
                    op, host=host, observer=host, session_id=session_id, now=utc_now()
                )
                # evidence count R3
                user_ev = sum(
                    1
                    for eid in (op.get("evidence_turns") or [])
                    if turn_by_id.get(eid, {}).get("role") == "user"
                )
                item["evidence_count"] = user_ev
                status, promoted = promote_decision(
                    item["origin"],
                    user_ev,
                    int(self.cfg["consolidation"]["promote_repeat"]),
                    item["type"],
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
                    if created.get("slot_key") and created.get("promoted"):
                        self._supersede_slot(repo, created)
                    for eid in op.get("evidence_turns") or []:
                        turn = turn_by_id[eid]
                        repo.conn.execute(
                            """
                            INSERT OR IGNORE INTO item_evidence(item_id, turn_id, role, quote)
                            VALUES (?,?,?,?)
                            """,
                            (created["id"], eid, turn["role"], op.get("quote")),
                        )
                    repo.record_change(item_id=created["id"], op="ADD", actor="llm", proposal=op)
                    repo.enqueue_job("embed", {"item_id": created["id"]})
                    return created

                created = self.db.run_write("memory", _w)
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
                    user_ev = sum(
                        1
                        for eid in (op.get("evidence_turns") or [])
                        if turn_by_id.get(eid, {}).get("role") == "user"
                    )
                    count = int(item.get("evidence_count") or 0) + user_ev
                    status, promoted = promote_decision(
                        item["origin"],
                        count,
                        int(self.cfg["consolidation"]["promote_repeat"]),
                        item["type"],
                    )
                    return repo.update_item(
                        str(tid),
                        evidence_count=count,
                        status=status,
                        promoted=promoted,
                        last_reinforced_at=utc_now(),
                    )

                return self.db.run_write("memory", _w)

            applied = apply_ops(
                ops,
                turns=batch,
                remember_fn=make_remember,
                update_fn=make_update,
                retract_fn=make_retract,
                reinforce_fn=make_reinforce,
                tombstone_hashes=tomb,
                screen_extra=list(self.cfg["privacy"].get("screen_patterns") or []),
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

    def doctor(self) -> dict[str, Any]:
        repo = MemoryRepo(self.db.read_memory())
        return {
            "embedder_available": self.embedder.available(),
            "embedder_error": self.embedder.load_error,
            "llm_available": self.llm.available(),
            "degrades": repo.recent_degrades(10),
            "pending_jobs": [
                dict(r)
                for r in self.db.read_memory()
                .execute("SELECT id, kind, status FROM jobs WHERE status='pending' LIMIT 20")
                .fetchall()
            ],
        }

    def crosscheck_overlap(self, content: str, session_id: str) -> float:
        turns = TurnsRepo(self.db.read_turns()).recent_user_turns(session_id, limit=3)
        if not turns:
            return 1.0
        c = set(normalize_text(content))
        best = 0.0
        for t in turns:
            other = set(normalize_text(str(t.get("content") or "")))
            if not c or not other:
                continue
            best = max(best, len(c & other) / max(len(c), 1))
        return best
