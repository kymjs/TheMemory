"""Internal daemon scheduler (spec §13.2)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from thememory.config import load
from thememory.paths import config_path

if TYPE_CHECKING:
    from thememory.runtime.service import MemoryService

logger = logging.getLogger(__name__)


class DaemonScheduler:
    """Periodic tasks: jobs, inbox, idle extract, config reload, backup, maintenance."""

    def __init__(
        self,
        service: MemoryService,
        root: Path | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.service = service
        self.root = root
        self._stop = stop_event or threading.Event()
        self._thread: threading.Thread | None = None
        self._config_mtime: float | None = None
        self._last_extract_eval = 0.0
        self._last_inbox = 0.0
        self._last_backup_check = 0.0
        self._last_maintenance = 0.0
        self._last_config_poll = 0.0
        self._last_residency = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        path = config_path(self.root)
        try:
            self._config_mtime = path.stat().st_mtime if path.exists() else None
        except OSError:
            self._config_mtime = None
        self._thread = threading.Thread(
            target=self._loop, name="thememory-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                self.service.process_jobs(limit=5)
            except Exception:  # noqa: BLE001
                logger.exception("job loop error")

            if now - self._last_config_poll >= 2.0:
                self._last_config_poll = now
                self._maybe_reload_config()

            if now - self._last_inbox >= 30.0:
                self._last_inbox = now
                try:
                    self.service.consume_inbox()
                except Exception:  # noqa: BLE001
                    logger.exception("inbox consume error")

            if now - self._last_residency >= 60.0:
                self._last_residency = now
                try:
                    self.service.tick_residency()
                except Exception:  # noqa: BLE001
                    logger.exception("residency check error")

            if now - self._last_extract_eval >= 60.0:
                self._last_extract_eval = now
                try:
                    self.service.evaluate_idle_extract()
                except Exception:  # noqa: BLE001
                    logger.exception("idle extract eval error")

            if now - self._last_backup_check >= 300.0:
                self._last_backup_check = now
                try:
                    self.service.maybe_run_backup()
                except Exception:  # noqa: BLE001
                    logger.exception("backup check error")

            if now - self._last_maintenance >= 3600.0:
                self._last_maintenance = now
                try:
                    self.service.run_maintenance()
                    self.service.maybe_evict_turns()
                    self.service.run_near_dup_consolidate()
                except Exception:  # noqa: BLE001
                    logger.exception("maintenance error")

            self._stop.wait(1.0)

    def _maybe_reload_config(self) -> None:
        path = config_path(self.root)
        try:
            mtime = path.stat().st_mtime if path.exists() else None
        except OSError:
            return
        if mtime is None or mtime == self._config_mtime:
            return
        try:
            cfg = load(self.root)
            self.service.reload_config(cfg)
            self._config_mtime = mtime
            logger.info("config hot-reloaded from %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("config hot-reload failed; keeping previous config")
            self._config_mtime = mtime
            self.service.note_config_error(f"{type(exc).__name__}: {exc}")
