# AtlasHarness

AtlasHarness 是一个模型无关、工具可插拔、会话可恢复、能力可演化的 Agent Runtime。

当前实现覆盖 M0/M1 基础设施、M2 安全工具执行、M3 模型适配层与 Agent Loop，以及 M4 Session / Lane / 快照与崩溃恢复：包括 append-only 事件日志、SQLite 事件索引、纯函数 Reducer、事件订阅、Tool Registry、四个内置工具、路径/命令/网络策略、审批、超时、取消、输出截断和统一脱敏，统一流式协议、OpenAI 兼容适配器、可脚本化的 fake 适配器、三条消息队列和一次完整的「模型 -> 工具 -> 结果 -> 模型」闭环，以及周期性快照、显式 session 恢复、`suspended`/`resume`/`abort` 与人工确认、Lane 与分支导航。后续功能模块会按照 [Python 开发计划](./agent-harness-python-development-plan.md) 的 M5-M9 里程碑逐步加入。

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
atlas recover <session-id> [--json]     # 只查看恢复计划，不写入任何事件
atlas resume <session-id> [--confirm <tool-call-id>]... [--json]
atlas abort <session-id> [--reason <text>] [--json]
atlas lanes <session-id> [--json]       # 列出 Lane 及各自的分支来源
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

## Session、快照与崩溃恢复（M4）

`schema_version` 升到 3，新增 `operation_suspended`、`operation_resumed`、`lane_created`、`branch_created`、`branch_switched`。读取端仍然接受版本 1 和 2。

M4 要解决的问题只有一句：**进程在模型请求、工具执行和事件写入之间崩溃后，能够恢复且不重复副作用。**

### 已完成的工具不会被重跑

恢复的判定顺序本身就是这条保证：

| 情况 | 处置 | 理由 |
| --- | --- | --- |
| 已有 `tool_result` | `restore` | 调用已经结清，无论风险多高都不再执行 |
| 幂等的只读调用 | `replay` | 按原幂等键重放是安全的 |
| 人工确认过的调用 | `replay` | 责任已由人承担 |
| 其他 | `confirm` | 副作用可能已经落地，等人决定 |

顺序很关键：`restore` 排在所有风险判断之前，所以「只有回写结果失败」的调用不会因为它风险高而被重跑——它已经跑完了，缺的只是那条记录。

注意 `idempotent` 单独不足以决定重放。`write_file` 声明自己幂等，但风险等级是 `write`，恢复时依然要人确认：harness 无法知道那次写入是否在崩溃前已经落盘。判定规则是 `risk == "read" and idempotent`，两者必须同时成立。

确认会折叠到工具调用的投影上（`ToolCallState.confirmed`），所以确认之后再崩一次，不会把同一个问题重新问一遍。部分回答会被直接拒绝而不是半应用：一个操作要恢复就得回答它全部的问题，只确认其中一部分不会让剩下的变安全，而这样的确认又不会被持久化，静默丢掉比报错更糟。

### 快照

快照文件先写、宣告事件后写。崩在中间只会留下一个没人指向的孤儿文件，恢复时忽略即可；反过来则会留下一条指向不存在文件的事件，那种日志恢复只能拒绝。文件读回时校验 checksum 和 schema 版本，任何一项不可信就退回整段日志重放——更慢，但一定正确。

```bash
uv run atlas recover <session-id>          # 只看，不写
uv run atlas resume <session-id>
uv run atlas resume <session-id> --confirm <tool-call-id>
uv run atlas abort <session-id> --reason "operator gave up"
```

恢复必须显式给出 session id。从目录 mtime 推断「最后一个会话」正是那种把崩溃变成数据丢失的猜测。`AgentService.run` 在启动时发现挂起的操作会直接抛 `RecoveryError`（退出码 7）并附上该跑的命令，不会把新工作叠在一个没人回答的问题上面。

### Lane 与分支

Lane 是一个会话内部的一条工作线。Lane 之间共享会话的只读上下文（工作区根、配置、事件日志本身），各自持有可变状态：操作队列和自己操作下的工具调用。

导航是 append-only 的：切换 Lane、或从更早的 seq 分叉出一条新 Lane，都只是写一条事件，从不改写或移除已有历史。**没有删除路径**——日志是审计记录，能编辑的历史不算审计记录。`branch_created` 拒绝日志里不存在的 `from_seq`。

### 索引表

`lanes`、`operations`、`tool_calls`、`snapshots` 四张表和 `events` 一样只是投影，全部可以由 `SessionRepository.sync()` 从日志重建（先删后插，而不是 upsert：没有对应事件的行必须消失而不是留着）。恢复决策不读这些表，它重放日志，所以一行陈旧或缺失只会影响查询结果，不影响正确性。

### 恢复测试

`tests/integration/test_crash_recovery.py` 在计划点出的每一个边界两侧都注入故障——模型请求、助手消息、工具开始、工具结果、快照创建、操作结束——每次都断言日志本身仍然完整、投影与日志一致、工具恰好执行了一次。Loop、执行器和 EventStore 共享同一个 `FaultInjector`，所以一次测试覆盖的是一条连续的时间线，不是三条互不相干的。

后续工具沙箱和能力演化功能以项目方案及开发计划为准。
