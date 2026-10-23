"""Daemon entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from thememory.config import load, write_default
from thememory.core.service import MemoryService
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
    _setup_logging(root)
    cfg = load(root)
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


def _setup_logging(root: Path) -> None:
    log_path = logs_dir(root) / "thememoryd.log"
    handler = RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stderr)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
