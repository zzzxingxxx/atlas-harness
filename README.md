# AtlasHarness

AtlasHarness 是一个模型无关、工具可插拔、会话可恢复、能力可演化的 Agent Runtime。

当前实现覆盖 M0/M1 基础设施、M2 安全工具执行和 M3 模型适配层与 Agent Loop：包括 append-only 事件日志、SQLite 事件索引、纯函数 Reducer、事件订阅、Tool Registry、四个内置工具、路径/命令/网络策略、审批、超时、取消、输出截断和统一脱敏，以及统一流式协议、OpenAI 兼容适配器、可脚本化的 fake 适配器、三条消息队列和一次完整的「模型 -> 工具 -> 结果 -> 模型」闭环。后续功能模块会按照 [Python 开发计划](./agent-harness-python-development-plan.md) 的 M4-M9 里程碑逐步加入。

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
atlas run "<prompt>" [--session <id>] [--provider fake] [--model <name>] \
    [--steer <msg>] [--yes] [--json]    # 跑一次 Agent Loop
atlas messages <session-id> [--send <msg>] [--queue steer|follow_up|next_run] [--json]
atlas trace <session-id> [--json]       # 按事件日志逐行打印一次运行
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

## 模型适配层和 Agent Loop（M3）

`schema_version` 升到 2，新增四个事件类型：`model_stream_completed`、`provider_error`、`queue_message_enqueued`、`queue_message_consumed`。读取端仍然接受版本 1，M1/M2 的日志可以照原样回放。

- **统一流式协议**：所有 Provider 都映射到同一个 `ModelEvent` 联合（文本、思考、工具调用增量、用量、停止、错误）。`StreamAssembler` 是唯一负责把分片的工具调用参数拼回完整 JSON 的地方，Loop 只看到组装好的结果。
- **错误是事件，不是异常**：Provider 故障写成 `provider_error`；模型给出无法解析的参数写成 `valid=False` 的工具调用，并把这个事实作为证据回喂给模型，让它自己纠正。
- **每个工具调用都有回复**：包括参数非法和被预算拒绝的调用，都会按原顺序拿到恰好一条 `tool` 消息，模型上下文不会出现悬空的调用。
- **模型不直接调用工具**：`model` 层只产出请求，是否执行由 Loop 决定。依赖方向单向——`agent` 依赖 `model` 和 `tools`，反过来不成立。
- **三条队列**：`steer`、`follow_up`、`next_run` 各自持久化。每轮迭代只消费前两条，`next_run` 按设计留给下一次运行。消费事件先写日志再出队，所以崩溃重放的失败模式是重复一轮，而不是静默丢掉一条指令。
- **可回放的轨迹**：`atlas trace` 不做任何重新推导，每一行就是一条已持久化的事件，因此轨迹和回放不可能互相矛盾。

不需要 API key 就能跑通一次运行并回放它。`--provider fake` 使用 canned 模式：它只回一段固定文本，不请求工具，用来验证「运行 -> 事件日志 -> 轨迹 -> 回放」这条链路本身：

```bash
uv run atlas run "say hello" --provider fake --json
uv run atlas trace <session-id>
uv run atlas replay <session-id> --json
```

带真实工具调用的完整闭环由脚本化的 `FakeAdapter` 驱动：它按预写的 turn 列表先请求 `read_file`、拿到结果后再回答。M3 的验收路径 `test_the_cli_reads_a_file_and_answers_the_question`（`tests/integration/test_cli_agent.py`）就是用 `register_provider` 把这样一个适配器挂进真实 CLI，断言事件日志恰好是：

```
session_created  operation_started
model_requested  model_stream_completed          # 模型请求 read_file
tool_started     tool_result                     # 在工作区内执行
model_requested  model_stream_completed  assistant_message
operation_finished
```

真实模型走 OpenAI 兼容适配器，端点和密钥只来自运营配置（`ATLAS_MODEL_BASE_URL`、`ATLAS_MODEL_API_KEY`），密钥以 `SecretStr` 持有、只在发起调用时读取，不会进入任何事件、日志或轨迹。

后续工具沙箱和 Session 恢复功能以项目方案及开发计划为准。
