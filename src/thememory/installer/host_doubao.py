"""Doubao Office connector instructions (spec §14.5 — manual STDIO only)."""

from __future__ import annotations

import sys
from pathlib import Path

from thememory.paths import ensure_layout, home_dir

INSTRUCTIONS = """# 豆包办公 · TheMemory 自定义连接器

豆包办公的自定义连接器只能在本机手动配置，安装器不能代写其配置。

在连接器界面填写：

| 字段 | 值 |
|---|---|
| 名称 | TheMemory |
| 传输 | STDIO |
| 命令 | {python} |
| 参数 | -m thememory.inbound.mcp_proxy |
| 环境变量 | THEMEMORY_HOST=doubao |

接入前请先在本机运行：

```
thememory daemon start
```

说明：

- 豆包办公没有 hook，不能自动采集对话，也不能自动注入画像。
- 使用方式是让模型调用 `thememory_context` / `thememory_search` / `thememory_remember`。
- 记忆与同一台电脑上的其他 Agent 共用；不跨设备同步。
- 程序性记忆默认不允许该宿主写入（`procedural_write=false`）。
"""


def instructions_text() -> str:
    return INSTRUCTIONS.format(python=sys.executable)


def install(root: Path | None = None) -> dict[str, str]:
    """Write the manual steps under ~/.thememory/docs/ (does not touch Doubao config)."""
    base = root or home_dir()
    ensure_layout(base)
    docs = base / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "doubao-connector.md"
    path.write_text(instructions_text(), encoding="utf-8")
    return {"instructions": str(path), "note": "manual connector; file is a checklist only"}


def uninstall(root: Path | None = None) -> None:
    path = (root or home_dir()) / "docs" / "doubao-connector.md"
    if path.exists():
        path.unlink()
