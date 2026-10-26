"""Daemon entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from thememory.config import load, write_default
from thememory.core.redaction import redact_secrets
from thememory.runtime.service import MemoryService
from thememory.daemon.server import DaemonServer
from thememory.installer.model import ensure_model
from thememory.paths import ensure_layout, home_dir, logs_dir
from thememory.storage.db import Database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thememoryd")
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.home or home_dir()
    ensure_layout(root)
    write_default(root)
    cfg = load(root)
    level = str((cfg.get("daemon") or {}).get("log_level") or "info")
    _setup_logging(root, level)
    ensure_model(root)
    db = Database(root)
    service = MemoryService(db, cfg, root=root)
    # warm embedder
    if cfg.get("embedding", {}).get("resident") in {"true", "auto", True}:
        service.embedder.available()
    server = DaemonServer(service, root=root)
    if not server.acquire_lock():
        print("thememoryd already running", file=sys.stderr)
        return 1
    server.start_listen()
    logging.getLogger(__name__).info("thememoryd ready under %s", root)
    try:
        while not server._stop.is_set():  # noqa: SLF001
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        db.close()
    return 0


class SecretLogFilter(logging.Filter):
    """Never write secrets, and collapse long bodies to a length (spec §13.5)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = redact_secrets(record.getMessage())
        if len(msg) > 800 and "Traceback" not in msg:
            msg = f"log body omitted len={len(record.getMessage())}"
        record.msg = msg
        record.args = ()
        return True


def _setup_logging(root: Path, level_name: str = "info") -> None:
    log_path = logs_dir(root) / "thememoryd.log"
    handler = RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    log_filter = SecretLogFilter()
    handler.addFilter(log_filter)
    stream = logging.StreamHandler(sys.stderr)
    stream.addFilter(log_filter)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler, stream],
        force=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
