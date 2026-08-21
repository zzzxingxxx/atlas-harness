# AtlasHarness

AtlasHarness 是一个模型无关、工具可插拔、会话可恢复、能力可演化的 Agent Runtime。

当前实现覆盖 M0/M1 基础设施和 M2 安全工具执行：包括 append-only 事件日志、SQLite 事件索引、纯函数 Reducer、事件订阅、Tool Registry、四个内置工具、路径/命令/网络策略、审批、超时、取消、输出截断和统一脱敏。后续功能模块会按照 [Python 开发计划](./agent-harness-python-development-plan.md) 的 M3-M9 里程碑逐步加入。

## 环境

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## 安装和验证

```bash
uv sync
uv run atlas --help
uv run atlas version
uv run atlas doctor --json
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest tests/unit tests/integration
uv run pytest tests/replay tests/security
```

复制 `.env.example` 为 `.env` 后，可以通过 `ATLAS_` 前缀环境变量覆盖默认配置。工具执行默认限制在 `ATLAS_WORKSPACE_ROOT` 内，网络关闭，写入和命令执行需要审批；使用 `--yes` 才会批准 CLI 发起的副作用调用。

## 命令

```bash
atlas --help
atlas version [--json]
atlas doctor [--json]
atlas sessions [--json]                 # 列出数据目录中的会话
atlas inspect <session-id> [--json]     # 回放并打印会话摘要
atlas replay <session-id> [--json]      # 从事件日志重建完整状态
atlas tools [--json]                   # 列出四个内置工具及其权限
atlas tool-check <tool> --args '{}'     # 只做参数和策略预检
atlas tool-run <tool> --args '{}' [--yes] [--json]
```

所有命令失败时输出结构化错误（`{"error", "message", "details"}`）并使用固定退出码：配置 2、生命周期 3、预算 4、事件校验 5、事件存储 6、恢复 7、日志损坏 8、会话不存在 9、策略拒绝 10、审批拒绝 11、工具错误 12-16、取消 130。

## 事件内核（M1）和安全执行（M2）

数据布局：

```
<data_dir>/index.sqlite3                        # 可重建的派生索引
<data_dir>/sessions/<session-id>/events.jsonl   # 唯一事实来源
```

- **写入顺序**：先追加 JSONL 并 `fsync`，再提交 SQLite 事务；索引失败时把日志截断回写入前的偏移，两者要么同时生效要么都不生效。
- **崩溃恢复**：日志始终胜出。重启后 `EventStore` 用日志校正索引（删除陈旧行、补写缺失行、重算会话汇总），因此进程可以在任意事件前后退出。
- **拒绝而非猜测**：seq 重复、缺失或乱序，schema 版本不支持，JSONL 截断或非法 JSON，都会抛出带 `last_valid_seq` 和行号的错误，不做自动修复。
- **确定性**：`SessionState.state_hash()` 让同一事件序列的投影可比较；`FrozenClock` 与 `FaultInjector`（`event_store.before_log_write` 等注入点）让恢复路径可重复测试。
- **最小事件集**：`session_created`、`operation_started`、`model_requested`、`assistant_message`、`approval_requested`、`approval_resolved`、`tool_started`、`tool_result`、`operation_finished`、`operation_failed`、`operation_aborted`、`snapshot_created`。

M2 内置工具：

- `read_file`：只读工作区内的 UTF-8 文本，并限制文件大小和行范围。
- `write_file`：原子写入，默认需要审批，提供差异预览并对重复内容保持幂等。
- `search`：在工作区内搜索文本，跳过敏感文件、二进制文件和依赖缓存目录。
- `run_command`：只执行 allowlist 内的程序，不经过 Shell，检查危险参数并清理超时或取消后的子进程。

所有工具调用都会经过 Manifest、Scope、Path/Command/Network Policy 和审批流程，并写入 `approval_*`、`tool_started`、`tool_result` 标准事件。结果和事件参数会统一脱敏，输出受单次调用最大字节数限制。

不依赖模型即可通过事件回放构造完整 Session 状态：

```python
from pathlib import Path

from atlas_harness.events import EventStore, EventType

with EventStore(Path(".atlas")) as store:
    session_id = store.new_session_id()
    store.append_new(EventType.SESSION_CREATED, session_id=session_id, payload={"title": "demo"})
    state = store.load_state(session_id)
```

后续 Agent Loop、工具沙箱和 Session 恢复功能以项目方案及开发计划为准。
