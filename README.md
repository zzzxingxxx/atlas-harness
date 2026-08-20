# AtlasHarness

AtlasHarness 是一个模型无关、工具可插拔、会话可恢复、能力可演化的 Agent Runtime。

当前实现为 M0 工程基线：配置加载、统一错误、可注入时钟、生命周期管理、结构化日志和 Typer CLI。功能模块会按照 [Python 开发计划](./agent-harness-python-development-plan.md) 的 M1-M9 里程碑逐步加入。

## 环境

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## 安装和验证

```bash
uv sync
uv run atlas --help
uv run atlas version
uv run atlas doctor --json
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

复制 `.env.example` 为 `.env` 后，可以通过 `ATLAS_` 前缀环境变量覆盖默认配置。M0 不会自动创建 `.atlas` 目录，也不会访问模型、文件或 Shell。

## M0 命令

```bash
atlas --help
atlas version [--json]
atlas doctor [--json]
```

后续 Agent Loop、事件日志、工具和 Session 功能以项目方案及开发计划为准。
