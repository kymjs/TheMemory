"""Local performance probe written into config [latency] / embedding residency (spec §16.2 step 7)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from thememory.config.loader import load, set_path, write_config
from thememory.outbound.embed_onnx import MODEL_DIR_NAME, OnnxEmbedder
from thememory.paths import models_dir
from thememory.storage.backup import run_backup


def _mem_available_gb() -> float | None:
    try:
        import sys

        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return float(stat.ullAvailPhys) / (1024**3)
        # Linux
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb / (1024**2)
    except Exception:  # noqa: BLE001
        return None
    return None


def run_benchmark(root: Path | None = None, *, write: bool = True) -> dict[str, Any]:
    """Measure embed latency and rough model footprint; optionally persist to config."""
    result: dict[str, Any] = {"status": "ok"}
    model_dir = models_dir(root) / MODEL_DIR_NAME
    embedder = OnnxEmbedder(model_dir)
    t0 = time.perf_counter()
    available = embedder.available()
    load_ms = (time.perf_counter() - t0) * 1000
    result["embedder_available"] = available
    result["model_load_ms"] = round(load_ms, 1)
    if not available:
        result["status"] = "degraded"
        result["embedder_error"] = embedder.load_error
        return result

    samples = ["用户偏好简洁回答", "下周三去天津", "thememory search latency"]
    t1 = time.perf_counter()
    embedder.embed(samples, is_query=True)
    query_ms = (time.perf_counter() - t1) * 1000 / max(len(samples), 1)
    result["query_embed_ms"] = round(query_ms, 1)

    # Rough residency: use load time heuristic + available RAM
    avail = _mem_available_gb()
    result["mem_available_gb"] = None if avail is None else round(avail, 2)
    # Spec: high water ≈ footprint×4 ≥ 2GB — we don't measure RSS portably; estimate 0.5GB
    estimated_model_gb = 0.5
    high = max(2.0, estimated_model_gb * 4)
    low = estimated_model_gb * 2
    result["resident_high_gb"] = high
    result["resident_low_gb"] = low
    if avail is not None and avail >= high:
        recommended = "true"
    elif avail is not None and avail < low:
        recommended = "false"
    else:
        recommended = "auto"
    result["recommended_resident"] = recommended

    # Hook/IPC style budget hints from measured embed
    per_turn = max(80, int(query_ms * 2 + 40))
    search_ms = max(100, int(query_ms * 3 + 50))
    session_start = max(200, int(load_ms * 0.2 + query_ms * 4 + 80))
    result["latency"] = {
        "per_turn_ms": per_turn,
        "session_start_ms": session_start,
        "search_ms": search_ms,
    }

    # Cheap backup smoke (ensures backup path works during setup)
    try:
        paths = run_backup(root, keep=2)
        result["backup_smoke"] = paths.get("stamp")
    except Exception as exc:  # noqa: BLE001
        result["backup_smoke_error"] = str(exc)

    if write:
        cfg = load(root)
        set_path(cfg, "latency.per_turn_ms", per_turn)
        set_path(cfg, "latency.session_start_ms", session_start)
        set_path(cfg, "latency.search_ms", search_ms)
        set_path(cfg, "embedding.resident", recommended)
        set_path(cfg, "embedding.resident_high_min_gb", high)
        write_config(cfg, root)
        result["config_written"] = True
    return result
