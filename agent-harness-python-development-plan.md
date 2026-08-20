# AtlasHarness Python 开发计划

> 本文是 AtlasHarness Python 版的可执行开发计划。它把 [Python 版项目方案](./agent-harness-project-plan-python.md) 拆成工程任务、实现顺序、测试和验收步骤，不改变原方案中的功能、权限边界、恢复语义或验收目标。

## 1. 开发目标

第一阶段交付一个单进程、本地运行的 Coding Agent，能够：

```text
接收用户任务
  -> 调用模型生成文本或工具调用
  -> 校验工具参数和权限
  -> 必要时请求人工审批
  -> 执行 read_file/write_file/search/run_command
  -> 将每一步写入事件日志和 SQLite
  -> 在上下文接近上限时进行结构化压缩
  -> 在进程崩溃后恢复未完成 Session
  -> 输出任务结果、修改摘要、测试结果和证据
```

第二阶段增加 Memory、Skills、MCP、子 Agent、HTTP API 和受控自进化。所有阶段都必须维持以下不变量：

1. 模型不能绕过 Runtime 直接访问文件、Shell 或网络。
2. 重要状态先写事件，再由 Reducer 投影成当前状态。
3. 写入、删除、网络和发布等副作用默认需要策略检查，必要时人工审批。
4. 恢复时只自动重放明确声明为幂等的操作。
5. 候选 Memory/Skill 通过评测前不能影响有效版本。
6. 原始事件和证据不能因为压缩而删除。

## 2. 技术基线

### 2.1 运行时和依赖

```text
Python：3.12+
异步：asyncio、TaskGroup、timeout
依赖管理：uv + pyproject.toml
数据模型：Pydantic v2、typing.Protocol、TypedDict
本地存储：SQLite/aiosqlite + append-only JSONL
检索：SQLite FTS5/BM25，后续接向量扩展
模型访问：httpx + Provider Adapter
CLI：Typer
HTTP：FastAPI + Uvicorn
MCP：Python MCP SDK
测试：pytest、pytest-asyncio、Hypothesis、回放测试
质量：Ruff、mypy、pre-commit
```

### 2.2 依赖原则

- 核心状态机只依赖标准库、Pydantic 和项目内部接口，不依赖具体模型供应商。
- 模型供应商 SDK 只能出现在 `model/providers/`，不能把供应商响应对象传入 Agent Loop。
- 工具实现不能直接写事件日志，统一通过 `ToolExecutor` 返回标准化结果。
- 所有外部输入，包括模型文本、工具结果、文件内容、网页内容和 MCP 返回值，都按不可信数据处理。
- 第一版只使用本地 SQLite 和 JSONL，不引入 Redis、消息队列或远程数据库。

## 3. 目标目录结构

```text
atlas-harness/
  pyproject.toml
  README.md
  .env.example
  src/atlas_harness/
    __init__.py
    config.py                 # 配置、环境变量和默认预算
    kernel/
      ids.py                  # Session/Operation/Event/ToolCall ID
      lifecycle.py            # 生命周期、关闭和取消
      errors.py               # 统一错误类型
      clock.py                # 可注入时钟，支持回放测试
    events/
      models.py               # EventRecord 和 payload 模型
      store.py                # JSONL 追加、校验和序号分配
      reducer.py              # 事件到 SessionState 的投影
      subscriptions.py        # 事件订阅
    model/
      protocol.py             # ModelAdapter、ModelEvent、能力声明
      catalog.py               # Provider 和模型目录
      providers/
        openai_compatible.py  # OpenAI 兼容接口
        anthropic.py          # 可选 Provider
    agent/
      state.py                # Agent 状态机
      loop.py                 # 主 Agent Loop
      queues.py               # steer/follow_up/next_run 队列
    tools/
      manifest.py             # 工具声明和风险枚举
      registry.py             # 注册、发现和版本
      executor.py             # 超时、取消、结果标准化
      builtin/
        read_file.py
        write_file.py
        search.py
        run_command.py
    policy/
      engine.py               # Policy preflight
      path_policy.py          # 路径边界和敏感文件
      command_policy.py       # 命令 allowlist/denylist
      network_policy.py       # 域名、方法和速率
      approval.py             # 人工审批接口
    session/
      service.py              # Session、Operation、Lane 服务
      repository.py           # SQLite 索引
      recovery.py             # 快照、恢复和 suspended
      branches.py             # lane 和分支导航
    context/
      compiler.py             # 槽位排序、去重和预算裁剪
      tokens.py               # Token 计算接口和估算器
      compaction.py           # 结构化压缩
    memory/
      models.py               # 四层 Memory 数据模型
      repository.py           # Memory 持久化和检索
      retrieval.py            # BM25/关键词检索
    skills/
      models.py               # Skill metadata 和版本
      loader.py               # 加载、触发和注入
      repository.py           # Skill 来源追踪
    evolution/
      extractor.py            # 反馈提取候选
      merger.py               # 相似 Skill 合并
      evaluator.py            # 规则、LLM Judge、回放评测
      champion.py             # 晋升、回滚和版本比较
    mcp/
      client.py               # MCP 连接和生命周期
      manifests.py            # MCP Server manifest
      isolation.py            # 权限和并发隔离
    subagent/
      contract.py             # 子 Agent 任务合同
      scheduler.py            # 调度、预算和截止时间
    transport/
      cli.py                  # Typer 命令
      http.py                 # FastAPI 路由
    observability/
      trace.py                # Trace/span
      audit.py                # 审计事件
      metrics.py              # 指标
    evals/
      datasets.py             # 固定任务集
      replay.py               # 事件回放
      reports.py              # 评测报告
  apps/
    cli.py
    server.py
    worker.py
  tests/
    unit/
    integration/
    replay/
    security/
    fixtures/
```

## 4. 核心数据合同

### 4.1 EventRecord

事件是系统事实。事件写入成功后才能更新内存中的投影状态。

```python
class EventRecord(BaseModel):
    schema_version: int
    session_id: str
    lane: str
    operation_id: str | None
    seq: int
    event_id: str
    type: EventType
    timestamp: int
    payload: dict[str, object]
```

`EventStore.append()` 必须完成以下检查：

1. `seq` 比上一个事件严格递增。
2. `schema_version` 是当前支持的版本。
3. `event_id` 和幂等键未重复。
4. JSONL 追加和 SQLite 索引写入成功，或整体失败。
5. 写入失败时不能继续执行依赖该事件的副作用。

### 4.2 ModelAdapter

```python
class ModelAdapter(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterable[ModelEvent]: ...
    async def count_tokens(self, input: TokenInput) -> int: ...
    def capabilities(self) -> ModelCapabilities: ...
```

Provider 适配器必须把原始响应转换为以下统一流式事件：

```text
text_delta
thinking_delta
tool_call_started
tool_call_delta
tool_call_completed
message_completed
provider_error
```

### 4.3 ToolManifest

```python
class ToolManifest(BaseModel):
    name: str
    description: str
    input_schema: dict[str, object]
    risk: Literal["read", "write", "network", "destructive"]
    scopes: list[str]
    idempotent: bool
    timeout_ms: int
```

每个工具必须有：

- 输入 Pydantic 模型或等价 JSON Schema。
- 风险等级和所需 scope。
- 是否幂等的明确声明。
- 默认超时和最大输出大小。
- 可审计的工具版本。

## 5. 端到端开发流程

### 5.1 启动阶段：创建或恢复 Session

```text
CLI/API 接收任务
  -> 校验输入和运行预算
  -> 创建 Session、Lane、Operation ID
  -> 追加 operation_started
  -> 读取最近快照
  -> 重放快照之后的事件
  -> 检查是否存在 suspended Operation
```

实现要求：

- 新任务默认创建 `main` lane。
- 恢复任务必须显式传入 `session_id`，不能通过“最后一个文件”推断状态。
- 存在未确认副作用时，状态必须停在 `suspended`，等待 `resume`、`abort` 或人工确认。

### 5.2 上下文编译阶段

```text
加载项目规则和安全策略
  -> 加载 lane 状态
  -> 检索相关 Memory
  -> 预筛和检索相关 Skill
  -> 收集最近消息、工具结果和证据引用
  -> 按固定/任务/能力/短期/证据槽位排序
  -> 去重、脱敏、按 token 预算裁剪
  -> 生成 ModelRequest
```

上下文槽位固定为：

| 槽位 | 内容 | 处理规则 |
|---|---|---|
| 固定 | System Prompt、项目规则、安全策略 | 不允许被工具结果覆盖 |
| 任务 | 目标、约束、已完成步骤、阻塞问题 | 优先保留 |
| 能力 | Memory、Skill、工具说明 | 按相关性和权限过滤 |
| 短期 | 最近消息、最近工具结果、未解决调用 | 按时间倒序截取 |
| 证据 | 路径、差异、日志、外部引用 | 只放引用和必要摘要 |

### 5.3 模型流式阶段

```text
追加 model_requested
  -> 调用 ModelAdapter.stream()
  -> 逐个接收统一 ModelEvent
  -> 临时缓冲 text/tool call delta
  -> 收到 message_completed
  -> 校验完整消息
  -> 追加 assistant_message
```

注意事项：

- 流式中断时追加 `provider_error`，不能把半截工具调用当成完整调用执行。
- 模型文本按不可信内容保存；文本中出现的命令不能改变 Policy。
- 工具调用 ID 缺失、参数 JSON 不完整或重复时，进入 `failed`，并保留原始响应证据。

### 5.4 工具调用阶段

```text
解析 tool_call
  -> ToolRegistry 查找工具
  -> Pydantic/JSON Schema 校验参数
  -> Scope 校验
  -> Policy preflight
  -> 需要审批？
       是 -> 追加 approval_requested -> 等待用户 -> approved/rejected
       否 -> 继续
  -> 追加 tool_started
  -> 使用 asyncio.timeout 执行
  -> 响应取消信号
  -> 标准化、截断、脱敏结果
  -> 追加 tool_result
```

并发规则：

- 互不冲突的只读工具可以并行执行。
- 写文件、Git、网络写入和其他外部副作用默认串行。
- 同一 `lane` 中存在写操作时，禁止与可能读取相同资源的操作无条件并行。
- 每个工具都必须使用 `idempotency_key = session/operation/tool_call`。

### 5.5 运行中队列

三个队列必须分开持久化：

| 队列 | 生效时机 | 示例 |
|---|---|---|
| `steer` | 当前运行中，尽快改变方向 | 用户要求停止修改某文件 |
| `follow_up` | 当前模型步骤完成后 | 用户补充一个检查项 |
| `next_run` | 当前 Operation 完成后 | 用户要求下次继续做文档 |

消费顺序固定为：取消信号 -> steer -> 当前工具结果 -> follow_up -> token 检查 -> 下一次模型请求。

### 5.6 上下文压缩阶段

```text
Token 使用量达到 70%
  -> 标记 compact_pending
达到 85%
  -> 生成结构化压缩对象
达到 95%
  -> 强制压缩、外置大型结果或暂停
  -> 只替换模型上下文
  -> 保留原始事件、差异和证据
```

压缩对象必须至少包含：

```json
{
  "taskProgress": [],
  "currentObjective": "",
  "blockers": [],
  "nextActions": [],
  "decisions": [],
  "toolLessons": [],
  "failedPaths": [],
  "evidenceRefs": [],
  "openQuestions": []
}
```

## 6. 崩溃恢复流程

### 6.1 启动恢复

```text
读取最新 snapshot
  -> 校验 snapshot schema/version
  -> 读取 snapshot.seq 之后的 JSONL 事件
  -> 校验 seq 连续性、event_id 和幂等键
  -> Reducer 重建 SessionState
  -> 找到未完成 Operation、模型步骤和工具调用
```

任何事件校验失败都必须拒绝自动恢复，并输出可定位的错误和最后一个有效 seq。

### 6.2 未完成工具调用处理

| 操作类型 | 恢复行为 |
|---|---|
| 明确幂等的读取或查询 | 可以按原幂等键重放 |
| 写文件、删除、Git 提交 | 进入 `suspended`，等待确认 |
| 发布、支付、外部提交 | 进入 `suspended`，禁止自动重放 |
| 已有 `tool_result` 的调用 | 不重复执行，直接恢复投影 |
| 只有 `tool_started` 的调用 | 按工具声明和副作用等级处理 |

恢复命令至少提供：

```bash
atlas resume <session-id>
atlas resume <session-id> --confirm <tool-call-id>
atlas abort <session-id>
atlas inspect <session-id>
atlas replay <session-id>
```

## 7. 分阶段实施计划

### M0：工程基线和可运行骨架

**目标**：项目能安装、启动、运行单元测试，并具备统一配置和日志入口。

**任务**：

- 初始化 `pyproject.toml`、uv lock、src layout 和测试目录。
- 配置 Ruff、mypy、pytest、pre-commit。
- 建立 `Settings`、日志格式、时钟接口和环境变量加载。
- 定义统一错误层：配置错误、事件错误、策略拒绝、工具错误、Provider 错误、恢复错误。
- 编写 `atlas --help`、版本信息和退出码约定。

**输出**：

- 可执行 `uv run atlas --help`。
- CI 能运行格式检查、类型检查和空测试集。
- `.env.example` 不包含真实密钥。

**完成条件**：新机器按照 README 在 10 分钟内完成安装并通过基线检查。

### M1：Kernel、事件日志和 Reducer

**目标**：建立事件溯源内核，所有后续模块只通过事件改变状态。

**任务**：

- 实现 ID、时间戳、seq、幂等键生成。
- 定义 `EventType` 和各事件 payload 模型。
- 实现 JSONL append-only 写入。
- 实现 SQLite 事件索引和事务边界。
- 实现 `SessionState`、`LaneState`、`OperationState` Reducer。
- 实现事件订阅、关闭和取消。
- 增加内存时钟和故障注入点。

**最小事件集**：

```text
session_created
operation_started
model_requested
assistant_message
approval_requested
approval_resolved
tool_started
tool_result
operation_finished
operation_failed
operation_aborted
snapshot_created
```

**测试**：

- 同一输入事件序列得到相同投影状态。
- seq 重复、缺失、乱序时拒绝写入或恢复。
- JSONL 截断、非法 JSON、schema 版本不支持时返回明确错误。
- 进程在每个事件前后退出，重启后状态可重建。

**完成条件**：不依赖模型即可通过事件回放构造完整 Session 状态。

### M2：Tool Registry、四个内置工具和 Policy

**目标**：在统一权限边界内执行本地工具。

**任务**：

- 实现 `ToolManifest`、注册、发现和版本校验。
- 实现 `read_file`：只允许工作区内路径，限制文件大小。
- 实现 `write_file`：默认需要审批，支持差异预览和幂等键。
- 实现 `search`：工作区范围搜索，过滤密钥和二进制内容。
- 实现 `run_command`：命令解析、超时、工作目录和输出截断。
- 实现路径策略：工作区根、允许读写路径、敏感文件 denylist。
- 实现命令策略：allowlist/denylist、危险参数检查。
- 实现网络策略接口，即使第一版工具暂不开放网络。
- 实现统一结果脱敏和最大输出字节数。

**测试**：

- 路径穿越、符号链接逃逸、`.env` 和 SSH Key 访问均被拒绝或审批。
- `run_command` 的 shell 注入、危险命令和超时被阻断。
- 工具取消后子进程被回收，不能遗留后台进程。
- 只读工具可并行，写工具按串行规则执行。

**完成条件**：四个工具可以被注册、校验、审批、执行、取消、截断并写入标准事件。

### M3：Model Adapter 和 Agent Loop

**目标**：完成一次真实的模型 -> 工具 -> 结果 -> 模型闭环。

**任务**：

- 定义 `ModelRequest`、`ModelEvent`、`ModelCapabilities`。
- 实现一个 OpenAI 兼容 Provider，支持 API key、超时、重试和流式响应。
- 将供应商响应映射为统一流式事件。
- 实现文本-only 完成分支。
- 实现工具调用解析、批量调用和调用失败反馈。
- 接入 steer/follow_up/next_run 队列。
- 在每个循环边界检查取消、deadline、token 和预算。
- 将所有模型请求和响应摘要写入 trace，不记录密钥。

**测试**：

- 使用 fake ModelAdapter 测试确定性 Agent Loop。
- 测试文本回复、单工具调用、多工具调用、非法参数和 Provider 错误。
- 测试模型流中断、重复 tool call ID、超时和取消。
- 使用 HTTP mock 测试 Provider，不依赖真实模型服务。

**完成条件**：本地 CLI 可以完成一次“读取文件并回答问题”的任务，所有步骤均可回放。

### M4：Session、Lane、快照和崩溃恢复

**目标**：进程在模型请求、工具执行和事件写入之间崩溃后，能够恢复且不重复副作用。

**任务**：

- 实现 Session/Lane/Operation 数据表和查询。
- 实现周期性 snapshot，记录最后有效 seq。
- 实现启动恢复和事件校验。
- 区分幂等读取与非幂等写入。
- 实现 suspended、resume、abort 和人工确认。
- 实现 lane 共享只读上下文、隔离可变队列和工具状态。
- 实现分支导航事件，禁止删除历史。

**故障注入矩阵**：

```text
model_requested 前后
assistant_message 前后
tool_started 前后
tool_result 前后
operation_finished 前后
snapshot_created 前后
```

每个注入点都验证：事件完整性、投影状态、工具是否重复执行、恢复命令是否正确。

**完成条件**：恢复测试覆盖全部关键边界；已完成工具不会因为只写回结果失败而被再次执行。

### M5：Context Compiler 和结构化压缩

**目标**：长任务在 token 接近上限时仍能继续，并保留目标、阻塞问题、下一步动作和证据。

**任务**：

- 定义固定、任务、能力、短期、证据五类槽位。
- 实现 token 计数器接口，先提供估算器，再接 Provider 精确计数。
- 实现排序、去重、敏感信息过滤和预算裁剪。
- 实现 70%/85%/95% 阈值策略。
- 实现 `/compact`、模型 `compact` 工具和自动压缩。
- 大型工具输出外置到 artifacts，并在上下文中保留引用。
- 记录压缩原因：`manual`、`threshold`、`overflow`。

**测试**：

- 压缩前后原始事件和 artifacts 完整。
- 压缩后仍保留当前目标、阻塞问题、下一步动作和证据引用。
- 大型日志不会超过单工具输出和总上下文预算。
- 敏感字段在上下文、trace 和审计日志中均被过滤。

**完成条件**：固定长任务经过至少一次自动压缩后仍能继续执行并完成。

### M6：Memory、Skill 和来源追踪

**目标**：让 Agent 使用可检索、可审计、可过期的能力信息。

**任务**：

- 实现 Working、Episodic、Semantic、Procedural 四层 Memory。
- 定义 Memory 的来源任务、创建时间、过期时间、置信度和证据引用。
- 使用 SQLite FTS5/BM25 完成关键词检索。
- 定义 Skill YAML/JSON metadata 和版本状态。
- 实现规则预筛、检索、权限过滤、token 排序和少量注入。
- 记录每次注入了哪些 Memory/Skill 以及未选中的原因。

**测试**：

- 相同任务得到稳定的检索排序。
- 无权限 Skill 不会进入上下文。
- Skill 版本、来源和证据可追踪。
- 过期 Episodic Memory 不会作为长期事实注入。

**完成条件**：固定任务可以检索到相关 Skill，并能在 trace 中解释选择来源。

### M7：Pending Window 和受控自进化

**目标**：从反馈中生成候选 Skill，但不直接改变有效能力。

**任务**：

- 收集用户纠正、任务失败和成功后的反馈。
- 实现 Candidate Extractor 和 evidence binding。
- 检索相似 Skill，执行 add/merge/reject。
- 实现 schema、lint、安全检查。
- 接入固定 benchmark、规则 evaluator、LLM Judge 和 shadow run。
- 实现 champion comparison、promote 和 rollback。
- 记录 pass@1、完成率、工具有效率、成本、安全违规率、回归率和恢复率。

**测试**：

- 候选 Skill 未评测前不会被默认注入。
- 评测失败的候选不能晋升。
- 晋升后可回滚到指定版本。
- 新 Skill 不降低旧任务和安全任务集成绩。

**完成条件**：完整跑通 feedback -> candidate -> evaluate -> promote/rollback 流程。

### M8：MCP、子 Agent、HTTP 和观测台接口

**目标**：在不破坏统一 Registry、Policy 和 Trace 的前提下扩展平台能力。

**任务**：

- 实现 MCP Server manifest、连接、能力清单和关闭流程。
- 将 MCP 工具转换为统一 ToolManifest。
- 实现 MCP 网络、文件、并发、超时和凭证隔离。
- 定义子 Agent 任务合同、allowedTools、maxTokens、deadlineMs 和 returnFormat。
- 子 Agent 只返回结果和证据，不共享主 Agent 的可变 Session、队列和权限。
- 提供 FastAPI Session、Run、Abort、Compact、Events、Trace、Skills 路由。
- 输出 trace.jsonl、audit.jsonl、metrics.json 和 replay-report.json。

**测试**：

- 恶意 MCP 工具不能绕过 Policy。
- 子 Agent 超时、预算耗尽和异常都能回收资源并回写结果。
- HTTP API 与 CLI 使用同一服务层，不复制业务逻辑。
- 审计日志能回答模型、Skill、工具、审批、截断、压缩和恢复问题。

**完成条件**：MCP、子 Agent 和 HTTP 入口都能被同一套回放测试验证。

### M9：稳定性、安全和发布

**目标**：达到第一版内部试用和持续迭代标准。

**任务**：

- 完成全量单元、集成、回放、安全和性能测试。
- 对路径、命令、网络、Prompt Injection 和 Skill Poisoning 做专项检查。
- 增加 SQLite/JSONL 备份、校验和迁移工具。
- 固化事件 schema 版本和向后兼容策略。
- 编写运行手册、故障恢复手册和安全审查记录。
- 打包 CLI，生成版本号、变更记录和可回放样例。

**完成条件**：发布检查清单全部通过，关键风险有明确的监控、暂停和回滚动作。

## 8. 推荐实现顺序

每个模块都按以下顺序开发，避免先写无法验证的抽象：

```text
1. 先写数据合同和失败场景
2. 再写最小实现
3. 用 fake/fault injection 覆盖确定性行为
4. 接入真实文件、SQLite 或 Provider
5. 增加集成和回放测试
6. 增加 trace、metrics 和审计字段
7. 更新 CLI、README 和迁移说明
```

模块依赖顺序：

```text
config/errors/ids
  -> events/store/reducer
  -> tools/registry/executor
  -> policy
  -> model/protocol/provider
  -> agent/loop
  -> session/recovery
  -> context/compaction
  -> memory/skills
  -> evolution
  -> mcp/subagent/transport/observability
```

不得反向依赖：

- `kernel` 不依赖 `agent`、`tools` 或具体 Provider。
- `policy` 不依赖模型输出内容来决定是否允许访问资源。
- `model` 不直接调用工具。
- `tools` 不直接修改 Session 投影。
- `memory/skills` 不直接晋升自身版本。
- `transport` 只调用应用服务，不复制业务流程。

## 9. CLI 开发顺序

### M0 命令

```bash
atlas --help
atlas version
```

### M1-M4 命令

```bash
atlas run "读取项目结构并说明入口"
atlas sessions
atlas inspect <session-id>
atlas resume <session-id>
atlas abort <session-id>
atlas replay <session-id>
```

### M5-M7 命令

```bash
atlas compact <session-id>
atlas memory search "事务配置"
atlas skills list
atlas skills show <skill>
atlas skills evaluate <candidate-id>
atlas skills rollback <skill> --to <version>
```

### M8-M9 命令

```bash
atlas mcp list
atlas mcp inspect <server>
atlas eval run <dataset>
atlas audit <session-id>
atlas doctor
```

所有命令都必须支持：明确退出码、结构化错误、`--json` 输出和不泄露密钥。

## 10. SQLite 数据设计

第一版至少包含以下表：

```text
sessions(
  id, created_at, updated_at, status, workspace_root, schema_version
)
lanes(
  session_id, lane, parent_lane, status, created_at, updated_at
)
operations(
  id, session_id, lane, status, started_at, finished_at, deadline_ms
)
events(
  session_id, lane, seq, event_id, operation_id, type, timestamp, payload_json
)
tool_calls(
  tool_call_id, operation_id, tool_name, idempotency_key, risk, status
)
snapshots(
  session_id, lane, seq, path, checksum, created_at
)
memories(
  id, layer, content, source_task_id, confidence, expires_at, evidence_json
)
skills(
  name, version, status, metadata_json, source_json, created_at
)
artifacts(
  id, session_id, operation_id, kind, path, checksum, size
)
```

约束：

- `(session_id, lane, seq)` 唯一。
- `event_id` 全局唯一。
- `idempotency_key` 在同一 Operation 内唯一。
- 大型 payload 不直接塞入事件，事件只保存 artifact 引用。
- 删除 Skill、Memory 和事件前必须有显式管理命令、审计记录和备份策略。

## 11. 测试策略

### 11.1 测试层级

| 层级 | 目标 | 运行时机 |
|---|---|---|
| 单元测试 | 数据模型、Reducer、Policy、压缩、检索 | 每次提交 |
| 属性测试 | seq、幂等、队列和状态转换不变量 | 每次提交 |
| 集成测试 | SQLite、JSONL、工具、Provider mock | 每次提交 |
| 回放测试 | 事件序列重建和崩溃恢复 | 每次提交 |
| 安全测试 | 路径、命令、注入、敏感信息 | 每次提交和发布前 |
| 端到端测试 | CLI 到结果、审批和证据 | 发布前 |
| 性能测试 | 大日志、长 Session、并发只读工具 | 里程碑结束 |

### 11.2 必测场景

```text
正常文本回答
正常单工具调用
多个只读工具并行
写文件审批通过
写文件审批拒绝
工具参数错误
工具超时
工具取消
Provider 流式中断
事件写入失败
模型请求前崩溃
工具执行中崩溃
工具完成但结果未写回时崩溃
快照损坏
事件 seq 缺失
上下文 70/85/95% 阈值
敏感文件访问
危险命令访问
Prompt Injection 工具结果
候选 Skill 评测失败
Champion 回滚
MCP 工具越权尝试
子 Agent 超时
```

### 11.3 每个 PR 的检查

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest tests/unit tests/integration
uv run pytest tests/replay tests/security
```

## 12. 验收标准

### 12.1 M0-M4：可靠闭环

- CLI 能完成 Coding Agent Demo。
- 四个内置工具经过 Schema、Scope、Policy 和审批流程。
- 每个重要状态变化都有事件记录。
- 崩溃后可由快照和事件重建状态。
- 幂等读取不会被重复执行，非幂等副作用恢复后进入 `suspended`。
- `atlas replay` 能生成与原运行一致的状态和事件摘要。

### 12.2 M5-M7：长程和能力演化

- 70% 开始准备压缩，85% 自动压缩，95% 强制压缩或暂停。
- 压缩后保留目标、阻塞问题、下一步、决定和证据引用。
- 四层 Memory 和版本化 Skill 可检索、可追踪、可过期。
- Candidate 必须绑定来源和证据，评测通过后才能晋升。
- Champion 回滚后行为回到指定版本。

### 12.3 M8-M9：平台和治理

- MCP 工具进入统一 Registry、Policy、Trace 和审计。
- 子 Agent 使用显式任务合同并受 token、时间和工具预算限制。
- CLI 和 HTTP API 共享同一应用服务。
- 能生成 trace、audit、metrics 和 replay report。
- 安全测试和旧任务回归测试没有新增失败。

## 13. Demo 交付流程

第一个可演示任务固定为“给项目增加一个接口，并补充测试”：

```text
1. 用户执行 atlas run
2. 创建 Session/main lane/Operation
3. 读取项目规则和目录
4. 检索相关 Skill
5. 读取目标文件
6. 模型生成修改计划
7. Runtime 预检 write_file
8. CLI 展示差异并请求批准
9. 写入代码和测试
10. 执行测试命令
11. 失败时记录 failedPaths 并继续修复
12. 达到 token 阈值时结构化压缩
13. 追加 operation_finished
14. 输出修改摘要、测试结果、文件差异和事件证据
```

Demo 必须演示一次审批、一次工具结果截断或外置、一次测试失败后的继续修复，并提供一份可重复回放的 Session fixture。

## 14. 发布和迭代节奏

### 每个里程碑结束

1. 冻结本阶段事件 schema 和数据迁移。
2. 执行全量回放测试。
3. 运行安全检查和旧任务回归集。
4. 更新 README、CLI help 和运维说明。
5. 记录已知限制，不用未完成功能掩盖失败。

### 发布前

- 备份并校验 SQLite、JSONL 和 artifacts。
- 验证从上一版本恢复 Session 的兼容性。
- 验证所有副作用工具的审批和 suspended 流程。
- 检查日志、trace、prompt 和异常中没有 API key、Cookie、私钥或完整敏感文件。
- 使用固定模型参数和固定数据集生成基线报告。

## 15. 主要风险和应对

| 风险 | 触发信号 | 应对 |
|---|---|---|
| Prompt Injection | 工具结果要求改变权限或执行额外命令 | 标记不可信，重新经过 Policy，拒绝隐式授权 |
| Replay Side Effect | 恢复时存在未完成写操作 | 幂等键、suspended、人工确认、审计 |
| Context Loss | 压缩后找不到文件位置或失败原因 | 结构化字段 + evidenceRefs + 原始事件保留 |
| Skill Poisoning | 候选 Skill 只在单任务有效 | pending window、固定 benchmark、回滚 |
| SQLite/JSONL 不一致 | 索引 seq 与日志 seq 不同 | 启动校验、重建索引、备份和迁移工具 |
| Provider 不稳定 | 流式断连、速率限制、格式变化 | Adapter 隔离、超时、重试、fake 测试 |
| 过度并发 | 工具结果顺序不稳定或读到中间状态 | 只读白名单并行，写操作串行 |
| 范围膨胀 | M0 还未稳定就开始平台能力 | 里程碑门禁，未通过验收不得进入下一阶段 |

## 16. 最终完成定义

当以下条件同时满足时，Python 第一版视为完成：

```text
单进程 CLI 可运行
  + 一个真实 Model Provider 和一个 fake Provider
  + read_file/write_file/search/run_command
  + Schema、Scope、Policy、审批、超时、取消
  + JSONL 事件日志、SQLite 索引、snapshot、lane
  + 崩溃恢复、幂等重放、suspended/resume/abort
  + token 预算和结构化压缩
  + Memory/Skill 检索和来源追踪
  + Candidate 评测、Champion 晋升和 Rollback
  + MCP、子 Agent、HTTP 和审计接口
  + 单元、集成、回放、安全、端到端测试
```

任何未满足项都必须在发布说明中明确标为限制，而不是改变原方案的功能定义。
