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

from thememory import PROTOCOL_VERSION, __version__
from thememory.core.service import MemoryService
from thememory.daemon.ipc.protocol import (
    decode_line,
    encode_msg,
    err_result,
    hello_handshake,
    ok_result,
)
from thememory.daemon.scheduler import DaemonScheduler
from thememory.paths import endpoint_path, lock_path, pid_path, run_dir, sock_path

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], dict[str, Any]]


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

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            f = conn.makefile("rwb")
            while not self._stop.is_set():
                line = f.readline()
                if not line:
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
        svc = self.service
        if method == "hello":
            return hello_handshake(
                params,
                server_version=__version__,
                server_protocol=PROTOCOL_VERSION,
                state=self._state,
            )
        if method == "tool.remember":
            return svc.remember(params)
        if method == "tool.search":
            return svc.search(params)
        if method == "tool.context":
            return svc.context(params)
        if method == "tool.forget":
            return svc.forget(params)
        if method == "tool.correct":
            return svc.correct(params)
        if method == "capture.turn":
            return svc.capture_turn(params)
        if method == "capture.event":
            return svc.capture_event(params)
        if method == "inject.session_start":
            return svc.inject_session_start(params)
        if method == "inject.per_turn":
            return svc.inject_per_turn(params)
        if method == "inject.after_compact":
            return svc.inject_after_compact(params)
        if method == "admin.status":
            return {
                "version": __version__,
                "state": self._state,
                "embedder": svc.embedder.available(),
                "llm": svc.llm.available(),
            }
        if method == "admin.doctor":
            return svc.doctor()
        if method == "admin.probe":
            return svc.run_capability_probe()
        if method == "admin.benchmark":
            from thememory.installer.benchmark import run_benchmark

            return run_benchmark(self.root, write=bool(params.get("write", True)))
        if method == "admin.import_host":
            from pathlib import Path

            from thememory.installer.host_import import import_host_memories

            pr = params.get("project_root")
            return import_host_memories(
                host=str(params.get("host") or "claude-code"),
                project_root=Path(pr) if pr else None,
                root=self.root,
                dry_run=bool(params.get("dry_run")),
                service=svc,
            )
        if method == "admin.eval_retrieval":
            from pathlib import Path

            from thememory.eval.runner import run_retrieval_eval

            fix = params.get("fixture")
            # Eval uses an isolated temp root to avoid polluting live DB
            import tempfile

            tmp = Path(tempfile.mkdtemp(prefix="thememory-eval-"))
            return run_retrieval_eval(
                fixture=Path(fix) if fix else None,
                root=tmp,
                k=int(params.get("k") or 3),
            )
        if method == "admin.extract_now":
            sid = params.get("session_id")
            if not sid and params.get("session_ref"):
                sess = svc.ensure_session(params)
                sid = sess["id"]
            if not sid:
                raise ValueError("session_id required")
            return svc.run_extract(str(sid))
        if method == "admin.shutdown":
            if params.get("drain"):
                try:
                    svc.process_jobs(limit=20)
                except Exception:  # noqa: BLE001
                    logger.exception("drain jobs before shutdown")
            return {"ok": True, "drain": bool(params.get("drain"))}
        if method == "admin.reload":
            from thememory.config import load

            svc.reload_config(load(self.root))
            return {"ok": True}
        if method == "admin.backup":
            return svc.run_db_backup()
        if method == "admin.pause":
            until = params.get("until") or "forever"
            return svc.set_pause(until=str(until))
        if method == "admin.resume":
            return svc.set_pause(until=None)
        if method == "admin.purge":
            return svc.purge(params)
        if method == "admin.memory_list":
            return svc.list_memory(params)
        if method == "admin.memory_show":
            return svc.show_memory(params)
        if method == "admin.memory_log":
            return svc.memory_log(params)
        if method == "admin.turns_search":
            return svc.search_turns(params)
        if method == "admin.maintenance":
            return {
                "maintenance": svc.run_maintenance(),
                "evict": svc.maybe_evict_turns(),
            }
        raise KeyError(method)


def os_getpid() -> int:
    import os

    return os.getpid()
