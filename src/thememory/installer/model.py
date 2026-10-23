"""Download / install bge-small-zh-v1.5 ONNX model into ~/.thememory/models."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from thememory.outbound.embed_onnx import MODEL_DIR_NAME, onnx_path_ok
from thememory.paths import models_dir

logger = logging.getLogger(__name__)

REPO_ID = "Xenova/bge-small-zh-v1.5"


def ensure_model(root: Path | None = None, *, force: bool = False) -> Path:
    dest = models_dir(root) / MODEL_DIR_NAME
    dest.mkdir(parents=True, exist_ok=True)
    # flatten if previously downloaded into onnx/
    nested = dest / "onnx" / "model.onnx"
    if nested.exists() and (force or not (dest / "model.onnx").exists()):
        shutil.copy2(nested, dest / "model.onnx")
    if not force and onnx_path_ok(dest):
        return dest
    vendor = Path(__file__).resolve().parents[3] / "vendor" / "models" / MODEL_DIR_NAME
    if vendor.exists() and onnx_path_ok(vendor):
        for name in ("model.onnx", "tokenizer.json", "manifest.json"):
            src = vendor / name
            if src.exists():
                shutil.copy2(src, dest / name)
        return dest

    # Prefer hf-mirror for CN networks
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    print(f"Downloading embedding model into {dest} (via huggingface_hub)...")
    from huggingface_hub import hf_hub_download

    tok = hf_hub_download(REPO_ID, "tokenizer.json", local_dir=str(dest))
    model = hf_hub_download(REPO_ID, "onnx/model.onnx", local_dir=str(dest))
    logger.info("downloaded %s and %s", tok, model)
    nested = dest / "onnx" / "model.onnx"
    if nested.exists():
        shutil.copy2(nested, dest / "model.onnx")
    (dest / "manifest.json").write_text(
        json.dumps({"model": MODEL_DIR_NAME, "repo": REPO_ID}, indent=2),
        encoding="utf-8",
    )
    if not onnx_path_ok(dest):
        raise RuntimeError(f"model install incomplete: {dest}")
    return dest
