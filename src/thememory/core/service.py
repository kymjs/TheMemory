"""Compatibility shim — MemoryService lives in thememory.runtime (spec §3.4)."""

from thememory.runtime.service import MemoryService, parse_backoff_seconds

__all__ = ["MemoryService", "parse_backoff_seconds"]
