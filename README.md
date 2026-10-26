# TheMemory

为 Agent 打造的统一推理记忆插件（后台常驻记忆层）。

## 要求

- Python 3.11–3.13（推荐 3.12）
- [uv](https://docs.astral.sh/uv/)
- 可选宿主：Claude Code、DeepSeek Harness（DSH）、Codex、Hermes、Cursor、WorkBuddy、豆包办公

## 体积与隐私说明

- 仓库内置嵌入模型 `vendor/models/bge-small-zh-v1.5/`（约 95MB ONNX）。连同 uv / Python / onnxruntime，总安装体积可能约 **200–300MB**。
- **提取会把对话发往你配置的 LLM 端点**；记忆只存在本机，**不跨设备、不做云同步**（本机定期备份也仅保护本机数据）。

## 开发安装

```bash
uv sync
uv run thememory setup --yes --install-hosts --benchmark
```

交互式向导（默认，TTY 下）：

```bash
uv run thememory setup
```

`setup` 会：

1. 创建 `~/.thememory/`
2. 从仓库 `vendor/models/` **离线复制** bge-small-zh-v1.5
3. 拉起 `thememoryd`
4. （可选）写入 Claude Code / DSH / Hermes / Cursor 等宿主配置

配置提取 LLM（推荐可视化）：

```bash
uv run thememory ui
# 浏览器打开 http://127.0.0.1:8765/
```

也可继续用 CLI / 手改 `~/.thememory/config.toml`：

```bash
uv run thememory config set llm.extraction.base_url https://api.deepseek.com/v1
uv run thememory config set llm.extraction.model deepseek-chat
uv run thememory config set llm.extraction.api_key_env THEMEMORY_LLM_API_KEY
```

## 常用命令

```bash
uv run thememory ui
uv run thememory daemon start|stop|status
uv run thememory remember "用户偏好简洁回答" --type preference
uv run thememory search 简洁
uv run thememory context
uv run thememory doctor
uv run thememory install claude-code
uv run thememory install deepseek-harness
uv run thememory extract --now --session-ref <id> --host claude-code
```

## 验收提示

- **DSH**：重启 profile 后应出现 `mcp__thememory__thememory_*` 工具；让模型记住一条偏好再 search。
- **Claude Code**：对话中表达偏好 → `extract --now`（或等触发）→ 新开会话，SessionStart 应注入画像。
- 跨宿主：DSH 写入后，Claude Code 新会话快照应能看到（默认 shared）。

技术方案见 `doc/技术方案文档.md`。
