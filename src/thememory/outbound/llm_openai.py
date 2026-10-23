"""OpenAI Chat Completions + Anthropic Messages clients (auto-detected)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

ApiStyle = Literal["openai", "anthropic"]
ANTHROPIC_VERSION = "2023-06-01"


def detect_api_style(base_url: str, *, hint: str | None = None) -> ApiStyle:
    """Choose wire protocol from explicit hint or base_url shape."""
    if hint in {"openai", "anthropic"}:
        return hint  # type: ignore[return-value]
    u = (base_url or "").strip().lower().rstrip("/")
    if not u:
        return "openai"
    if "api.anthropic.com" in u or u.endswith("/anthropic") or "/anthropic/" in u:
        return "anthropic"
    return "openai"


def _anthropic_headers(api_key: str) -> dict[str, str]:
    # Official Anthropic uses x-api-key; MiniMax / gateways often accept Bearer too.
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if api_key:
        headers["x-api-key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _openai_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _anthropic_text(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
    elif isinstance(blocks, str):
        parts.append(blocks)
    return "".join(parts).strip()


def _parse_json_content(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        data = json.loads(m.group(0))
        if isinstance(data, dict):
            return data
    raise ValueError("llm did not return JSON object")


def list_models(
    *,
    base_url: str,
    api_key: str,
    api: str | None = None,
    timeout_s: float = 30.0,
) -> list[str]:
    """List models via OpenAI GET /models or Anthropic GET /v1/models."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("base_url 为空")
    if "{" in base:
        raise RuntimeError("base_url 含占位符，请先改成完整地址再刷新模型列表")
    style = detect_api_style(base, hint=api)
    if style == "anthropic":
        url = f"{base}/v1/models"
        headers = _anthropic_headers(api_key)
    else:
        url = f"{base}/models"
        headers = _openai_headers(api_key)
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError("响应不是模型列表")
    models: list[str] = []
    for item in items:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
    return sorted(set(models))


def probe_llm(
    *,
    base_url: str,
    model: str,
    api_key: str,
    api: str | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Tiny ping against the detected protocol."""
    base = (base_url or "").strip().rstrip("/")
    if not (base and model and api_key):
        raise RuntimeError("LLM 未配置完整（需要 base_url、model、api_key）")
    style = detect_api_style(base, hint=api)
    with httpx.Client(timeout=timeout_s) as client:
        if style == "anthropic":
            resp = client.post(
                f"{base}/v1/messages",
                headers=_anthropic_headers(api_key),
                json={
                    "model": model,
                    "max_tokens": 8,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        else:
            resp = client.post(
                f"{base}/chat/completions",
                headers=_openai_headers(api_key),
                json={
                    "model": model,
                    "temperature": 0,
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return {"ok": True, "message": f"探测成功（{style}, model={model}）", "api": style}


class OpenAICompatLLM:
    """Extraction LLM that speaks OpenAI or Anthropic based on base_url / hint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout_s: float = 120.0,
        json_mode: str = "auto",
        api: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.json_mode = json_mode
        self.api = detect_api_style(self.base_url, hint=api)

    def available(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        if not self.available():
            raise RuntimeError("llm not configured")
        try:
            if self.api == "anthropic":
                content = self._complete_anthropic(system, user)
            else:
                content = self._complete_openai(system, user)
        except Exception:
            logger.exception("llm request failed (%s)", self.api)
            raise
        return _parse_json_content(content)

    def _complete_openai(self, system: str, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.json_mode in {"on", "auto"}:
            body["response_format"] = {"type": "json_object"}
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(url, headers=_openai_headers(self.api_key), json=body)
            if resp.status_code >= 400 and self.json_mode == "auto":
                body.pop("response_format", None)
                resp = client.post(url, headers=_openai_headers(self.api_key), json=body)
            resp.raise_for_status()
            data = resp.json()
        return str(data["choices"][0]["message"]["content"] or "")

    def _complete_anthropic(self, system: str, user: str) -> str:
        url = f"{self.base_url}/v1/messages"
        sys_prompt = system
        if self.json_mode in {"on", "auto"}:
            sys_prompt = f"{system}\n\nRespond with a single JSON object only, no markdown."
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": sys_prompt,
            "messages": [{"role": "user", "content": user}],
        }
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(url, headers=_anthropic_headers(self.api_key), json=body)
            resp.raise_for_status()
            data = resp.json()
        return _anthropic_text(data)
