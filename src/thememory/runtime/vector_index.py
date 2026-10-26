"""In-memory embedding set maintained by the daemon (spec §9.5)."""

from __future__ import annotations

from typing import Any

import numpy as np

from thememory.core.vectors import unpack_vector


class VectorIndex:
    def __init__(self) -> None:
        self.model_id: str | None = None
        self._ids: list[str] = []
        self._id_to_row: dict[str, int] = {}
        self._matrix = np.zeros((0, 0), dtype=np.float32)

    @property
    def ids(self) -> list[str]:
        return self._ids

    @property
    def matrix(self) -> np.ndarray:
        return self._matrix

    def clear(self) -> None:
        self.model_id = None
        self._ids = []
        self._id_to_row = {}
        self._matrix = np.zeros((0, 0), dtype=np.float32)

    def rebuild(
        self,
        rows: list[tuple[str, bytes, int]],
        *,
        model_id: str,
    ) -> None:
        self.clear()
        self.model_id = model_id
        if not rows:
            return
        dim = int(rows[0][2])
        ids: list[str] = []
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        for i, (item_id, blob, d) in enumerate(rows):
            if int(d) != dim:
                continue
            vec = np.asarray(unpack_vector(blob, dim), dtype=np.float32)
            n = float(np.linalg.norm(vec))
            if n > 0:
                vec = vec / n
            ids.append(item_id)
            mat[len(ids) - 1] = vec
            self._id_to_row[item_id] = len(ids) - 1
        self._ids = ids
        self._matrix = mat[: len(ids)]

    def upsert(self, item_id: str, vector: list[float], *, model_id: str) -> None:
        if self.model_id != model_id:
            self.clear()
            self.model_id = model_id
        vec = np.asarray(vector, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(vec))
        if n > 0:
            vec = vec / n
        if item_id in self._id_to_row:
            self._matrix[self._id_to_row[item_id]] = vec
            return
        if self._matrix.size == 0:
            self._matrix = vec.reshape(1, -1)
        else:
            if self._matrix.shape[1] != vec.shape[0]:
                return
            self._matrix = np.vstack([self._matrix, vec.reshape(1, -1)])
        self._id_to_row[item_id] = len(self._ids)
        self._ids.append(item_id)

    def remove(self, item_id: str) -> None:
        row = self._id_to_row.pop(item_id, None)
        if row is None:
            return
        self._ids.pop(row)
        self._matrix = np.delete(self._matrix, row, axis=0)
        self._id_to_row = {iid: i for i, iid in enumerate(self._ids)}

    def get(self, item_id: str) -> list[float] | None:
        row = self._id_to_row.get(item_id)
        if row is None:
            return None
        return self._matrix[row].tolist()

    def snapshot_map(self, item_ids: list[str]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for iid in item_ids:
            vec = self.get(iid)
            if vec is not None:
                out[iid] = vec
        return out

    def as_debug(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "count": len(self._ids),
            "dim": int(self._matrix.shape[1]) if self._matrix.ndim == 2 else 0,
        }
