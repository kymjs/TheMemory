"""Cursor hook client — stdlib only. Installed to ~/.cursor/hooks/thememory.py."""

from __future__ import annotations

CURSOR_HOOK_SOURCE = r'''#!/usr/bin/env python3
"""TheMemory Cursor hook — stdlib only. Generated; do not edit."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("THEMEMORY_HOME", Path.home() / ".thememory"))
ENDPOINT = HOME / "run" / "endpoint.json"
SOCK = HOME / "run" / "thememory.sock"
HOST = "cursor"


def _connect():
    if sys.platform == "win32":
        data = json.loads(ENDPOINT.read_text(encoding="utf-8"))
        sock = socket.create_connection(
            (data.get("host", "127.0.0.1"), int(data["port"])), timeout=2.0
        )
        return sock, data.get("token")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    sock.connect(str(SOCK))
    return sock, None


def _request(method, params):
    try:
        sock, token = _connect()
    except Exception:
        return None
    try:
        if token:
            params = dict(params)
            params["token"] = token
        req = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
        sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        msg = json.loads(buf.decode("utf-8").split("\n", 1)[0])
        if "error" in msg:
            return None
        return msg.get("result")
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _read_stdin():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def _base_params(payload):
    session = (
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("conversation_id")
        or "unknown"
    )
    return {
        "host": HOST,
        "session_ref": str(session),
        "project": payload.get("cwd") or payload.get("workspace_roots") or payload.get("project"),
        "tz": payload.get("tz"),
    }


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--event", required=True)
    args = p.parse_args()
    payload = _read_stdin()
    params = _base_params(payload)
    try:
        if args.event == "sessionStart":
            _request("capture.event", {**params, "event": "session_start"})
            res = _request("inject.session_start", params) or {}
            block = (res.get("block") or "").strip()
            if block:
                _out({"additional_context": block})
            else:
                _out({})
        elif args.event == "beforeSubmitPrompt":
            msg = payload.get("prompt") or payload.get("user_message") or ""
            if msg:
                _request(
                    "capture.turn",
                    {
                        **params,
                        "role": "user",
                        "content": str(msg)[:200000],
                        "turn_id": payload.get("turn_id") or str(time.time_ns()),
                    },
                )
                _request("inject.per_turn", {**params, "user_message": str(msg)[:8000]})
            _out({"continue": True})
        elif args.event == "afterAgentResponse":
            content = payload.get("text") or payload.get("last_assistant_message") or ""
            if content:
                _request(
                    "capture.turn",
                    {
                        **params,
                        "role": "assistant",
                        "content": str(content)[:200000],
                        "turn_id": payload.get("turn_id") or str(time.time_ns()),
                    },
                )
            _out({})
        elif args.event == "preCompact":
            _request("inject.after_compact", params)
            _out({})
        elif args.event == "sessionEnd":
            _request("capture.event", {**params, "event": "session_end"})
            _out({})
        elif args.event == "postToolUse":
            tool = (
                payload.get("tool_name")
                or payload.get("toolName")
                or payload.get("name")
                or ""
            )
            if tool:
                _request(
                    "capture.event",
                    {**params, "event": "tool_risk", "tool": str(tool)},
                )
            _out({})
        elif args.event == "stop":
            _out({})
        else:
            _out({})
    except Exception:
        if args.event == "beforeSubmitPrompt":
            _out({"continue": True})
        else:
            _out({})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
