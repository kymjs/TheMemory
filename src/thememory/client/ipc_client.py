"""IPC client for thememoryd (Unix socket or Windows TCP+token)."""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from thememory import PROTOCOL_VERSION, __version__
from thememory.daemon.ipc.protocol import decode_line, encode_msg
from thememory.daemon.spawn import spawn_daemon
from thememory.paths import endpoint_path, home_dir, sock_path


class IpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class IpcClient:
    def __init__(
        self,
        root: Path | None = None,
        timeout: float = 5.0,
        *,
        autostart: bool = True,
    ) -> None:
        self.root = root or home_dir()
        self.timeout = timeout
        self.autostart = autostart
        self._sock: socket.socket | None = None
        self._token: str | None = None
        self._file: Any = None

    def connect(self, *, autostart: bool | None = None) -> None:
        if autostart is None:
            autostart = self.autostart
        try:
            self._connect_once()
            return
        except OSError:
            if not autostart:
                raise
        self._try_start_daemon()
        deadline = time.time() + self.timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                self._connect_once()
                return
            except OSError as exc:
                last = exc
                time.sleep(0.1)
        raise IpcError(f"cannot connect to thememoryd: {last}")

    def _connect_once(self) -> None:
        if sys.platform == "win32":
            ep = endpoint_path(self.root)
            if not ep.exists():
                raise FileNotFoundError(ep)
            data = json.loads(ep.read_text(encoding="utf-8"))
            host = data.get("host", "127.0.0.1")
            port = int(data["port"])
            self._token = data.get("token")
            sock = socket.create_connection((host, port), timeout=self.timeout)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect(str(sock_path(self.root)))
        self._sock = sock
        self._file = sock.makefile("rwb")
        hello = self.request(
            "hello",
            {"client_version": __version__, "protocol_version": PROTOCOL_VERSION},
        )
        if hello.get("restart_required"):
            raise IpcError("restart_required", code=1001, data=hello)
        if hello.get("state") == "initializing":
            # still usable for degraded ops
            pass

    def _try_start_daemon(self) -> None:
        spawn_daemon(home=self.root)

    def close(self) -> None:
        try:
            if self._file:
                self._file.close()
        finally:
            if self._sock:
                self._sock.close()
        self._sock = None
        self._file = None

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._sock or not self._file:
            raise IpcError("not connected")
        params = dict(params or {})
        if self._token:
            params.setdefault("token", self._token)
        req_id = str(time.time_ns())
        self._file.write(encode_msg({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}))
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise IpcError("connection closed")
        msg = decode_line(line)
        if "error" in msg:
            err = msg["error"]
            raise IpcError(err.get("message", "error"), code=err.get("code"), data=err.get("data"))
        result = msg.get("result")
        if not isinstance(result, dict):
            raise IpcError("invalid result")
        return result

    def __enter__(self) -> IpcClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
