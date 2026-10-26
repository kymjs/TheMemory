"""FTS + vector retrieval with RRF fusion (pure; no storage imports)."""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from thememory.core.vectors import unpack_vector


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


def vector_search_matrix(
    *,
    ids: list[str],
    matrix: np.ndarray,
    query_vec: list[float],
    tau: float,
    limit: int = 20,
    active_ids: set[str] | None = None,
    resolve_item: Callable[[str], dict[str, Any] | None],
    statuses: set[str] | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Cosine/dot search over an in-memory L2-normalized matrix (spec §9.5)."""
    if matrix.size == 0 or not ids:
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    if q.ndim != 1 or matrix.shape[1] != q.shape[0]:
        return []
    sims = matrix @ q
    order = np.argsort(-sims)
    allowed = statuses or {"active"}
    out: list[tuple[dict[str, Any], float]] = []
    for idx in order:
        sim = float(sims[int(idx)])
        if sim < tau:
            break
        item_id = ids[int(idx)]
        if active_ids is not None and item_id not in active_ids:
            continue
        item = resolve_item(item_id)
        if item and item.get("status") in allowed:
            out.append((item, sim))
            if len(out) >= limit:
                break
    return out


def keyword_hit(item: dict[str, Any], query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return False
    blob = " ".join(
        str(part) for part in (item.get("keywords") or []) + (item.get("aliases") or [])
    ).casefold()
    return needle in blob


def filter_fts_by_threshold(
    hits: list[tuple[dict[str, Any], float]],
    tau_bm25: float,
    *,
    query: str = "",
) -> list[tuple[dict[str, Any], float]]:
    """Keep hits with -bm25() >= tau, or an explicit keyword/alias hit (spec §9.1)."""
    kept: list[tuple[dict[str, Any], float]] = []
    for item, score in hits:
        if keyword_hit(item, query) or (-float(score)) >= tau_bm25:
            kept.append((item, score))
    return kept


def scope_visible(item: dict[str, Any], project_scope: str | None) -> bool:
    scope = str(item.get("scope") or "user")
    if not scope.startswith("project:"):
        return True
    return bool(project_scope) and scope == project_scope


def tombstone_similar(
    vector: list[float],
    fingerprints: list[tuple[bytes, int]],
    *,
    tau: float,
) -> bool:
    if not vector or not fingerprints:
        return False
    query = np.asarray(vector, dtype=np.float32)
    qn = float(np.linalg.norm(query))
    if qn <= 0:
        return False
    for blob, dim in fingerprints:
        if dim != len(query):
            continue
        other = np.asarray(unpack_vector(blob, dim), dtype=np.float32)
        norm = float(np.linalg.norm(other))
        if norm <= 0:
            continue
        if float(np.dot(query, other) / (qn * norm)) >= tau:
            return True
    return False


def drop_near_duplicates(
    items: list[dict[str, Any]],
    vectors: dict[str, list[float]],
    *,
    tau: float = 0.92,
) -> list[dict[str, Any]]:
    """Within one result list, keep the higher-ranked item of a near-duplicate pair."""
    kept: list[dict[str, Any]] = []
    kept_vecs: list[np.ndarray] = []
    for item in items:
        raw = vectors.get(str(item.get("id")))
        if not raw:
            kept.append(item)
            kept_vecs.append(np.zeros(1, dtype=np.float32))
            continue
        vec = np.asarray(raw, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm <= 0:
            kept.append(item)
            kept_vecs.append(vec)
            continue
        unit = vec / norm
        duplicate = False
        for previous in kept_vecs:
            if previous.shape != unit.shape or float(np.linalg.norm(previous)) <= 0:
                continue
            prev = previous / float(np.linalg.norm(previous))
            if float(np.dot(unit, prev)) >= tau:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
            kept_vecs.append(unit)
    return kept


def trust_tier(item: dict[str, Any]) -> str:
    origin = item.get("origin")
    promoted = bool(item.get("promoted"))
    evidence = int(item.get("evidence_count") or 0)
    flags = item.get("flags") or []
    if flags or item.get("status") == "candidate" or origin == "agent_inferred":
        return "low"
    if origin in {"user_requested", "user_stated"} and promoted:
        return "high"
    if evidence >= 3 and promoted:
        return "high"
    if promoted or (origin == "user_stated" and item.get("status") == "active"):
        return "medium"
    return "low"


_HALF_LIFE_DAYS = {
    "profile_fact": 365,
    "preference": 180,
    "pattern": 90,
    "note": 60,
    "procedural": 365,
}


def recency_factor(item: dict[str, Any], today: str) -> float:
    """Spec §9.1. Events use the validity window; other types use a half-life."""
    type_ = str(item.get("type") or "note")
    vf = str(item.get("valid_from") or "")
    vt = str(item.get("valid_to") or "")
    if type_ in {"event", "plan"}:
        if vf and vt and vf <= today <= vt:
            return 1.3
        if vf and vf > today:
            return 1.1
        return 0.5
    half = _HALF_LIFE_DAYS.get(type_, 60)
    observed = str(item.get("observed_at") or "")[:10]
    age = 0
    try:
        from datetime import date

        age = max(0, (date.fromisoformat(today) - date.fromisoformat(observed)).days)
    except ValueError:
        age = 0
    return 0.6 + 0.4 * (0.5 ** (age / half))


def apply_deterministic_weights(
    items: list[dict[str, Any]],
    *,
    project_scope: str | None = None,
    today: str | None = None,
) -> list[dict[str, Any]]:
    """Re-rank by scope / trust / promoted / recency (spec §9.1)."""
    tier_w = {"high": 1.2, "medium": 1.0, "low": 0.8}
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        base = float(item.get("_score") or 1.0)
        trust = trust_tier(item)
        item = dict(item)
        item["trust"] = trust
        w = tier_w.get(trust, 1.0)
        if project_scope and item.get("scope") == project_scope:
            w *= 1.15
        if item.get("promoted"):
            w *= 1.1
        if today:
            w *= recency_factor(item, today)
        evidence = int(item.get("evidence_count") or 0)
        if evidence > 1:
            import math

            w *= min(1.3, 1 + 0.1 * math.log2(evidence))
        scored.append((base * w, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, item in scored:
        item["_score"] = score
        out.append(item)
    return out
