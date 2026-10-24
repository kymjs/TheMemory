"""NDJSON JSON-RPC helpers."""

from __future__ import annotations

import json
from typing import Any


def encode_msg(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode_line(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    data = json.loads(line)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def hello_handshake(
    params: dict[str, Any],
    *,
    server_version: str,
    server_protocol: int,
    state: str,
) -> dict[str, Any]:
    """Same-protocol version skew also asks the client to replace the process (spec §12.6)."""
    client_proto = int(params.get("protocol_version") or 0)
    client_version = str(params.get("client_version") or "")
    proto_mismatch = bool(client_proto) and client_proto != server_protocol
    version_mismatch = bool(client_version) and client_version != server_version
    restart = proto_mismatch or version_mismatch
    body: dict[str, Any] = {
        "ok": not restart,
        "server_version": server_version,
        "protocol_version": server_protocol,
        "state": state,
    }
    if restart:
        body["restart_required"] = True
    return body


def ok_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def err_result(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}
