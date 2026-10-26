"""Async / threaded JSON-RPC server for thememoryd."""

from __future__ import annotations

import json
import logging
import secrets
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from thememory.daemon.handlers import dispatch as dispatch_method
from thememory.daemon.ipc.protocol import decode_line, encode_msg, err_result, ok_result
from thememory.daemon.scheduler import DaemonScheduler
from thememory.paths import endpoint_path, lock_path, pid_path, run_dir, sock_path
from thememory.runtime.service import MemoryService

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], dict[str, Any]]
_DEFAULT_MAX_REQUEST = 8_388_608


class DaemonServer:
    def __init__(self, service: MemoryService, root: Path | None = None) -> None:
        self.service = service
        self.root = root
        self._token = secrets.token_hex(16)
        self._stop = threading.Event()
        self._server_sock: socket.socket | None = None
        self._state = "initializing"
        self._lock_fh: Any = None
        self._scheduler = DaemonScheduler(service, root=root, stop_event=self._stop)

    def acquire_lock(self) -> bool:
        run_dir(self.root).mkdir(parents=True, exist_ok=True)
        path = lock_path(self.root)
        self._lock_fh = open(path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._lock_fh.seek(0)
                msvcrt.locking(self._lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_fh.close()
            self._lock_fh = None
            return False
        self._lock_fh.seek(0)
        self._lock_fh.truncate()
        self._lock_fh.write(str(os_getpid()))
        self._lock_fh.flush()
        pid_path(self.root).write_text(str(os_getpid()), encoding="utf-8")
        return True

    def release_lock(self) -> None:
        if self._lock_fh:
            try:
                self._lock_fh.close()
            except OSError:
                pass
            self._lock_fh = None

    def start_listen(self) -> None:
        if sys.platform == "win32":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            endpoint_path(self.root).write_text(
                json.dumps({"host": "127.0.0.1", "port": port, "token": self._token}),
                encoding="utf-8",
            )
        else:
            sp = sock_path(self.root)
            if sp.exists():
                sp.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(sp))
            try:
                import os

                os.chmod(sp, 0o600)
            except OSError:
                pass
        sock.listen(32)
        sock.settimeout(0.5)
        self._server_sock = sock
        self._state = "ready"
        from thememory.storage.repos.memory import MemoryRepo
        from thememory.util import utc_now

        MemoryRepo(self.service.db.read_memory()).meta_set("daemon_started_at", utc_now())
        threading.Thread(target=self._accept_loop, name="thememory-accept", daemon=True).start()
        self._scheduler.start()
        # Run maintenance once at startup (spec §8.4 / §13.2)
        try:
            self.service.recover_jobs()
            self.service.run_maintenance()
            self.service.consume_inbox()
        except Exception:  # noqa: BLE001
            logger.exception("startup maintenance failed")

    def stop(self) -> None:
        self._stop.set()
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        self.release_lock()

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _max_request_bytes(self) -> int:
        try:
            return int(self.service.cfg.get("daemon", {}).get("max_request_bytes") or _DEFAULT_MAX_REQUEST)
        except (TypeError, ValueError):
            return _DEFAULT_MAX_REQUEST

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            f = conn.makefile("rwb")
            while not self._stop.is_set():
                line = f.readline()
                if not line:
                    break
                if len(line) > self._max_request_bytes():
                    f.write(
                        encode_msg(
                            err_result(None, 1003, "payload_too_large", {"limit": self._max_request_bytes()})
                        )
                    )
                    f.flush()
                    break
                try:
                    msg = decode_line(line)
                except Exception:  # noqa: BLE001
                    f.write(encode_msg(err_result(None, -32700, "parse error")))
                    f.flush()
                    continue
                req_id = msg.get("id")
                method = msg.get("method")
                params = msg.get("params") or {}
                if not isinstance(params, dict):
                    f.write(encode_msg(err_result(req_id, -32602, "invalid params")))
                    f.flush()
                    continue
                if sys.platform == "win32" and method != "hello":
                    if params.get("token") != self._token:
                        f.write(encode_msg(err_result(req_id, 1002, "unauthorized")))
                        f.flush()
                        continue
                try:
                    result = self.dispatch(str(method), params)
                    f.write(encode_msg(ok_result(req_id, result)))
                except KeyError:
                    f.write(encode_msg(err_result(req_id, -32601, f"method not found: {method}")))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("handler error")
                    f.write(encode_msg(err_result(req_id, -32000, str(exc))))
                f.flush()
                if method == "admin.shutdown":
                    self._stop.set()
                    break
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return dispatch_method(
            method,
            params,
            service=self.service,
            state=self._state,
            root=self.root,
            on_shutdown=lambda: self._stop.set(),
        )


def os_getpid() -> int:
    import os

    return os.getpid()
