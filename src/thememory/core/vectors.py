"""Float32 embedding helpers (pure; no storage I/O)."""

from __future__ import annotations

import struct
from typing import Iterable

import numpy as np


def pack_vector(values: list[float] | np.ndarray) -> bytes:
    arr = np.asarray(values, dtype=np.float32).reshape(-1)
    return arr.tobytes(order="C")


def unpack_vector(blob: bytes, dim: int) -> list[float]:
    if dim <= 0:
        return []
    count = min(dim, len(blob) // 4)
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def as_unit_matrix(rows: Iterable[list[float] | np.ndarray]) -> np.ndarray:
    mats = [np.asarray(r, dtype=np.float32).reshape(-1) for r in rows]
    if not mats:
        return np.zeros((0, 0), dtype=np.float32)
    return np.stack(mats, axis=0)
