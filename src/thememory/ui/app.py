"""Loopback HTTP UI for editing TheMemory config."""

from __future__ import annotations

import contextlib
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from thememory.client.ipc_client import IpcClient, IpcError
from thememory.config import load, write_config, write_default
from thememory.config.loader import ConfigError, resolve_api_key, set_path, validate
from thememory.outbound.llm_openai import list_models, probe_llm
from thememory.paths import ensure_layout, home_dir

PAGE_PATH = Path(__file__).with_name("page.html")
PROVIDERS_PATH = Path(__file__).with_name("providers.json")
LLM_API_KEY_ENV = "THEMEMORY_LLM_API_KEY"


def load_providers() -> dict[str, Any]:
    return json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))


def _resolve_request_api_key(
    *,
    api_key: str | None,
    root: Path | None,
) -> str:
    if api_key:
        return api_key
    cfg = load(root)
    ext = dict(cfg.get("llm", {}).get("extraction", {}))
    ext["api_key_env"] = LLM_API_KEY_ENV
    return resolve_api_key(ext)


def fetch_models(
    *,
    base_url: str,
    api_key: str,
    api: str | None = None,
    catalog: list[str] | None = None,
    timeout_s: float = 30.0,
) -> list[str]:
    """Catalog first. api=none never calls the network (spec: use a gateway)."""
    known = [str(name) for name in (catalog or []) if str(name).strip()]
    if api == "none":
        if not known:
            raise RuntimeError("此提供方没有 OpenAI/Anthropic 协议，请改用兼容网关或直接填写模型名")
        return known
    try:
        live = list_models(
            base_url=base_url,
            api_key=api_key,
            api=api,
            timeout_s=timeout_s,
        )
    except Exception:
        if known:
            return known
        raise
    merged: list[str] = []
    for name in known + live:
        if name not in merged:
            merged.append(name)
    return merged


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    ext = cfg.get("llm", {}).get("extraction", {})
    return {
        "llm": {
            "base_url": ext.get("base_url") or "",
            "model": ext.get("model") or "",
            "api_key_set": bool(ext.get("api_key")),
        },
        "extraction": {
            "trigger_turns": int(cfg.get("extraction", {}).get("trigger_turns") or 80),
            "trigger_idle_hours": float(cfg.get("extraction", {}).get("trigger_idle_hours") or 3),
            "window_tokens": int(cfg.get("extraction", {}).get("window_tokens") or 6000),
        },
        "embedding": {
            "model": cfg.get("embedding", {}).get("model") or "",
            "resident": str(cfg.get("embedding", {}).get("resident") or "auto"),
            "query_instruction": cfg.get("embedding", {}).get("query_instruction") or "",
            "resident_min_total_gb": float(cfg.get("embedding", {}).get("resident_min_total_gb") or 4),
        },
        "privacy": {
            "redact_secrets": bool(cfg.get("privacy", {}).get("redact_secrets", True)),
            "screen_enabled": bool(cfg.get("privacy", {}).get("screen_enabled", True)),
            "crosscheck_enabled": bool(cfg.get("privacy", {}).get("crosscheck_enabled", True)),
        },
        "isolation": {
            "profile": str(cfg.get("isolation", {}).get("profile") or "shared"),
            "procedural": str(cfg.get("isolation", {}).get("procedural") or "shared"),
        },
        "backup": {
            "interval_hours": int(cfg.get("backup", {}).get("interval_hours") or 24),
            "keep": int(cfg.get("backup", {}).get("keep") or 7),
        },
        "disclosures": {
            "extraction": "提取会把对话发往所配置的 LLM 服务。",
            "sync": "记忆仅保存在本机，不跨设备、不做云同步；定期备份也只在本机。",
        },
    }


def _apply_updates(cfg: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    llm = body.get("llm") or {}
    if "base_url" in llm:
        set_path(cfg, "llm.extraction.base_url", str(llm.get("base_url") or "").strip().rstrip("/"))
    if "model" in llm:
        set_path(cfg, "llm.extraction.model", str(llm.get("model") or ""))
    set_path(cfg, "llm.extraction.api_key_env", LLM_API_KEY_ENV)
    # empty api_key means keep existing
    if "api_key" in llm and str(llm.get("api_key") or "") != "":
        set_path(cfg, "llm.extraction.api_key", str(llm["api_key"]))

    extraction = body.get("extraction") or {}
    if "trigger_turns" in extraction:
        turns = int(extraction["trigger_turns"])
        if turns < 1:
            raise ConfigError("trigger_turns must be >= 1")
        set_path(cfg, "extraction.trigger_turns", turns)
    if "trigger_idle_hours" in extraction:
        hours = float(extraction["trigger_idle_hours"])
        if hours < 0:
            raise ConfigError("trigger_idle_hours must be >= 0")
        set_path(cfg, "extraction.trigger_idle_hours", hours)

    embedding = body.get("embedding") or {}
    if "resident" in embedding:
        resident = str(embedding["resident"])
        if resident not in {"auto", "true", "false"}:
            raise ConfigError("resident must be auto|true|false")
        set_path(cfg, "embedding.resident", resident)
    if "query_instruction" in embedding:
        set_path(cfg, "embedding.query_instruction", str(embedding.get("query_instruction") or ""))
    if "resident_min_total_gb" in embedding:
        set_path(cfg, "embedding.resident_min_total_gb", float(embedding["resident_min_total_gb"]))

    if "window_tokens" in extraction:
        set_path(cfg, "extraction.window_tokens", int(extraction["window_tokens"]))

    privacy = body.get("privacy") or {}
    for key in ("redact_secrets", "screen_enabled", "crosscheck_enabled"):
        if key in privacy:
            set_path(cfg, f"privacy.{key}", bool(privacy[key]))

    isolation = body.get("isolation") or {}
    for key in ("profile", "procedural"):
        if key in isolation:
            mode = str(isolation[key])
            if mode not in {"shared", "isolated"}:
                raise ConfigError(f"isolation.{key} must be shared|isolated")
            set_path(cfg, f"isolation.{key}", mode)

    backup = body.get("backup") or {}
    if "interval_hours" in backup:
        set_path(cfg, "backup.interval_hours", int(backup["interval_hours"]))
    if "keep" in backup:
        set_path(cfg, "backup.keep", int(backup["keep"]))

    validate(cfg)
    return cfg


# Cold start loads the ONNX embedder. On Windows a closed IPC port also takes
# about 2s to refuse, so a 2s socket timeout is reported as "timed out".
_DAEMON_IPC_TIMEOUT_S = 20.0


def _daemon_client(root: Path | None) -> IpcClient:
    return IpcClient(root, timeout=_DAEMON_IPC_TIMEOUT_S, autostart=True)


def _reload_daemon(root: Path | None) -> str:
    try:
        with _daemon_client(root) as client:
            client.request("admin.reload", {})
        return "已保存，守护进程已热重载。"
    except (IpcError, OSError, FileNotFoundError) as exc:
        return f"已保存。守护进程未就绪（{exc}）。请执行 thememory daemon start。"


def _doctor(root: Path | None) -> dict[str, Any]:
    try:
        with _daemon_client(root) as client:
            result = client.request("admin.doctor", {})
        result["daemon_running"] = True
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "daemon_running": False,
            "error": str(exc),
            "hint": "保存配置时会尝试启动守护进程；若仍失败，请执行 thememory daemon start",
        }


def _probe_llm(cfg: dict[str, Any]) -> dict[str, Any]:
    ext = cfg.get("llm", {}).get("extraction", {})
    return probe_llm(
        base_url=str(ext.get("base_url") or ""),
        model=str(ext.get("model") or ""),
        api_key=resolve_api_key(ext),
        api=str(ext.get("api") or "") or None,
        timeout_s=30,
    )


def make_handler(root: Path | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(
            self,
            code: int,
            payload: dict[str, Any] | bytes,
            content_type: str = "application/json",
        ) -> None:
            if isinstance(payload, dict):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                raw = payload
            self.send_response(code)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("body must be object")
            return data

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path in {"/", "/index.html"}:
                    self._send(200, PAGE_PATH.read_bytes(), "text/html")
                    return
                if path == "/api/config":
                    ensure_layout(root)
                    write_default(root)
                    cfg = load(root)
                    self._send(200, _public_config(cfg))
                    return
                if path == "/api/doctor":
                    self._send(200, _doctor(root))
                    return
                if path == "/api/providers":
                    self._send(200, load_providers())
                    return
                self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/api/config":
                    body = self._read_json()
                    ensure_layout(root)
                    write_default(root)
                    cfg = load(root)
                    _apply_updates(cfg, body)
                    write_config(cfg, root)
                    msg = _reload_daemon(root)
                    self._send(
                        200,
                        {
                            "ok": True,
                            "message": msg,
                            "config": _public_config(load(root)),
                        },
                    )
                    return
                if path == "/api/probe":
                    cfg = load(root)
                    result = _probe_llm(cfg)
                    self._send(200, result)
                    return
                if path == "/api/models":
                    body = self._read_json()
                    base_url = str(body.get("base_url") or "").strip()
                    key = _resolve_request_api_key(
                        api_key=str(body.get("api_key") or "") or None,
                        root=root,
                    )
                    raw_catalog = body.get("catalog") or []
                    if isinstance(raw_catalog, list):
                        catalog = [str(name) for name in raw_catalog]
                    else:
                        catalog = []
                    models = fetch_models(
                        base_url=base_url,
                        api_key=key,
                        api=str(body.get("api") or "") or None,
                        catalog=catalog,
                    )
                    self._send(
                        200,
                        {
                            "ok": True,
                            "models": models,
                            "count": len(models),
                        },
                    )
                    return
                self._send(404, {"error": "not found"})
            except ConfigError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send(400, {"error": str(exc)})

    return Handler


def run_ui(*, port: int = 8765, open_browser: bool = True, root: Path | None = None) -> int:
    ensure_layout(root or home_dir())
    write_default(root)
    handler = make_handler(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"TheMemory UI: {url}")
    print("按 Ctrl+C 退出")
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0
