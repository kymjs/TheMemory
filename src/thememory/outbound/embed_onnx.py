"""ONNX embedding via bge-small-zh-v1.5."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MODEL_DIR_NAME = "bge-small-zh-v1.5"
MODEL_ID = "bge-small-zh-v1.5@onnx-fp32"
EXPECTED_DIM = 512


class OnnxEmbedder:
    def __init__(
        self,
        model_dir: Path,
        *,
        query_instruction: str = "",
    ) -> None:
        self.model_dir = model_dir
        self.query_instruction = query_instruction
        self.model_id = MODEL_ID
        self.dim = EXPECTED_DIM
        self._session = None
        self._tokenizer = None
        self._load_error: str | None = None

    def available(self) -> bool:
        return self._ensure() is not None

    def _ensure(self) -> object | None:
        if self._session is not None:
            return self._session
        onnx_path = self.model_dir / "model.onnx"
        tok_path = self.model_dir / "tokenizer.json"
        if not onnx_path.exists() or not tok_path.exists():
            self._load_error = f"model files missing under {self.model_dir}"
            return None
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(tok_path))
            self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            self._tokenizer.enable_truncation(max_length=512)
            opts = ort.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 2
            self._session = ort.InferenceSession(
                str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
            # detect dim from output
            out_shape = self._session.get_outputs()[0].shape
            if len(out_shape) >= 2 and isinstance(out_shape[-1], int):
                self.dim = int(out_shape[-1])
            return self._session
        except Exception as exc:  # noqa: BLE001
            self._load_error = str(exc)
            logger.exception("failed to load onnx embedder")
            return None

    def embed(self, texts: list[str], *, is_query: bool = False) -> list[list[float]]:
        session = self._ensure()
        if session is None or self._tokenizer is None:
            raise RuntimeError(self._load_error or "embedder unavailable")
        prepared = []
        for t in texts:
            if is_query and self.query_instruction:
                prepared.append(f"{self.query_instruction}{t}")
            else:
                prepared.append(t)
        encodings = self._tokenizer.encode_batch(prepared)
        max_len = max(len(e.ids) for e in encodings)
        input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(encodings), max_len), dtype=np.int64)
        token_type_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            ids = enc.ids
            mask = enc.attention_mask
            input_ids[i, : len(ids)] = ids
            attention_mask[i, : len(mask)] = mask
        feeds = {}
        input_names = {i.name for i in session.get_inputs()}  # type: ignore[union-attr]
        if "input_ids" in input_names:
            feeds["input_ids"] = input_ids
        if "attention_mask" in input_names:
            feeds["attention_mask"] = attention_mask
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = token_type_ids
        outputs = session.run(None, feeds)  # type: ignore[union-attr]
        arr = outputs[0]
        # mean pool if token embeddings
        if arr.ndim == 3:
            mask = attention_mask[..., None].astype(np.float32)
            summed = (arr * mask).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            arr = summed / counts
        # L2 normalize
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.clip(norms, 1e-12, None)
        arr = arr / norms
        return [row.astype(np.float32).tolist() for row in arr]

    @property
    def load_error(self) -> str | None:
        return self._load_error


def verify_manifest(model_dir: Path) -> bool:
    manifest = model_dir / "manifest.json"
    if not manifest.exists():
        return onnx_path_ok(model_dir)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for name, expected in data.get("files", {}).items():
        path = model_dir / name
        if not path.exists():
            return False
        if expected.get("sha256"):
            import hashlib

            h = hashlib.sha256(path.read_bytes()).hexdigest()
            if h != expected["sha256"]:
                return False
    return True


def onnx_path_ok(model_dir: Path) -> bool:
    return (model_dir / "model.onnx").exists() and (model_dir / "tokenizer.json").exists()
