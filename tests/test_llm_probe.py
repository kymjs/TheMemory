from __future__ import annotations

from thememory.core.llm_probe import FakeProbeLLM, run_capability_probe


def test_probe_pass() -> None:
    result = run_capability_probe(FakeProbeLLM("pass"))
    assert result["grade"] == "pass"
    assert result["status"] == "ok"


def test_probe_partial() -> None:
    result = run_capability_probe(FakeProbeLLM("partial"))
    assert result["grade"] in {"partial", "fail"}
    assert "relative_date" in result["failed"] or "anti_inject" in result["failed"]


def test_probe_json_fail() -> None:
    result = run_capability_probe(FakeProbeLLM("fail_json"))
    assert result["status"] == "failed"
    assert result["grade"] == "fail"
