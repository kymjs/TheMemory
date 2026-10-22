# TheMemory

为 Agent 打造的统一推理记忆插件（后台常驻记忆层）。

## 开发环境

要求：Python ≥ 3.11，推荐使用 [uv](https://docs.astral.sh/uv/)。

```bash
# 安装本机工具链（若尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

# 同步依赖并进入可编辑安装
uv sync

# 运行检查
uv run ruff check src tests
uv run mypy src
uv run pytest
```

入口命令（实现中）：

- `thememory` — 用户 CLI
- `thememoryd` — 常驻守护进程

数据目录默认在 `~/.thememory/`。技术方案见 `doc/技术方案文档.md`。
