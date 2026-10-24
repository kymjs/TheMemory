from __future__ import annotations

from pathlib import Path

import pytest

from thememory.eval.runner import run_retrieval_eval


def test_retrieval_eval_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    result = run_retrieval_eval(root=tmp_path, k=3)
    assert result["status"] == "ok"
    assert result["n_queries"] >= 1
    # Soft assertion: with embeddings maybe unavailable, still returns metrics shape
    assert "recall_at_k" in result
    assert "details" in result
