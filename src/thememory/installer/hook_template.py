"""Generate stdlib-only hook client into ~/.thememory/bin/hook.py."""

from __future__ import annotations

from pathlib import Path

HOOK_SOURCE = r'''#!/usr/bin/env python3
"""TheMemory hook client — stdlib only. Generated; do not edit."""
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


def _connect():
    if sys.platform == "win32":
        data = json.loads(ENDPOINT.read_text(encoding="utf-8"))
        sock = socket.create_connection((data.get("host", "127.0.0.1"), int(data["port"])), timeout=2.0)
        return sock, data.get("token")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    sock.connect(str(SOCK))
    return sock, None


def _request(method, params):
    sock, token = _connect()
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
    sock.close()
    msg = json.loads(buf.decode("utf-8").split("\n", 1)[0])
    if "error" in msg:
        return None
    return msg.get("result")


def _read_stdin():
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _out_context(event_name, text):
    if not text:
        print("{}")
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }, ensure_ascii=False))


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--event", required=True)
    args = p.parse_args()
    payload = _read_stdin()
    session = payload.get("session_id") or payload.get("sessionId") or "unknown"
    params = {
        "host": args.host,
        "session_ref": str(session),
        "project": payload.get("cwd") or payload.get("project"),
        "tz": payload.get("tz"),
    }
    try:
        if args.event in ("SessionStart", "session_start"):
            _request("capture.event", {**params, "event": "session_start"})
            res = _request("inject.session_start", params) or {}
            _out_context("SessionStart", res.get("block") or "")
        elif args.event in ("UserPromptSubmit", "user_prompt"):
            msg = payload.get("prompt") or payload.get("user_message") or ""
            _request("capture.turn", {
                **params,
                "role": "user",
                "content": msg,
                "turn_id": payload.get("turn_id") or str(time.time_ns()),
            })
            res = _request("inject.per_turn", {**params, "user_message": msg}) or {}
            _out_context("UserPromptSubmit", res.get("block") or "")
        elif args.event in ("Stop", "stop"):
            content = ""
            tpath = payload.get("transcript_path") or payload.get("transcriptPath")
            if tpath and Path(tpath).exists():
                try:
                    lines = Path(tpath).read_text(encoding="utf-8").splitlines()
                    for line in reversed(lines):
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("role") == "assistant" or obj.get("type") == "assistant":
                            content = obj.get("message", {}).get("content") if isinstance(obj.get("message"), dict) else obj.get("content") or ""
                            if isinstance(content, list):
                                content = "".join(
                                    (c.get("text") or "") if isinstance(c, dict) else str(c) for c in content
                                )
                            break
                except OSError:
                    content = ""
            if not content:
                content = payload.get("last_assistant_message") or ""
            if content:
                _request("capture.turn", {
                    **params,
                    "role": "assistant",
                    "content": str(content)[:200000],
                    "turn_id": payload.get("turn_id") or str(time.time_ns()),
                })
            print("{}")
        elif args.event in ("PreCompact", "compact"):
            res = _request("inject.after_compact", params) or {}
            _out_context("PreCompact", res.get("block") or "")
        elif args.event in ("SessionEnd", "session_end"):
            _request("capture.event", {**params, "event": "session_end"})
            print("{}")
        else:
            print("{}")
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_hook_script(root: Path | None = None) -> Path:
    from thememory.paths import bin_dir, ensure_layout

    ensure_layout(root)
    path = bin_dir(root) / "hook.py"
    path.write_text(HOOK_SOURCE, encoding="utf-8")
    try:
        import os

        os.chmod(path, 0o755)
    except OSError:
        pass
    return path
