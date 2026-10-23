"""Hermes MemoryProvider that talks to thememoryd via TheMemory's Python."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, spawn_context_thread

logger = logging.getLogger(__name__)

# Filled by TheMemory installer; falls back to PATH "thememory" / current interpreter.
THEMEMORY_PYTHON = r"__THEMEMORY_PYTHON__"
HOST = "hermes"


def _python() -> str:
    baked = THEMEMORY_PYTHON
    if baked and not baked.startswith("__") and os.path.isfile(baked):
        return baked
    return sys.executable


def _rpc(method: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> dict[str, Any]:
    payload = dict(params or {})
    payload.setdefault("host", HOST)
    code = (
        "import json\n"
        "from thememory.client.ipc_client import IpcClient\n"
        f"method = {method!r}\n"
        f"params = {payload!r}\n"
        "with IpcClient(timeout=15.0, autostart=True) as client:\n"
        "    print(json.dumps(client.request(method, params), ensure_ascii=False))\n"
    )
    env = os.environ.copy()
    proc = subprocess.run(
        [_python(), "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(err[:500])
    line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
    data = json.loads(line) if line else {}
    if not isinstance(data, dict):
        raise RuntimeError("invalid thememory response")
    return data


def _schema(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
        },
    }


def _p(description: str, type_: str = "string", **extra: Any) -> dict:
    return {"type": type_, "description": description, **extra}


_SCHEMAS = (
    _schema(
        "thememory_context",
        "Load TheMemory profile / long-term context for this user.",
        {"budget": _p("Token budget hint", "integer")},
    ),
    _schema(
        "thememory_search",
        "Search TheMemory for facts, preferences, or dated notes.",
        {"query": _p("What to search for."), "limit": _p("Max results", "integer")},
        ("query",),
    ),
    _schema(
        "thememory_remember",
        "Persist a fact or preference into TheMemory.",
        {
            "content": _p("The fact to remember."),
            "origin": _p(
                "user_requested if the user asked to remember; else agent_inferred.",
                enum=["user_requested", "agent_inferred"],
            ),
            "type": _p("Optional type, e.g. preference / fact / note."),
        },
        ("content", "origin"),
    ),
    _schema(
        "thememory_forget",
        "Forget a memory by id (preferred) or query.",
        {"id": _p("Memory id."), "query": _p("Lookup query if id unknown.")},
    ),
)


class TheMemoryProvider(MemoryProvider):
    """Bridge Hermes to the local TheMemory daemon."""

    def __init__(self) -> None:
        self._session_id = ""
        self._prefetch_cache = ""
        self._available: Optional[bool] = None

    @property
    def name(self) -> str:
        return "thememory"

    def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            _rpc("admin.status", {}, timeout=8.0)
            self._available = True
        except Exception as exc:
            logger.debug("thememory unavailable: %s", exc)
            self._available = False
        return self._available

    def unavailable_reason(self) -> str:
        return "thememoryd not reachable — run: thememory daemon start"

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or ""
        self._prefetch_cache = ""
        agent_context = str(kwargs.get("agent_context") or "primary")
        if agent_context != "primary":
            return
        try:
            _rpc(
                "capture.event",
                {
                    "event": "session_start",
                    "session_ref": self._session_id,
                    "host": HOST,
                },
                timeout=10.0,
            )
        except Exception as exc:
            logger.debug("thememory session_start capture skipped: %s", exc)

    def system_prompt_block(self) -> str:
        return (
            "# TheMemory\n"
            "Local long-term memory is active. Use thememory_context at session start if needed, "
            "thememory_search to recall, thememory_remember when the user asks to remember, "
            "thememory_forget to drop obsolete facts. Do not store secrets or one-off task noise."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        q = (query or "").strip()
        if len(q) < 4:
            return self._prefetch_cache
        try:
            data = _rpc(
                "tool.context",
                {"budget": 1200, "host": HOST},
                timeout=15.0,
            )
            text = str(data.get("block") or data.get("text") or "").strip()
            if text:
                self._prefetch_cache = f"## TheMemory\n{text}"
            return self._prefetch_cache
        except Exception as exc:
            logger.debug("thememory prefetch failed: %s", exc)
            return self._prefetch_cache

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        def _work() -> None:
            try:
                self.prefetch(query, session_id=session_id)
            except Exception as exc:
                logger.debug("thememory queue_prefetch failed: %s", exc)

        spawn_context_thread(_work, name="thememory-prefetch").start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        sid = session_id or self._session_id

        def _work() -> None:
            try:
                if user_content.strip():
                    _rpc(
                        "capture.turn",
                        {
                            "host": HOST,
                            "session_ref": sid,
                            "role": "user",
                            "content": user_content[:8000],
                        },
                        timeout=15.0,
                    )
                if assistant_content.strip():
                    _rpc(
                        "capture.turn",
                        {
                            "host": HOST,
                            "session_ref": sid,
                            "role": "assistant",
                            "content": assistant_content[:8000],
                        },
                        timeout=15.0,
                    )
            except Exception as exc:
                logger.debug("thememory sync_turn failed: %s", exc)

        spawn_context_thread(_work, name="thememory-sync").start()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == "thememory_context":
                data = _rpc(
                    "tool.context",
                    {"budget": args.get("budget"), "host": HOST},
                )
            elif tool_name == "thememory_search":
                data = _rpc(
                    "tool.search",
                    {
                        "query": args.get("query"),
                        "limit": args.get("limit") or 8,
                        "host": HOST,
                    },
                )
            elif tool_name == "thememory_remember":
                data = _rpc(
                    "tool.remember",
                    {
                        "content": args.get("content"),
                        "origin": args.get("origin") or "agent_inferred",
                        "type": args.get("type"),
                        "host": HOST,
                        "session_ref": self._session_id,
                    },
                )
            elif tool_name == "thememory_forget":
                data = _rpc(
                    "tool.forget",
                    {"id": args.get("id"), "query": args.get("query"), "host": HOST},
                )
            else:
                return json.dumps({"error": f"unknown tool {tool_name}"})
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def shutdown(self) -> None:
        return None


def register(ctx: Any) -> None:
    ctx.register_memory_provider(TheMemoryProvider())
