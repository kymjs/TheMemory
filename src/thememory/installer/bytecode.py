"""Compile installed modules to bytecode at setup time (spec §16.2 step 3)."""

from __future__ import annotations

import compileall
from pathlib import Path


def compile_modules(package_dir: Path) -> dict[str, object]:
    ok = compileall.compile_dir(str(package_dir), quiet=1, force=False)
    return {"ok": bool(ok), "path": str(package_dir)}


def compile_installed_package() -> dict[str, object]:
    import thememory

    root = Path(thememory.__file__).resolve().parent
    return compile_modules(root)
