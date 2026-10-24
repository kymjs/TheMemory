"""Embedding residency decision (spec §13.3). Memory probes stay in the stdlib."""

from __future__ import annotations

import sys
from typing import Any


def read_memory_gb() -> tuple[float | None, float | None]:
    """Return (available_gb, total_gb). None when the platform probe fails."""
    try:
        if sys.platform == "win32":
            return _windows_memory_gb()
        if sys.platform == "linux":
            return _linux_memory_gb()
        if sys.platform == "darwin":
            return _macos_memory_gb()
    except OSError:
        return None, None
    return None, None


def decide_residency(
    *,
    mode: str,
    available_gb: float | None,
    total_gb: float | None,
    footprint_gb: float,
    high_multiple: float = 4,
    high_min_gb: float = 2,
    low_multiple: float = 2,
    min_total_gb: float = 0,
) -> tuple[str, str]:
    """Return (action, reason). action is pin, on_demand, or unload."""
    normalized = str(mode).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return "pin", "resident=true：启动即常驻"
    if normalized in {"0", "false", "no"}:
        return "on_demand", "resident=false：按需加载，空闲后卸载"
    if min_total_gb > 0 and total_gb is not None and total_gb < min_total_gb:
        return (
            "on_demand",
            f"auto：总内存 {total_gb:.1f}GB < 下限 {min_total_gb:.1f}GB，不常驻",
        )
    if available_gb is None or footprint_gb <= 0:
        return "on_demand", "auto：缺少可用内存或模型占用，按需加载"
    high = max(footprint_gb * high_multiple, high_min_gb)
    low = footprint_gb * low_multiple
    if available_gb >= high:
        return "pin", f"auto：可用 {available_gb:.1f}GB ≥ 高水位 {high:.1f}GB，常驻"
    if available_gb < low:
        return "unload", f"auto：可用 {available_gb:.1f}GB < 低水位 {low:.1f}GB，卸载"
    return "on_demand", f"auto：可用 {available_gb:.1f}GB < 高水位 {high:.1f}GB，按需加载"


def residency_from_config(
    cfg: dict[str, Any],
    *,
    available_gb: float | None,
    total_gb: float | None,
) -> tuple[str, str]:
    emb = cfg.get("embedding") or {}
    return decide_residency(
        mode=str(emb.get("resident") or "auto"),
        available_gb=available_gb,
        total_gb=total_gb,
        footprint_gb=float(emb.get("footprint_gb") or 0.5),
        high_multiple=float(emb.get("resident_high_multiple") or 4),
        high_min_gb=float(emb.get("resident_high_min_gb") or 2),
        low_multiple=float(emb.get("resident_low_multiple") or 2),
        min_total_gb=float(emb.get("resident_min_total_gb") or 0),
    )


def _windows_memory_gb() -> tuple[float | None, float | None]:
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
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None, None
    return stat.ullAvailPhys / (1024**3), stat.ullTotalPhys / (1024**3)


def _linux_memory_gb() -> tuple[float | None, float | None]:
    available = total = None
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) / (1024**2)
            elif line.startswith("MemTotal:"):
                total = int(line.split()[1]) / (1024**2)
    return available, total


def _macos_memory_gb() -> tuple[float | None, float | None]:
    """Pressure level only. Level 1–2 counts as enough to pin; 3–4 does not."""
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    level = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(level))
    name = b"kern.memorystatus_vm_pressure_level"
    if libc.sysctlbyname(name, ctypes.byref(level), ctypes.byref(size), None, 0) != 0:
        return None, None
    # Map pressure to a synthetic available figure so decide_residency stays pure.
    synthetic = {1: 8.0, 2: 4.0, 3: 1.0, 4: 0.4}.get(int(level.value), 1.0)
    return synthetic, None
