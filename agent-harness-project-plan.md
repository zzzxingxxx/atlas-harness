# AtlasHarness：可恢复、可治理、可进化的 Agent Harness

> 本文是项目设计方案。名称 `AtlasHarness` 暂定，可替换。

研究结论见：[agent-harness-research-summary.md](./agent-harness-research-summary.md)。

## 1. 项目目标

AtlasHarness 是一个模型无关、工具可插拔、会话可恢复、能力可演化的 Agent Runtime。

它不是聊天 UI，也不是简单的 Workflow 编排器。它的核心职责是：

```text
接收任务
  -> 让模型提出计划和工具调用
  -> 对工具调用进行验证、审批和执行
  -> 保存每一步状态
  -> 在长程任务中压缩上下文
  -> 在失败或崩溃后恢复
  -> 从反馈中形成候选 Memory/Skill
  -> 通过评测后再让新能力生效
```

第一阶段重点支持 Coding Agent 和本地知识任务，后续扩展到运维、研究、数据分析和内部业务工具。

## 2. 设计原则

### 2.1 Model 和 Runtime 分离

模型只负责推理和提出动作，Runtime 负责执行、权限、状态、恢复和评测。模型不能绕过 Runtime 直接访问文件、Shell 或外部网络。

### 2.2 事件是事实，状态是投影

所有重要状态变化先写入事件日志，再由 Reducer 投影为当前状态。当前状态可以重建，事件不能被静默覆盖。

### 2.3 副作用默认保守

读取可以自动化，写入、删除、网络、发布、支付和外部 API 操作按照风险分级审批。

### 2.4 能力变化必须可回滚

Memory 和 Skill 采用候选版本，不允许模型直接修改有效版本。候选版本先评测，再决定是否晋升。

### 2.5 先做内核，再做平台

先完成单进程 CLI 和本地存储，再增加 Web、远程执行、插件市场和多租户能力。

## 3. 技术选型

### 3.1 推荐方案

```text
Runtime：TypeScript 5.x / Node.js 22+
Schema：Zod 或等价运行时校验库
持久化：SQLite + append-only JSONL
向量检索：先 BM25，后接 SQLite 向量扩展或独立向量库
MCP：标准 MCP Client/Server
CLI：Node CLI，后续接 TUI
HTTP：Fastify 或等价轻量 HTTP 层
测试：Vitest + 集成测试 + 回放测试
```

选择 TypeScript 是因为 Pi 和 DeepSeek Harness 都有可借鉴的类型化运行时设计。Python 可以作为评测、数据分析和特定工具的 Worker，不放在核心状态机中。

### 3.2 目录结构

```text
packages/
  kernel/          生命周期、事件、错误、取消
  model/           Provider、模型目录、流式协议
  agent/           Agent Loop 和运行状态机
  tools/           Tool Manifest、Registry、Executor
  policy/          权限、审批、路径、命令、网络策略
  session/         JSONL、SQLite、分支、lane、恢复
  context/         Token 预算、上下文编译、压缩
  memory/          Working/Episodic/Semantic/Procedural Memory
  skills/          Skill 加载、触发、注入、版本
  evolution/       Extractor、Merger、Evaluator、Champion
  mcp/             MCP 连接、能力清单、隔离
  subagent/        子 Agent 调度、预算、结果
  transport/       CLI、HTTP、WebSocket、SSE
  observability/   Trace、事件订阅、指标、审计
  evals/           数据集、任务回放、评测报告
apps/
  cli/
  server/
  worker/
```

## 4. 核心模块职责

### 4.1 Kernel

负责：

- 生成 Session、Operation、ToolCall、Event ID。
- 管理生命周期和取消信号。
- 统一错误类型。
- 提供事件订阅和关闭流程。
- 为所有异步操作设置超时和预算。

Kernel 不应该知道具体模型和具体业务工具。

### 4.2 Model Adapter

统一不同模型供应商的差异：

```ts
interface ModelAdapter {
  stream(request: ModelRequest): AsyncIterable<ModelEvent>;
  countTokens(input: TokenInput): Promise<number>;
  capabilities(): ModelCapabilities;
}
```

内部流式事件统一为：

```text
text_delta
thinking_delta
tool_call_started
tool_call_delta
tool_call_completed
message_completed
provider_error
```

Agent Loop 不能直接依赖 OpenAI 或 Anthropic 的原始响应对象。

### 4.3 Tool Registry 和 Executor

每个工具包含：

```ts
interface ToolManifest {
  name: string;
  description: string;
  inputSchema: JsonSchema;
  risk: "read" | "write" | "network" | "destructive";
  scopes: string[];
  idempotent: boolean;
  timeoutMs: number;
}
```

调用流程：

```text
发现工具
  -> Schema 校验
  -> Scope 校验
  -> Policy preflight
  -> 用户审批（必要时）
  -> before_tool hook
  -> 执行
  -> 超时/取消处理
  -> 结果截断和脱敏
  -> after_tool hook
  -> 写入 tool_result
```

工具结果必须限制大小，避免单个日志、网页或文件把上下文全部占满。

### 4.4 Policy

权限策略至少覆盖：

- 工作区根目录。
- 允许读取和写入的路径。
- `.env`、密钥、SSH Key 等敏感文件。
- Shell 命令 allowlist/denylist。
- 网络域名和 HTTP 方法。
- 工具调用次数、运行时间和资源预算。
- 是否需要人工审批。

工具输出也属于不可信输入，不能因为内容里出现“请执行某命令”就自动提升权限。

## 5. Agent Loop 设计

### 5.1 状态机

```text
created
  -> planning
  -> awaiting_model
  -> tool_preflight
  -> awaiting_approval
  -> executing_tool
  -> tool_result
  -> compacting
  -> completed

任意阶段可以进入：
  failed / aborted / suspended
```

### 5.2 运行伪代码

```text
run(input):
  append operation_started
  load lane state
  retrieve memory
  select skills
  compile context

  while true:
    append model_requested
    stream model events
    persist assistant message

    if response is text-only:
      append operation_finished(completed)
      return

    for toolCall in response.toolCalls:
      validate toolCall
      run policy preflight
      request approval if needed
      append tool_started
      execute with timeout and cancellation
      normalize, truncate and redact result
      append tool_result

    consume steering/follow-up queue

    if token budget is near limit:
      compact structured state

    continue
```

工具并行执行只对互不冲突的只读工具开放。写文件、Git 操作和具有外部副作用的工具默认串行。

### 5.3 Steering、Follow-up 和 Next Run

需要区分三种消息：

- `steer`：当前运行中立即改变方向。
- `followUp`：当前模型步骤完成后追加任务。
- `nextRun`：当前 Operation 完成后，作为下一次运行的输入。

这三个队列都必须持久化，避免进程重启后丢失用户意图。

## 6. Session 和持久化

### 6.1 数据层

```text
events.jsonl       追加式事实日志
snapshots/         周期性状态快照
session.db         SQLite 索引和查询数据
artifacts/         大型工具输出、文件差异、评测结果
```

### 6.2 事件格式

```json
{
  "schemaVersion": 1,
  "sessionId": "sess_123",
  "lane": "main",
  "operationId": "op_456",
  "seq": 42,
  "eventId": "evt_789",
  "type": "tool_started",
  "timestamp": 1780000000000,
  "payload": {
    "toolCallId": "call_001",
    "toolName": "read_file",
    "idempotencyKey": "sess_123/op_456/call_001"
  }
}
```

事件写入必须具备：

- 单调序号。
- schemaVersion。
- 事务或原子追加。
- 校验失败时拒绝恢复。
- 事件 ID 和幂等键。

### 6.3 Lane 和分支

```text
Session
  ├─ main
  ├─ research
  ├─ review
  └─ subagent-xxx
```

Lane 可以共享只读项目上下文，但不能默认共享可变队列和工具状态。分支导航应该产生新事件，而不是删除历史。

### 6.4 崩溃恢复

恢复流程：

1. 读取最近快照。
2. 读取快照之后的事件。
3. Reducer 重建当前 lane。
4. 找到未完成的 Operation、模型步骤和工具调用。
5. 只自动重放声明为幂等的操作。
6. 对写文件、删除、发布和外部提交等副作用操作进入 suspended 状态。
7. 提供 `resume`、`abort` 或人工确认入口。

绝不能简单地重放“最后一个事件”，因为工具可能已经完成，只是结果还没来得及写回。

## 7. Context Compiler 和三级压缩

### 7.1 上下文槽位

```text
固定槽位：System Prompt、项目规则、安全策略
任务槽位：目标、约束、已完成步骤、阻塞问题
能力槽位：Memory、Skills、工具说明
短期槽位：最近消息、最近工具结果、未解决调用
证据槽位：文件路径、差异、日志和外部引用
```

Context Compiler 负责排序、去重、预算裁剪和敏感信息过滤，而不是把所有内容简单拼接起来。

### 7.2 压缩对象

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

### 7.3 触发策略

```text
70%：开始准备压缩
85%：自动执行压缩
95%：强制压缩、截断大型结果或暂停
```

支持三种入口：

- 用户命令 `/compact`。
- 模型调用 `compact` 工具。
- Runtime 根据 token 阈值自动触发。

压缩只改变“送给模型的上下文”，不能删除原始事件和证据。

## 8. Memory 和 Skills

### 8.1 四层 Memory

| 层级 | 内容 | 生命周期 |
|---|---|---|
| Working | 当前任务临时状态 | 当前 Operation |
| Episodic | 某次任务和结果 | 跨任务，可过期 |
| Semantic | 用户、项目和领域事实 | 长期 |
| Procedural | 成功路径、失败经验和策略 | 长期，可评测 |

### 8.2 Skill 格式

```yaml
name: fix-spring-transaction
description: 修复 Spring 事务失效问题
triggers:
  - 事务不生效
preconditions:
  - 项目使用 Spring
steps:
  - 检查代理对象
  - 检查方法可见性
  - 检查异常类型
evidence:
  - taskId: task-001
confidence: 0.82
version: 3
status: champion
```

### 8.3 Skill 注入

```text
规则预筛
  -> BM25/关键词检索
  -> 向量检索
  -> 任务类型和权限过滤
  -> 按 token 预算排序
  -> 注入少量高相关 Skill
```

默认不把全部 Skill 放入 System Prompt。工具和 Skill 过多会降低模型选择质量并浪费上下文。

## 9. Self-Evolution 设计

### 9.1 Pending Window

用户纠正、任务失败、任务成功后的反馈，不立即写入有效 Skill，而是进入 pending window：

```text
feedback
  -> candidate extraction
  -> evidence binding
  -> similar skill retrieval
  -> add / merge / reject
  -> candidate version
```

候选对象至少包含：

- 来源任务。
- 原始反馈。
- 触发条件。
- 建议步骤。
- 适用范围。
- 禁止范围。
- 证据引用。
- 置信度。

### 9.2 评测和晋升

```text
candidate
  -> schema/lint/security check
  -> fixed benchmark
  -> rule evaluator
  -> LLM judge
  -> shadow run
  -> champion comparison
  -> promote or rollback
```

晋升不能只看一个任务的成功率。至少要比较：

- pass@1。
- 任务完成率。
- 工具调用有效率。
- Token 和时间成本。
- 安全违规率。
- 旧任务回归率。
- 失败后恢复率。

高风险 Skill、权限规则和系统 Prompt 必须人工审批。

## 10. MCP 和子 Agent

### 10.1 MCP

每个 MCP Server 使用 manifest 描述：

- 提供哪些工具。
- 需要哪些权限。
- 访问哪些网络和文件。
- 最大并发、超时和速率。
- 凭证由谁提供。

MCP 工具必须进入统一 Tool Registry，不能绕过 Policy 和 Trace。

### 10.2 子 Agent

子 Agent 使用显式任务合同：

```json
{
  "task": "分析认证模块",
  "allowedTools": ["read_file", "search"],
  "maxTokens": 12000,
  "deadlineMs": 120000,
  "returnFormat": "findings_with_evidence"
}
```

子 Agent 返回结果和证据，不默认共享主 Agent 的可变 Session、队列和权限。

## 11. 可观测性和审计

每次运行要能回答：

- 使用了哪个模型和 Skill？
- 模型提出了哪些工具调用？
- 哪些工具被批准、拒绝或修改？
- 工具结果是否被截断或脱敏？
- 什么时候发生了压缩？
- 哪个 Skill 导致了任务成功或失败？
- 发生崩溃后是否重放过操作？

建议输出：

```text
trace.jsonl
audit.jsonl
metrics.json
replay-report.json
```

## 12. API 和 CLI 草案

### 12.1 CLI

```bash
atlas run "修复这个项目的测试失败"
atlas resume <session-id>
atlas sessions
atlas compact <session-id>
atlas inspect <session-id>
atlas skills list
atlas skills evaluate <candidate-id>
atlas skills rollback <skill> --to <version>
atlas replay <session-id>
```

### 12.2 HTTP

```text
POST /v1/sessions
POST /v1/sessions/:id/runs
POST /v1/sessions/:id/abort
POST /v1/sessions/:id/compact
GET  /v1/sessions/:id/events
GET  /v1/sessions/:id/trace
GET  /v1/skills
POST /v1/skills/candidates/:id/evaluate
POST /v1/skills/:name/rollback
```

## 13. 开发路线

### M0：最小闭环

- 一个模型 Provider。
- `read_file`、`write_file`、`search`、`run_command` 四个工具。
- Schema 校验、基础审批和 CLI。
- 完成一次可重复的 Agent Loop。

### M1：可靠 Session

- JSONL 事件日志。
- SQLite 索引。
- Session Tree 和 lane。
- 崩溃恢复和基本回放。

### M2：安全执行

- 路径策略。
- 危险命令策略。
- 工具超时、取消和幂等键。
- 统一审计日志。

### M3：长程任务

- Token 计算。
- 三级结构化压缩。
- 大型工具结果外置。
- manual/threshold/overflow 三种压缩原因。

### M4：Memory 和 Skills

- 四层 Memory。
- Skill metadata 和版本。
- BM25 检索。
- Skill 注入和来源追踪。

### M5：受控自进化

- Pending Window。
- Candidate Extractor。
- Add/Merge/Reject。
- 规则评测、LLM Judge、Champion 和 Rollback。

### M6：平台能力

- MCP Server 管理。
- 子 Agent。
- HTTP/WebSocket。
- Web 观测台。
- 多 Provider 和远程 Worker。

## 14. 验收指标

### 可靠性

- 进程在模型请求、工具执行和写入事件之间崩溃后，可以重建状态。
- 幂等工具不会因恢复被重复执行。
- 非幂等工具恢复后会进入人工确认。

### Agent 能力

- 固定任务集 pass@1。
- 工具参数有效率。
- 任务完成率。
- 失败后的恢复率。

### 长上下文

- 压缩前后保留当前目标、阻塞问题和下一步动作。
- 压缩后能够继续完成原任务。
- 统计 Token 减少量、压缩耗时和错误率。

### 自进化

- 每个候选 Skill 都有来源和版本。
- Champion 晋升有评测记录。
- 回滚后行为恢复到指定版本。
- 新 Skill 不降低安全指标和旧任务成绩。

## 15. 主要风险

### Prompt Injection

文件、网页、日志和 MCP 返回值都可能包含恶意指令。工具结果必须标记为不可信数据，不能自动改变权限。

### Skill Poisoning

用户反馈和任务结果可能诱导 Agent 学到错误规则。候选 Skill 必须绑定证据、限定适用范围，并通过固定数据集验证。

### Replay Side Effect

恢复时重复执行写文件、删除、提交、发布或支付操作会造成真实损失。必须使用幂等键和 suspended 状态。

### Context Loss

压缩摘要可能丢失精确文件位置、失败原因和未解决工具调用。压缩状态必须包含证据引用，原始事件必须保留。

### Over-Engineering

不要第一天就复制 DeepSeek Harness 的所有插件和服务。先实现单进程、单 Session、少量工具，再通过真实扩展需求增加抽象。

## 16. 推荐的第一个 Demo

实现一个本地 Coding Agent，完成以下任务：

```text
用户：给项目增加一个接口，并补充测试。

Agent：
1. 检查项目结构。
2. 检索项目规则和相关 Skill。
3. 读取目标文件。
4. 生成修改计划。
5. 请求用户批准写文件。
6. 写入代码和测试。
7. 运行测试。
8. 如果失败，记录失败路径并继续修复。
9. 上下文达到阈值时执行结构化压缩。
10. 返回修改摘要、测试结果和证据。
```

这个 Demo 同时验证 Agent Loop、工具权限、Session、恢复、压缩、Skill 注入和可观测性，适合作为项目主线。

## 17. 最终架构决策

AtlasHarness 的第一版采用以下组合：

```text
Pi：Provider、流式协议、Agent Loop、Session Tree
DeepSeek Harness：Event、Plugin 边界、Cancellation、Lifecycle
BearAgent：三级压缩、Memory、Skills、Candidate/Champion 演化

AtlasHarness：
  Typed Agent Loop
  + Event-sourced Session
  + Policy-governed Tools
  + Lane/Subagent
  + Structured Context Compaction
  + Versioned Memory/Skills
  + Evaluation and Rollback
```

项目是否真正有深度，不取决于接入了多少工具，而取决于能否证明：

1. Agent 崩溃后可以恢复。
2. 工具副作用不会被无意重放。
3. 上下文压缩后仍能继续任务。
4. 新能力经过评测才生效。
5. 每一次模型决策和工具执行都可以回放和审计。
