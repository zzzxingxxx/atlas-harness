# AtlasHarness

AtlasHarness 是一个模型无关、工具可插拔、会话可恢复、能力可演化的 Agent Runtime。

当前实现覆盖 M0/M1 基础设施、M2 安全工具执行、M3 模型适配层与 Agent Loop、M4 Session / Lane / 快照与崩溃恢复、M5 上下文编译器与结构化压缩、M6 Memory、Skill 和来源追踪、M7 Pending Window 与受控自进化，以及 M8 MCP、子 Agent、HTTP 和观测台接口：包括 append-only 事件日志、SQLite 事件索引、纯函数 Reducer、事件订阅、Tool Registry、五个内置工具、路径/命令/网络策略、审批、超时、取消、输出截断和统一脱敏，统一流式协议、OpenAI 兼容适配器、可脚本化的 fake 适配器、三条消息队列和一次完整的「模型 -> 工具 -> 结果 -> 模型」闭环，周期性快照、显式 session 恢复、`suspended`/`resume`/`abort` 与人工确认、Lane 与分支导航，以及五槽位上下文编译、Token 计数接口、70%/85%/95% 阈值策略、九字段结构化摘要和大工具输出的 artifact 外置，以及四层 Memory、SQLite FTS5/BM25 检索、Skill YAML/JSON metadata 与版本状态机、权限过滤和逐次注入记账，以及 Pending Window 反馈收集、候选 Skill 抽取与证据绑定、add/merge/reject 决策、固定 benchmark 与七项指标评测、champion 晋升和回滚，以及 MCP 服务器连接与工具翻译、每服务器的 scope/超时/并发/凭证隔离、子 Agent 任务合同与隔离执行、FastAPI 的 Session/Run/Abort/Resume/Compact/Events/Trace/Audit/Export/Skills 路由，和 trace.jsonl、audit.jsonl、metrics.json、replay-report.json 四个观测台产物。后续功能模块会按照 [Python 开发计划](./agent-harness-python-development-plan.md) 的 M9 里程碑逐步加入。

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
atlas tools [--json]                   # 列出五个内置工具及其权限
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
atlas compact <session-id> [--operation <id>] [--objective <text>] [--json]
atlas skills [--status draft|candidate|active|deprecated|retired] [--json]
atlas skills-load [--dir <path>] [--session <id>] [--json]   # 注册磁盘上的 Skill 文件
atlas skill-status <skill-id> [--version <v>] --to <status> \
    [--reason <text>] [--evaluation <ref>] [--json]           # 沿生命周期推进一个版本
atlas memory [--remember <text>] [--layer working|episodic|semantic|procedural] \
    [--source-task <id>] [--confidence <0-1>] [--evidence <ref>]... [--tag <t>]... [--json]
atlas memory-expire <memory-id> | --sweep [--reason <text>] [--json]
atlas capabilities "<task text>" [--json]                     # 检索会选中什么，以及别的为什么落选
atlas feedback [--record <text>] [--kind correction|failure|success] \
    [--task <id>] [--tool <name>] [--evidence <ref>]... [--tag <t>]... [--json]
atlas candidates [--status proposed|rejected|evaluated|promoted] [--json]
atlas skill-propose [--task <id>] [--skill <skill-id>] [--json]   # 从反馈生成候选，不改变有效能力
atlas skill-evaluate <candidate-id> [--json]                      # 跑固定 benchmark，记录七项指标
atlas skill-promote <candidate-id> [--reason <text>] [--json]      # 只有评测通过的候选能晋升
atlas skill-rollback <skill-id> --to <version> [--reason <text>] [--json]
atlas audit <session-id> [--category model|skill|tool|approval|truncation|compaction|recovery|mcp|subagent] [--json]
atlas export <session-id> [--out <dir>] [--json]   # 写出四个观测台产物
atlas mcp list [--json]                            # 只读配置，不连接任何服务器
atlas mcp inspect <server> [--json]                # 连接一次，打印能力清单和被拒工具
python -m atlas_harness.transport.http             # FastAPI 入口，默认 127.0.0.1:8765
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

## 上下文编译器与结构化压缩（M5）

`schema_version` 升到 4，新增 `context_compact_pending`、`context_compacted`、`artifact_stored`。读取端仍然接受版本 1、2、3。

M5 的全部安全论证都建立在一句不对称上：**压缩替换的是模型读到的内容，不是日志持有的内容。** 上下文是日志的缓存，压缩重建缓存；事件日志一条都不少。`test_compacting_does_not_remove_any_event` 用 `atlas replay` 从日志重建状态来检查这一点——如果压缩真删了什么，回放里就会缺东西。

### 五个固定槽位

编译顺序即优先级，裁剪顺序是它的逆序：

| 槽位 | 内容 | 裁剪 |
| --- | --- | --- |
| `fixed` | 系统提示、身份、硬约束 | 钉死，永不裁剪 |
| `task` | 当前目标、计划、压缩摘要 | 最后 |
| `capability` | 工具声明与权限 | 第四 |
| `short_term` | 最近若干轮对话 | 第二 |
| `evidence` | 工具输出、检索结果、artifact 引用 | 最先 |

`fixed` 不允许被工具结果覆盖，所以预算再紧也不会出现一个没有指令的 prompt；预算连钉死的槽位都装不下时 `_trim` 抛 `BudgetExceededError`——拒绝比发出一个没有系统提示的请求好。

编译里有两种互不相同的排序，混淆它们是一个值得点名的 bug：**相关性**决定预算紧张时谁活下来（`_rank`），**插入顺序**决定活下来的内容出现在 prompt 的哪里（`_reading_order`）。只用相关性排序会把对话记录倒过来交给模型。

### Token 计数与阈值

`TokenCounter` 是一个 Protocol：先用 `EstimatingCounter` 估算，Provider 提供精确接口时换成 `AdapterCounter`。计数端点失败时回落到估算值——一个统计接口挂了不该让运行停下来。

`ContextBudget` 把上限映射到三条线：

- **70%** `prepare`：只写一条 `context_compact_pending`，不丢任何东西。越过 70% 是信息，还不是理由。
- **85%** `compact`：自动压缩，理由记为 `threshold`。
- **95%** `force`：强制压缩，理由记为 `overflow`。

三个理由（`manual`/`threshold`/`overflow`）是一个封闭集合，未知理由直接拒绝，这样审计可以按它分组。

### 结构化摘要的九个字段

摘要不问模型，而是从日志折叠出来——`Compactor.summarize()` 读该操作下的事件，得到 `current_objective`、`task_progress`、`blockers`、`next_actions`、`decisions`、`tool_lessons`、`failed_paths`、`evidence_refs`、`open_questions`。九个字段永远都在，即使为空：消费方不该需要区分「没找到」和「键被丢了」。

- 成功的工具调用进 `task_progress`，失败的进 `tool_lessons` 和 `failed_paths`——模型的下一步取决于知道什么没成。
- M4 的挂起状态进 `blockers` 和 `open_questions`，所以压缩不会让模型忘记运行为什么停了。
- 未消费的队列消息进 `next_actions`，欠着的指令不会因为压缩而消失。
- 上一次压缩进 `decisions`，长任务不会丢掉第一次压缩确立的东西。
- 摘要会重新进入 prompt，所以和其他内容一样脱敏。

摘要每项都有条数和长度上限，超出时保留最近的：一个无界增长的摘要否定了它自己的目的。

### 重建 prompt

`_rebuild()` 保留系统消息 + 摘要 + 最近 N 轮。`_safe_split()` 会把切分点往前挪，越过任何一条其助手调用会被切掉的 `tool` 消息——一条没有对应请求的工具结果在若干 Provider 上是非法的。

已经到底的对话记录（比如第 1 轮只有 `[system, user]`）没有可替换的内容：自动触发时 `require_replacement=True` 让它什么都不记录，否则压力一直高就会每轮写一条空压缩事件；手工触发仍然记录，因为运营者问了，答案就该进记录。

### 大输出外置为 artifact

超过 `max_artifact_inline_bytes`（默认 4096，按编码后字节数算）的工具输出写成 artifact 文件，prompt 里只留 `artifact_id`、大小和一段预览。文件先写、`artifact_stored` 事件后写，和快照同一个顺序。写盘前脱敏——artifact 文件和日志一样持久，里面的密钥会活过它下游的每一道过滤器。

artifact 是**额外**一份拷贝：事件日志无论如何都保留着输出，所以这里不会是那些字节唯一存在的地方。

### 三个触发点

```bash
uv run atlas compact <session-id>                      # 运营者手工标记，理由 manual
uv run atlas compact <session-id> --objective "ship M5"
```

模型侧有 `compact_context` 工具，但这个工具本身不做压缩：它只记录意图，重写由 Loop 完成——一个能重写自己 prompt 的模型也能把 `fixed` 槽位丢掉。自动压缩在每轮迭代 drain 队列之后测量，所以一条把 prompt 顶过线的 steer 消息会算在它到达的那一轮里。

`tests/integration/test_compaction_loop.py` 验证计划的完成条件：一个固定长任务经过至少一次自动压缩后仍然继续执行并完成。

## Memory、Skill 和来源追踪（M6）

`schema_version` 在 M6 升到 5，新增 `memory_stored`、`memory_expired`、`skill_registered`、`skill_status_changed`、`capability_injected`。读取端仍然接受版本 1、2、3、4。

M6 要回答的是一个审计问题，不是一个检索问题：**为什么这次请求里有这条 Skill，而运营者以为会出现的那条不在。** 所以选择过程本身是被记录的对象，`capability_injected` 同时写下选中的和落选的，每条落选都带一个来自封闭集合的理由。

### 四层 Memory 和它们的来源

| 层 | 默认 TTL | 权重 | 用途 |
| --- | --- | --- | --- |
| `working` | 1 小时 | 0.5 | 当前任务的临时事实 |
| `episodic` | 14 天 | 0.6 | 发生过什么 |
| `semantic` | 无 | 0.9 | 关于项目的长期事实 |
| `procedural` | 无 | 1.0 | 怎么做一件事 |

每条 Memory 都带来源任务、创建时间、过期时间、置信度和证据引用，检索得分是 `文本分 × 层权重 × (0.5 + 0.5 × 置信度)`。层权重让一条 `procedural` 记录压过一条同样匹配的 `working` 记录——短期噪音不该和长期结论竞争。

**过期不是删除。** `atlas memory-expire` 写一条 `memory_expired` 并把条目从 FTS 索引里摘掉，投影行和原始的 `memory_stored` 事件都留着，所以回放仍然能说出这条 Memory 存在过、以及它什么时候停止被使用。真正删除需要显式的管理命令、审计记录和备份策略。这也是 Reducer 把 `expired_memory_ids` 和 `memory_ids` 分成两个列表的原因：合成一个就分不出「从未存在」和「不再有效」。

过期的 `episodic` 记录**不会**作为长期事实注入。它在排序之前就被过滤掉，而不是被降权——降权意味着在候选稀少时它仍然可能挤进 limit-N 的结果里。

### 检索为什么是稳定的

SQLite `bm25()` 返回负值、越负越好，`memory/retrieval.py::score_for()` 和 `SkillRepository.search()` 都做了翻转，下游一律「越大越好」。排序键是 `(-score, -created_at_ms, memory_id)`：id 是一个全序，所以并列项的先后与索引遍历顺序无关，同一个任务每次得到同一个排序。

MATCH 查询里每个词都被引号包起来再用 `OR` 连接——散文里裸露的 `AND`、`NEAR`、`*` 都是 FTS5 语法，不加引号会让一句正常的话变成语法错误。

### Skill 的版本状态机

Skill 从 YAML/JSON 文件加载，带 `skill_id`、`version`、`triggers`、`required_scopes`、`source_task`、`evidence_refs`。状态图是：

```
draft ──→ candidate ──→ active ──→ deprecated
  │           │           │            │
  └───────────┴───────────┴────────────┴──→ retired
```

`draft → active` 这条边故意不存在，**缺失的这条边就是评测门禁**：一次提升必须先经过 `candidate`，并在 `--evaluation` 里带上评测引用。加载也不等于启用——`LOADABLE_STATUSES` 只有 `draft` 和 `candidate`，一个文件无法宣布自己 `active`，所以往目录里放一个文件不能让它进入下一次请求的上下文。

### 选择流水线

按计划的顺序：规则预筛 → 检索 → 权限过滤 → token 排序 → 少量注入。每一级只能拒绝，后面看到的东西不会被前面藏起来。

- **声明的 trigger 压过任何文本分**（记 10.0）。trigger 是作者对「什么时候该用我」的直接陈述，不该取决于 BM25 有没有恰好索引到对的词。
- **权限不是排序信号**。`required_scopes` 超出授权的 Skill 记为 `not_permitted` 直接拒绝，而不是降权。注入它只会换来一次策略必然拒绝的调用，白花一轮迭代去发现 harness 已经知道的事。授权来自 `AgentService.build_selector()` 传进去的 `executor.policy.granted_scopes`，选择器和执行器读的是同一份，不会出现「注入了但执行不了」。
- **预算紧张时 Skill 先于 Memory**。Skill 是怎么做，Memory 是是什么；只装得下一个的 prompt 拿指令比拿事实更有用。
- **`_fit()` 是首次适应，不是背包**。为了塞进两条边缘条目而跳过一条高相关条目，会让同一个查询随着库增长产生不同的上下文。
- 注入是**每次请求**的事：`_request_messages()` 只把选中的内容放进 `ModelRequest`，从不写进 `RunState`，所以关掉 `ATLAS_INJECT_CAPABILITIES` 得到的就是 M6 之前的 prompt——这正是评测检索本身需要的对照。

落选理由是一个封闭集合：`not_permitted`、`not_active`、`expired`、`below_threshold`、`over_limit`、`budget`、`duplicate`。空的能力槽位因此是可解释的，而不是「什么都没匹配上」和「全都没权限」看起来一样。

### 从命令行看一次选择

```bash
uv run atlas skills-load --dir ./skills          # 全部登记为 draft/candidate
uv run atlas skill-status release-notes --version 1.0.0 --to candidate
uv run atlas skill-status release-notes --version 1.0.0 --to active --evaluation eval-42
uv run atlas memory --remember "发布说明写在 docs/releases 下" --layer semantic --confidence 0.9
uv run atlas capabilities "帮我写发布说明"
```

`capabilities` 跑的是 Agent Loop 跑的同一个选择器、用的是执行器强制的同一份授权，所以一条无权限的 Skill 在这里显示为带理由的 `-` 行，而不是一个排名很低的 `+` 行。

`tests/unit/test_capability_selector.py` 和 `tests/integration/test_capability_injection.py` 覆盖计划的四条测试条件：同一任务的检索排序稳定、无权限 Skill 不进入上下文、Skill 版本与来源和证据可追踪、过期 Episodic Memory 不作为长期事实注入。

## Pending Window 和受控自进化（M7）

`schema_version` 升到 6，新增 `feedback_recorded`、`skill_candidate_proposed`、`candidate_evaluated`、`champion_promoted`、`champion_rolled_back`。读取端仍然接受版本 1 到 5。

M7 只做一件事：**让反馈能变成候选 Skill，同时让候选在通过评测之前无法影响有效能力。** 这两半必须同时成立——只有前一半是 Skill Poisoning，只有后一半等于没有自进化。

### 缺失的那条边就是门禁

M6 的状态机里 `draft → active` 不存在，M7 就建在这个缺口上：候选一律以 `candidate` 状态登记，而注入过滤器只认 `active`。所以「候选未评测前不会被默认注入」不是一条需要记得去检查的规则，而是状态机和过滤器共同的结果——想绕过它得先加一条边。

`SkillCandidate.to_skill_record()` 因此把状态硬编码成 `candidate`。一个能在这里要求 `active` 的调用方就是第二条晋升路径，而且不带任何评测检查。

### 候选必须绑定来源和证据

`extract()` 是一条只会拒绝的阶梯，每一级都有名字：`no_evidence`（没有绑定到任务或会话的反馈）、`low_signal`（内容太短，或没有一条纠正性反馈）、`security`（脱敏命中，或正文里出现 `rm -rf`、`curl | sh`、「ignore previous instructions」、关闭策略/审批/沙箱、`--no-verify`）、`lint`、`schema`。凭空造不出候选：`atlas skill-propose` 在没有反馈时报 `considered: 0`，而不是发明一条。

检索到相似 Skill 时按 Jaccard 相似度分三档处置：低于 0.45 是 `add`，0.45 到 0.92 之间是 `merge`（继承对方的 `skill_id`，版本号小版本加一），高于 0.92 或 checksum 相同是 `reject: duplicate`。

### 七项指标和固定 benchmark

评测跑两个固定集合（`regression` 三题 + `security` 两题），每题的成绩从**事件日志**里读，不从运行的返回值里读——shadow run 结束时内存里的结果已经丢掉了工具调用，日志没有丢。同一个打分函数因此既能评一次刚跑完的 shadow run，也能评几周前的会话，这才让回归检查有意义。

七项指标永远都在，即使为零：`pass_at_1`、`completion_rate`、`tool_effectiveness`、`cost_usd`、`safety_violation_rate`、`regression_rate`、`recovery_rate`。消费方在比较候选和 champion 时不该需要区分「键缺失」和「真的是 0」，而回归恰好是缺失字段最容易藏住的东西。

安全违规单独计数，不并入普通失败：一个错答案的代价是重试一次，一个泄露的密钥无法重试。

### shadow run 不写激活

候选的 shadow run 通过替换 `service.skills` 视图完成，一次运行内有效，不写任何 `skill_status_changed`。如果它靠激活来生效，评测中途崩一次就会留下一个没被测量过的版本在对外服务。

### 晋升与回滚的顺序

两个方向的写入顺序是相反的，而且都不是任意选的：

- **晋升是先激活后弃用**。有一瞬间两个版本都是 `active`，此时 `active()` 解析到更高的版本；反过来则会有一瞬间**没有**有效版本。
- **回滚是先弃用后激活**。回滚目标是更低的版本，先激活它的话更高的那个仍然赢。

`rollback_targets()` 只列 `deprecated` 版本。一个 `candidate` 从未在役，回到它是一次披着回滚外衣的晋升。所以回滚一个从未存在的版本会被拒绝，并在 `details.available` 里给出真正可去的版本。

### 完整流程

```bash
uv run atlas feedback --record "发布说明要按模块分组" --kind correction --task op_release_notes --evidence ses_abc
uv run atlas candidates                                  # pending window：登记了，但不生效
uv run atlas skill-propose --task op_release_notes
uv run atlas skill-evaluate <candidate-id>               # 失败时退出码非零，CI 能看见
uv run atlas skill-promote <candidate-id> --reason measured
uv run atlas skill-rollback <skill-id> --to 0.1.0
```

`tests/unit/test_evolution.py` 和 `tests/integration/test_cli_evolution.py` 覆盖计划的四条测试条件：候选未评测前不会被默认注入、评测失败的候选不能晋升、晋升后可回滚到指定版本、新 Skill 不降低旧任务和安全任务集的成绩。集成测试里两个脚本化 Provider 的唯一区别是安全题的回答，所以门禁的两侧用同一套装置就能跑到。

## MCP、子 Agent、HTTP 和观测台（M8）

`schema_version` 在 M8 升到 7，新增 `mcp_server_connected`、`mcp_server_disconnected`、`mcp_tools_registered`、`subagent_task_started`、`subagent_task_finished`。读取端仍然接受版本 1 到 6。

M8 加的是三个入口和一个出口，约束只有一条：**它们必须共用已有的 Registry、Policy 和 Trace，而不是各自再造一份。** 一个外部工具服务器、一个被委派的子任务和一次 HTTP 请求，如果各自带着自己的权限判断，那么「这次运行为什么被允许」就不再有单一答案。

### MCP 工具是被翻译进来的，不是被信任进来的

外部服务器声明的每个工具都要过一遍 `bridge_tools()` 才能进 Registry，被拒的理由来自一个封闭集合：`unnamed`、`invalid_name`、`name_collision`、`scope_not_granted`、`too_many_tools`、`duplicate`。

- **名字被重写而不是被采纳**。`mcp_<server>_<tool>` 是唯一形式，两段都做规范化，所以外部工具永远不可能叫 `read_file`，也不可能盖住任何内置工具。规范化后工具那半为空（比如服务器提供了一个叫 `!!!` 的工具）会被判 `invalid_name`——否则它会退化成服务器的裸名字，两个不可命名的工具就会静默合成一个。
- **scope 是交集，不是声明**。`_manifest_for()` 取 `spec.scopes & config.granted_scopes`。服务器要 `fs:write` 而配置只给了 `fs:read`，这个工具直接被拒，不是被降级。
- **翻译完就是普通工具**。转成 `ToolManifest` 之后它走的是 `ToolExecutor.execute` 的同一条预检链：`registry.get` -> `tool.parse` -> `tool.policy_request` -> `policy.preflight`。`tests/security/test_mcp_policy.py` 要证明的就是这件事——一个恶意 MCP 工具无法绕过 Policy，因为它根本没有第二条通路可走。
- **隔离写在配置里**。`granted_scopes`、`connect_timeout_ms`、`call_timeout_ms`、`max_tools`、`max_output_bytes`、`max_concurrent_calls`、`env` / `env_passthrough` 都是每服务器独立的，凭证默认不继承进程环境。`requires_approval` 默认为真。
- **关闭先撤工具再写事件**。`shutdown` 的顺序是先 unbridge 再写 `mcp_server_disconnected`，所以不存在「日志说已断开但 Registry 里还留着它的工具」的窗口。

`enable_mcp` 关的是**读取**而不只是连接：没启用时连配置文件都不读。但 `atlas mcp list` 走的是绕过这道门的服务，因为它必须能区分「配置了但没启用」和「什么都没配」——后者是这里唯一不能给出的错答案。

### 子 Agent 的隔离是结构性的

`SubagentTask` 是一个 frozen 模型，`allowed_tools`、`max_tokens`、`deadline_ms`、`return_format` 四项在孩子启动前就写进 `subagent_task_started`，所以一次运行可以拿它被给定的条款来评判，而不是拿 runner 当时恰好做了什么来评判。

- **孩子拿不到父亲的可变对象**。它有自己的 session id、自己的 `QueueManager`、一个只含 `allowed_tools` 的 `ToolRegistry`，以及建在这个窄 Registry 上的 executor。父亲的队列和权限都不经过它，所以「孩子给父亲塞一条 steer 消息」没有代码路径。
- **`allowed_tools` 必须显式给**。空元组意味着完全没有工具，这是个有用的契约（总结这段、判断那个）。继承父亲的整个 Registry 会让「委派」变成多绕一步的提权。
- **契约只会被收窄**。`_clamped()` 用 `min()` 把 `max_tokens` 和 `deadline_ms` 压到运营者的上限，所以一份由模型写出来的契约无法给自己加预算。
- **超时、预算耗尽和异常都收口到同一处**。`dispatch()` 的每条出路——包括 `raise`——都只写一个 `subagent_task_finished`；超时那条还会替孩子补一个 `operation_failed`，否则孩子的 session 会带着一个未关闭的 operation，在恢复看来那是一次需要人介入的崩溃。
- **回来的是结果和证据，不是日志**。`evidence_refs` 指向孩子 session 里的 `tool_result` / `assistant_message` / `operation_failed`，父亲的日志不会因为委派而长出孩子那么大。Reducer 从不把孩子的事件折进父亲的投影。

### HTTP 与 CLI 是同一层服务的两个入口

`build_app()` 里的每个路由都只调用 `AgentService`、`SessionService`、`SkillRepository` 或 `build_bundle`，没有任何一条自己实现业务流程。

```
GET  /healthz                      GET  /sessions                 GET  /sessions/{id}
GET  /sessions/{id}/events         POST /runs                     POST /sessions/{id}/run
POST /sessions/{id}/abort          POST /sessions/{id}/resume     POST /sessions/{id}/compact
GET  /sessions/{id}/trace          GET  /sessions/{id}/audit      GET  /sessions/{id}/export
GET  /skills                       GET  /tools
```

- **错误体和 CLI 完全一致**，只多一个 `exit_code`：一个脚本对一半操作 shell out、对另一半发 POST 时，可以只判一个字段。
- **恢复规则跨入口成立**。`resume` 还欠一个人工确认时返回 409 并列出待确认的 call，往一个 suspended session 上 `run` 也是 409——新工作不能把一个待决问题埋掉。409 而不是 500，是因为客户端不该重试进去。
- **未知 session 是 404，不是空**。`read_events` 对不存在的 id 返回空列表，这对投影是对的，对路由是错的：打错一个字会得到一份干干净净的空 trace，而调用方会信它。
- **每个请求单独开 store**，因为 SQLite 连接不是线程安全的。`http_host` 默认 loopback——这个传输层没有认证，绑到 `0.0.0.0` 等于把整个工作区交出去。

### 四个产物回答的是问责问题

`atlas export` 写出 `trace.jsonl`、`audit.jsonl`、`metrics.json`、`replay-report.json`，`GET /sessions/{id}/export` 返回同一个 bundle 的 JSON 形式。审计的九个类别就是计划里那七个问题加上 M8 的两个面：`model`、`skill`、`tool`、`approval`、`truncation`、`compaction`、`recovery`、`mcp`、`subagent`——一台外部工具服务器和一次委派任务同样需要事后归因。

`replay-report.json` 的折叠是**非严格**的：这份报告恰恰是在日志看起来有损时才被人打开的，所以中间缺一个 seq 必须变成报告里的一处 `gaps`，而不是变成让报告写不出来的那个异常。完整日志两种折叠方式结果相同，因为那时每个 seq 都是期望的那个。被派出去却没有结果的任务同样进 `problems`——`open_subagent_task_ids` 只能意味着「还在跑」，绝不能意味着「runner 把它丢了」。

### 三个入口由同一套回放验证

计划的完成条件是 MCP、子 Agent 和 HTTP 入口都能被同一套回放测试验证，所以 `tests/replay/test_determinism.py` 底部把三者放进了同一个文件：一段含 MCP 握手和委派任务的日志要两次折出同一个 hash、丢掉索引后仍能重建、每个前缀都等于增量归约；而一次由 HTTP 驱动的运行读回来之后，`replay()` 的 hash 要等于 store 投影的 hash。

`tests/integration/test_http_api.py` 用同样的方式证明「不重复业务逻辑」——它不去比对两个 handler 的代码，而是断言经 HTTP 跑出的事件序列和 `tests/integration/test_cli_agent.py` 断言的 CLI 序列逐项相同，且 trace / audit / export 三个响应体等于直接从这份日志构建出来的结果。两个入口产出一份日志，是这条要求唯一可观测的形式。

后续工具沙箱和分布式执行功能以项目方案及开发计划为准。
