"""Sweep retrieval cosine threshold on the Chinese fixture and optionally persist it."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from thememory.config.loader import load, set_path, write_config
from thememory.eval.runner import run_retrieval_eval

DEFAULT_TAUS = (0.35, 0.45, 0.50, 0.55, 0.60, 0.70)


def _score(metrics: dict[str, Any]) -> float:
    recall = float(metrics.get("recall_at_k") or 0.0)
    empty = metrics.get("empty_accuracy")
    empty_s = float(empty) if empty is not None else 1.0
    return recall + 0.5 * empty_s


def calibrate_tau(
    *,
    root: Path | None = None,
    fixture: Path | None = None,
    k: int = 3,
    taus: tuple[float, ...] = DEFAULT_TAUS,
    write: bool = False,
) -> dict[str, Any]:
    """Pick tau_search_cosine maximizing recall + empty-result accuracy."""
    trials: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    scratch = Path(tempfile.mkdtemp(prefix="thememory-cal-"))
    for tau in taus:
        trial_root = scratch / f"tau-{str(tau).replace('.', '_')}"
        metrics = run_retrieval_eval(
            fixture=fixture,
            root=trial_root,
            k=k,
            tau_search_cosine=tau,
        )
        row = {
            "tau": tau,
            "score": round(_score(metrics), 4),
            "recall_at_k": metrics.get("recall_at_k"),
            "empty_accuracy": metrics.get("empty_accuracy"),
            "mrr": metrics.get("mrr"),
        }
        trials.append(row)
        if best is None or row["score"] > best["score"]:
            best = row
    assert best is not None
    result: dict[str, Any] = {
        "status": "ok",
        "best_tau": best["tau"],
        "best_score": best["score"],
        "trials": trials,
        "written": False,
    }
    if write and root is not None:
        cfg = load(root)
        set_path(cfg, "retrieval.tau_search_cosine", best["tau"])
        # Inject threshold stays slightly stricter than search (spec §9.1)
        set_path(cfg, "retrieval.tau_inject_cosine", min(0.9, round(float(best["tau"]) + 0.1, 2)))
        write_config(cfg, root)
        result["written"] = True
        result["config_root"] = str(root)
    return result
