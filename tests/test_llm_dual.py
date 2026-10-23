"""OpenAI + Anthropic dual-protocol LLM helpers."""

from __future__ import annotations

import json

import httpx
import pytest

from thememory.outbound.llm_openai import OpenAICompatLLM, detect_api_style, list_models, probe_llm


def test_detect_api_style() -> None:
    assert detect_api_style("https://api.deepseek.com/v1") == "openai"
    assert detect_api_style("https://api.anthropic.com") == "anthropic"
    assert detect_api_style("https://api.minimaxi.com/anthropic") == "anthropic"
    assert detect_api_style("https://api.minimaxi.com/v1") == "openai"
    assert detect_api_style("https://api.minimaxi.com/anthropic", hint="openai") == "openai"


def test_complete_json_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"ops":[]}'}}]}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url: str, headers=None, json=None):  # noqa: A002
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    llm = OpenAICompatLLM(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        json_mode="off",
    )
    assert llm.api == "openai"
    assert llm.complete_json("sys", "user") == {"ops": []}
    assert calls == ["https://api.example.com/v1/chat/completions"]


def test_complete_json_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"content": [{"type": "text", "text": '{"ops":[],"session_summary":"x"}'}]}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url: str, headers=None, json=None):  # noqa: A002
            calls.append((url, headers or {}))
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    llm = OpenAICompatLLM(
        base_url="https://api.minimaxi.com/anthropic",
        model="MiniMax-M3.1-Flash",
        api_key="k",
        json_mode="auto",
    )
    assert llm.api == "anthropic"
    data = llm.complete_json("sys", "user")
    assert data["session_summary"] == "x"
    assert calls[0][0] == "https://api.minimaxi.com/anthropic/v1/messages"
    assert calls[0][1]["x-api-key"] == "k"
    assert calls[0][1]["anthropic-version"]


def test_list_models_anthropic_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"data": [{"id": "claude-sonnet"}]}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *a) -> None:
            return None

        def get(self, url: str, headers=None):
            assert url.endswith("/v1/models")
            return FakeResp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert list_models(base_url="https://api.anthropic.com", api_key="k") == ["claude-sonnet"]


def test_probe_rejects_incomplete() -> None:
    with pytest.raises(RuntimeError, match="未配置"):
        probe_llm(base_url="", model="m", api_key="k")
