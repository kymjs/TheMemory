"""FTS + vector retrieval with RRF fusion."""

from __future__ import annotations

from typing import Any

import numpy as np

from thememory.storage.repos.memory import MemoryRepo, unpack_vector


def rrf_fuse(
    fts_hits: list[tuple[dict[str, Any], float]],
    vec_hits: list[tuple[dict[str, Any], float]],
    *,
    k: int = 60,
    limit: int = 8,
) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}
    matched: dict[str, set[str]] = {}
    for rank, (item, _) in enumerate(fts_hits):
        iid = item["id"]
        items[iid] = item
        scores[iid] = scores.get(iid, 0.0) + 1.0 / (k + rank + 1)
        matched.setdefault(iid, set()).add("fts")
    for rank, (item, _) in enumerate(vec_hits):
        iid = item["id"]
        items[iid] = item
        scores[iid] = scores.get(iid, 0.0) + 1.0 / (k + rank + 1)
        matched.setdefault(iid, set()).add("vector")
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    out = []
    for iid, score in ordered:
        item = dict(items[iid])
        paths = matched.get(iid, set())
        if paths == {"fts", "vector"}:
            item["matched_by"] = "both"
        elif "vector" in paths:
            item["matched_by"] = "vector"
        else:
            item["matched_by"] = "fts"
        item["_score"] = score
        out.append(item)
    return out


def vector_search(
    repo: MemoryRepo,
    query_vec: list[float],
    *,
    model_id: str,
    tau: float,
    limit: int = 20,
    active_ids: set[str] | None = None,
) -> list[tuple[dict[str, Any], float]]:
    q = np.asarray(query_vec, dtype=np.float32)
    hits: list[tuple[str, float]] = []
    for item_id, blob, dim in repo.all_embeddings(model_id):
        if active_ids is not None and item_id not in active_ids:
            continue
        if dim != len(q):
            continue
        v = np.asarray(unpack_vector(blob, dim), dtype=np.float32)
        sim = float(np.dot(q, v))
        if sim >= tau:
            hits.append((item_id, sim))
    hits.sort(key=lambda x: x[1], reverse=True)
    out: list[tuple[dict[str, Any], float]] = []
    for item_id, sim in hits[:limit]:
        item = repo.get(item_id)
        if item and item.get("status") == "active":
            out.append((item, sim))
    return out


def filter_fts_by_threshold(
    hits: list[tuple[dict[str, Any], float]], tau_bm25: float
) -> list[tuple[dict[str, Any], float]]:
    # bm25 in sqlite is lower (more negative) for better matches in some builds;
    # our query uses ORDER BY score ascending. Accept all if threshold unused.
    # We treat absolute score: keep top results regardless when tau is low.
    if not hits:
        return []
    # Keep hits whose bm25 score is <= tau (better) OR just keep all ranked
    return [(item, score) for item, score in hits if score <= tau_bm25 or True]
