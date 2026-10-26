"""Run retrieval metrics on fixture JSON (no live LLM)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from thememory.config import load, write_default
from thememory.runtime.service import MemoryService
from thememory.paths import ensure_layout
from thememory.storage.db import Database


def _default_fixture() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "retrieval_zh.json"


def _recall_at_k(ranked_ids: list[str], relevant: list[str], k: int) -> float:
    if not relevant:
        # "no relevant → empty" accuracy handled separately
        return 1.0 if not ranked_ids[:k] else 0.0
    hit = any(i in ranked_ids[:k] for i in relevant)
    return 1.0 if hit else 0.0


def _mrr(ranked_ids: list[str], relevant: list[str]) -> float:
    if not relevant:
        return 1.0 if not ranked_ids else 0.0
    for i, rid in enumerate(ranked_ids, start=1):
        if rid in relevant:
            return 1.0 / i
    return 0.0


def run_retrieval_eval(
    *,
    fixture: Path | None = None,
    root: Path | None = None,
    k: int = 3,
    tau_search_cosine: float | None = None,
) -> dict[str, Any]:
    path = fixture or _default_fixture()
    data = json.loads(path.read_text(encoding="utf-8"))
    ensure_layout(root)
    write_default(root)
    cfg = load(root)
    # Relax limits for eval seeding
    cfg["limits"] = {
        "session_max_writes": 1000,
        "session_max_procedural": 1000,
        "burst_window_seconds": 1,
        "burst_max_writes": 1000,
    }
    if tau_search_cosine is not None:
        cfg.setdefault("retrieval", {})["tau_search_cosine"] = tau_search_cosine
    db = Database(root)
    svc = MemoryService(db, cfg, root=root)
    id_map: dict[str, str] = {}
    for item in data.get("items") or []:
        res = svc.remember(
            {
                "host": "eval",
                "origin": "user_requested",
                "type": item.get("type") or "note",
                "content": item["content"],
                "session_ref": "eval",
            }
        )
        if res.get("id"):
            id_map[str(item["id"])] = str(res["id"])
            # promote for retrieval visibility
            from thememory.storage.repos.memory import MemoryRepo

            rid = res["id"]

            def _promo(conn: Any, rid: str = rid) -> None:
                MemoryRepo(conn).update_item(rid, status="active", promoted=True)

            db.run_write("memory", _promo)

    recalls: list[float] = []
    mrrs: list[float] = []
    empty_ok = 0
    empty_n = 0
    details: list[dict[str, Any]] = []
    for q in data.get("queries") or []:
        relevant_fix = [id_map[i] for i in (q.get("relevant") or []) if i in id_map]
        result = svc.search({"host": "eval", "query": q["query"], "limit": k})
        ranked = [str(it["id"]) for it in (result.get("items") or [])]
        if not (q.get("relevant") or []):
            empty_n += 1
            if not ranked:
                empty_ok += 1
            details.append(
                {
                    "query": q["query"],
                    "relevant": [],
                    "ranked": ranked,
                    "empty_correct": not ranked,
                }
            )
            continue
        r = _recall_at_k(ranked, relevant_fix, k)
        m = _mrr(ranked, relevant_fix)
        recalls.append(r)
        mrrs.append(m)
        details.append(
            {
                "query": q["query"],
                "relevant": relevant_fix,
                "ranked": ranked,
                "recall": r,
                "mrr": m,
            }
        )
    db.close()
    summary = {
        "status": "ok",
        "fixture": str(path),
        "recall_at_k": sum(recalls) / len(recalls) if recalls else None,
        "mrr": sum(mrrs) / len(mrrs) if mrrs else None,
        "empty_accuracy": (empty_ok / empty_n) if empty_n else None,
        "k": k,
        "n_queries": len(data.get("queries") or []),
        "details": details,
    }
    if root is not None:
        from thememory.eval.scores import append_score

        append_score(
            root,
            {
                "suite": "retrieval",
                "fixture": summary["fixture"],
                "recall_at_k": summary["recall_at_k"],
                "mrr": summary["mrr"],
                "empty_accuracy": summary["empty_accuracy"],
                "k": summary["k"],
                "n_queries": summary["n_queries"],
            },
        )
    return summary
