"""ONNX portable optimize, bytecode compile, and version handshake (spec §12.6 / §16.2)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import onnxruntime as ort

from thememory.daemon.ipc.protocol import hello_handshake
from thememory.installer.bytecode import compile_modules
from thememory.outbound.embed_onnx import active_onnx_path, optimize_onnx


def test_optimize_extended_then_skip(tmp_path: Path, monkeypatch: Any) -> None:
    (tmp_path / "model.onnx").write_bytes(b"raw")
    seen: dict[str, object] = {}

    def fake_session(path: str, sess_options: Any = None, providers: Any = None) -> object:
        assert sess_options is not None
        seen["level"] = sess_options.graph_optimization_level
        dest = Path(str(sess_options.optimized_model_filepath))
        dest.write_bytes(b"opt")
        return object()

    monkeypatch.setattr(ort, "InferenceSession", fake_session)
    first = optimize_onnx(tmp_path)
    assert first["status"] == "ok"
    assert first["skipped"] is False
    assert seen["level"] == ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    second = optimize_onnx(tmp_path)
    assert second["skipped"] is True


def test_optimize_missing_model(tmp_path: Path) -> None:
    assert optimize_onnx(tmp_path)["reason"] == "model_missing"


def test_active_path_prefers_fresh_opt(tmp_path: Path) -> None:
    src = tmp_path / "model.onnx"
    opt = tmp_path / "model.opt.onnx"
    src.write_bytes(b"raw")
    opt.write_bytes(b"opt")
    os.utime(opt, (src.stat().st_atime, src.stat().st_mtime + 5))
    assert active_onnx_path(tmp_path) == opt
    os.utime(opt, (src.stat().st_atime, src.stat().st_mtime - 5))
    assert active_onnx_path(tmp_path) == src


def test_compile_modules_writes_pyc(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    result = compile_modules(pkg)
    assert result["ok"] is True
    assert list(pkg.rglob("*.pyc"))


def test_same_protocol_newer_client_restarts() -> None:
    same = hello_handshake(
        {"client_version": "0.1.0", "protocol_version": 1},
        server_version="0.1.0",
        server_protocol=1,
        state="ready",
    )
    assert same["ok"] is True
    assert "restart_required" not in same
    newer = hello_handshake(
        {"client_version": "0.2.0", "protocol_version": 1},
        server_version="0.1.0",
        server_protocol=1,
        state="ready",
    )
    assert newer["restart_required"] is True
    assert newer["ok"] is False
    proto = hello_handshake(
        {"client_version": "0.1.0", "protocol_version": 2},
        server_version="0.1.0",
        server_protocol=1,
        state="ready",
    )
    assert proto["restart_required"] is True
