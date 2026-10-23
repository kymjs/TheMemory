# TheMemory

为 Agent 打造的统一推理记忆插件（后台常驻记忆层）。

## 要求

- Python 3.11–3.13（推荐 3.12）
- [uv](https://docs.astral.sh/uv/)
- 可选宿主：Claude Code、DeepSeek Harness（DSH）

## 开发安装

```bash
uv sync
uv run thememory setup --dev --install-hosts
```

`setup` 会：

1. 创建 `~/.thememory/`
2. 下载/安装 bge-small-zh-v1.5 ONNX 嵌入模型
3. 拉起 `thememoryd`
4. （`--install-hosts`）写入 Claude Code hooks + MCP，以及 DSH `cordis.patch.yml`

配置提取 LLM（推荐可视化）：

```bash
uv run thememory ui
# 浏览器打开 http://127.0.0.1:8765/
# 可选择 DSH/pi-ai 同款提供方自动填 base_url，并刷新模型列表
```

也可继续用 CLI / 手改 `~\.thememory\config.toml`：

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
