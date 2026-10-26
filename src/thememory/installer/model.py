"""Install bge-small-zh-v1.5 ONNX from the vendored tree into ~/.thememory/models."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path

from thememory.outbound.embed_onnx import MODEL_DIR_NAME, onnx_path_ok
from thememory.paths import models_dir

logger = logging.getLogger(__name__)

_VENDOR = Path(__file__).resolve().parents[3] / "vendor" / "models" / MODEL_DIR_NAME
_REQUIRED = ("model.onnx", "tokenizer.json", "manifest.json")


def vendor_model_dir() -> Path:
    return _VENDOR


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def ensure_model(root: Path | None = None, *, force: bool = False) -> Path:
    """Copy vendored ONNX into the data dir. Never downloads at runtime."""
    dest = models_dir(root) / MODEL_DIR_NAME
    dest.mkdir(parents=True, exist_ok=True)
    vendor = vendor_model_dir()
    if not vendor.exists() or not onnx_path_ok(vendor):
        raise RuntimeError(
            f"vendored embedding model missing at {vendor}. "
            "Expected model.onnx + tokenizer.json in the repository."
        )
    if not force and onnx_path_ok(dest):
        _verify_or_refresh(vendor, dest)
        return dest
    for name in _REQUIRED:
        src = vendor / name
        if not src.exists() and name == "model.onnx":
            nested = vendor / "onnx" / "model.onnx"
            if nested.exists():
                shutil.copy2(nested, dest / "model.onnx")
                continue
        if src.exists():
            shutil.copy2(src, dest / name)
    nested = dest / "onnx" / "model.onnx"
    if nested.exists() and not (dest / "model.onnx").exists():
        shutil.copy2(nested, dest / "model.onnx")
    _write_install_manifest(vendor, dest)
    if not onnx_path_ok(dest):
        raise RuntimeError(f"model install incomplete: {dest}")
    logger.info("installed embedding model from %s -> %s", vendor, dest)
    return dest


def _verify_or_refresh(vendor: Path, dest: Path) -> None:
    v_onnx = vendor / "model.onnx"
    d_onnx = dest / "model.onnx"
    if not v_onnx.exists() or not d_onnx.exists():
        return
    if v_onnx.stat().st_size != d_onnx.stat().st_size:
        shutil.copy2(v_onnx, d_onnx)


def _write_install_manifest(vendor: Path, dest: Path) -> None:
    payload = {
        "model": MODEL_DIR_NAME,
        "source": "vendor",
        "vendor_path": str(vendor),
        "files": {},
    }
    for name in _REQUIRED:
        path = dest / name
        if path.exists():
            payload["files"][name] = {
                "sha256": _file_sha256(path),
                "bytes": path.stat().st_size,
            }
    (dest / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
