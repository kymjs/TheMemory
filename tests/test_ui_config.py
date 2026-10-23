from __future__ import annotations

import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from thememory.config import load, write_default
from thememory.ui.app import (
    _apply_updates,
    _doctor,
    _public_config,
    _reload_daemon,
    fetch_models,
    load_providers,
    make_handler,
)


def test_apply_updates_masks_and_keeps_key(tmp_path: Path) -> None:
    write_default(tmp_path)
    cfg = load(tmp_path)
    cfg["llm"]["extraction"]["api_key"] = "secret-old"
    _apply_updates(
        cfg,
        {
            "llm": {
                "base_url": "https://example.com/v1",
                "model": "demo",
                "api_key": "",
            },
            "extraction": {"trigger_turns": 10, "trigger_idle_hours": 1},
            "embedding": {"resident": "false", "query_instruction": "q:"},
        },
    )
    assert cfg["llm"]["extraction"]["base_url"] == "https://example.com/v1"
    assert cfg["llm"]["extraction"]["api_key"] == "secret-old"
    assert cfg["llm"]["extraction"]["api_key_env"] == "THEMEMORY_LLM_API_KEY"
    pub = _public_config(cfg)
    assert pub["llm"]["api_key_set"] is True
    assert "api_key" not in pub["llm"]


def test_providers_catalog_has_deepseek() -> None:
    data = load_providers()
    assert data["providers"]
    ids = {p["id"] for p in data["providers"]}
    assert "deepseek" in ids
    assert "custom" in ids
    deepseek = next(p for p in data["providers"] if p["id"] == "deepseek")
    assert deepseek["base_url"].endswith("/v1")
    assert deepseek["api"] == "openai"
    minimax = next(p for p in data["providers"] if p["id"] == "minimax-cn")
    assert minimax["api"] == "anthropic"
    assert minimax["base_url"].endswith("/anthropic")
    anthropic = next(p for p in data["providers"] if p["id"] == "anthropic")
    assert anthropic["api"] == "anthropic"
    assert anthropic["base_url"] == "https://api.anthropic.com"


def test_fetch_models_rejects_placeholder() -> None:
    with pytest.raises(RuntimeError, match="占位符"):
        fetch_models(
            base_url="https://example.com/{ACCOUNT}/v1",
            api_key="x",
        )


def test_http_get_post_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMEMORY_HOME", str(tmp_path))
    monkeypatch.setattr(
        "thememory.ui.app._reload_daemon",
        lambda root: "已保存，守护进程已热重载。",
    )
    write_default(tmp_path)
    handler = make_handler(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/config")
        res = conn.getresponse()
        body = json.loads(res.read().decode("utf-8"))
        assert res.status == 200
        assert "llm" in body
        assert "api_key" not in body["llm"]

        conn.request("GET", "/api/providers")
        res_p = conn.getresponse()
        providers = json.loads(res_p.read().decode("utf-8"))
        assert res_p.status == 200
        assert len(providers["providers"]) >= 20

        payload = json.dumps(
            {
                "llm": {
                    "base_url": "https://api.example.com/v1",
                    "model": "m1",
                    "api_key": "sk-test",
                },
                "extraction": {"trigger_turns": 12, "trigger_idle_hours": 2},
                "embedding": {"resident": "auto", "query_instruction": "x"},
            }
        ).encode("utf-8")
        conn.request(
            "POST",
            "/api/config",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        res2 = conn.getresponse()
        body2 = json.loads(res2.read().decode("utf-8"))
        assert res2.status == 200
        assert body2["ok"] is True
        cfg = load(tmp_path)
        assert cfg["llm"]["extraction"]["model"] == "m1"
        assert cfg["llm"]["extraction"]["api_key_env"] == "THEMEMORY_LLM_API_KEY"
        assert cfg["extraction"]["trigger_turns"] == 12
        conn.close()
    finally:
        server.shutdown()


def test_doctor_autostarts_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[float, bool]] = []

    class FakeClient:
        def __init__(self, root: Path | None = None, timeout: float = 5.0, *, autostart: bool = True) -> None:
            assert root == tmp_path
            seen.append((timeout, autostart))

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def request(self, method: str, params: dict | None = None) -> dict:
            assert method == "admin.doctor"
            return {"llm_available": True}

    monkeypatch.setattr("thememory.ui.app.IpcClient", FakeClient)
    result = _doctor(tmp_path)
    assert seen == [(20.0, True)]
    assert result["daemon_running"] is True
    assert result["llm_available"] is True

    class DownClient(FakeClient):
        def request(self, method: str, params: dict | None = None) -> dict:
            raise OSError("timed out")

    monkeypatch.setattr("thememory.ui.app.IpcClient", DownClient)
    assert "timed out" in _reload_daemon(tmp_path)
