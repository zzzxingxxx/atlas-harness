# Agent Harness 研究总结

> 研究对象：Pi Coding Agent / `pi-mono`、DeepSeek Harness、BearAgent/Bear Code（小红书文章）以及同作者公开的相邻项目。
>
> 研究目标：提炼 Agent Harness 的核心机制，为自研项目提供架构和实现依据。

配套实施方案见：[agent-harness-project-plan.md](./agent-harness-project-plan.md)。

## 1. 结论先行

Agent Harness 不是“LLM + 几个工具”，而是包围模型的运行时系统。模型负责提出下一步思路和工具调用，Harness 负责把思路变成受控、可恢复、可观测、可评测的执行过程。

一个完整的 Harness 至少需要处理：

1. 模型适配和流式响应。
2. Agent Loop 和工具调用。
3. 权限、审批、沙箱和副作用控制。
4. 会话持久化、分支、恢复和重放。
5. Token 预算和上下文压缩。
6. Memory、Skills 和能力检索。
7. 子 Agent 和任务编排。
8. Trace、评测、回归和版本治理。

Pi 证明了“核心循环可以很小”；DeepSeek Harness 证明了“运行时治理和插件化可以独立成核”；BearAgent 证明了“上下文、Memory、Skills、自进化才是长期任务的主要差异化”。

我们的项目应当将三者组合为：

```text
Pi 的简洁 Agent Loop
+ DeepSeek Harness 的事件、插件和生命周期治理
+ BearAgent 的结构化压缩、Memory、Skills 和受控自进化
```

## 2. 资料和证据边界

### 2.1 Pi

- 项目：[badlogic/pi-mono](https://github.com/badlogic/pi-mono)
- 研究重点：`pi-ai`、`pi-agent-core`、`pi-coding-agent` 和 AgentHarness v2。
- 可验证结论：Provider 抽象、流式事件、Agent Loop、工具执行、steering/follow-up、JSONL 会话树、扩展、压缩和重试。
- 注意事项：分析的 AgentHarness v2 代码包含完整的类型、记录和 reducer 设计，但部分公开方法仍是 scaffold，不能把它当作已经完全实现的生产运行时。

### 2.2 DeepSeek Harness

- 项目：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
- 研究重点：Cordis Plugin Kernel、Service、Event、Profile/Bundle、Session、Tool Pipeline、Cancellation、Subagent。
- 可验证结论：它更接近可插拔 Runtime Kernel，而不是单一 Coding Agent。

### 2.3 BearAgent / Bear Code

- 页面一：[一个适合秋招和社招的 agent 项目](https://www.xiaohongshu.com/explore/6a72c74600000000250165db)
- 页面二：[我做了一个更适合长程任务的 Agent](https://www.xiaohongshu.com/explore/6a771fcc000000002202f402)
- 页面明确提到：约 8,000 行代码、MCP、Skills 自动注入、Memory、三级上下文压缩、子 Agent、自进化和评测。
- 第二篇声称在两个数据集的 665 道任务上测试并使用 pass@1；该数据和配置没有在页面中提供完整的可复现实验材料。
- 截至本次检索，没有找到作者公开的 BearAgent 源码仓库。因此 BearAgent 的具体类、存储格式、并发模型和恢复逻辑不能当作源码审计结论。

### 2.4 同作者公开相邻项目

- [girlfriend.skill](https://github.com/juanjuandog/girlfriend.skill)：展示了 Skill 文件化、Memory、Correction、版本快照、diff 和 rollback 的设计风格。
- [OpsPilot](https://github.com/juanjuandog/OpsPilot)：展示了 Spring AI、MCP Tool Registry、Planner/Executor/Supervisor、Milvus RAG、会话摘要和 SSE 流式接口。
- 这些仓库是可验证的相邻实现，不等同于 BearAgent 源码。

## 3. Pi 的核心机制

### 3.1 分层架构

```text
pi-ai
  Provider / Model / Auth / Streaming

pi-agent-core
  Agent Loop / AgentMessage / Tool Lifecycle

pi-coding-agent
  AgentSession / SessionManager / Compaction / Retry
  Extension / TUI / RPC / Coding Tools
```

### 3.2 Agent Loop

Pi 的循环可以抽象为：

```text
构造 system prompt 和上下文
  -> 请求模型并读取流式事件
  -> 写入 assistant message
  -> 提取 tool call
  -> 工具 preflight
  -> 执行工具
  -> 写入 tool result
  -> 处理 steering/follow-up
  -> 继续请求模型或结束
```

它的价值在于把模型协议和执行协议分开。Provider 的差异被归一成统一的流式事件，Agent Loop 不需要知道具体使用 OpenAI、Anthropic 还是其他模型。

### 3.3 Session Tree

稳定版 Coding Agent 使用追加式 JSONL 和 `parentId/leafId` 维护会话树。这样可以：

- 追加写入，降低损坏风险。
- 从任意历史节点创建分支。
- 重新打开会话。
- 对不同分支进行压缩和导航。
- 在 UI 中显示历史路线。

### 3.4 AgentHarness v2 的重要思想

v2 设计引入了：

- lane：一个会话中多个独立执行通道。
- durable operation：把一次运行、压缩、导航记录成持久化操作。
- reducer：从记录恢复当前状态。
- step attempt：记录模型步骤、压缩步骤和分支摘要尝试。
- tool started/result：建立工具调用和结果的严格对应关系。
- replay policy：区分可以安全重放和不能自动重放的操作。

这套设计比单纯保存对话更适合崩溃恢复，但实现成本更高。

### 3.5 Pi 的优势和局限

优势：核心小、模型适配清晰、工具循环成熟、会话树实用、扩展入口明确。

局限：Memory 和自进化不是核心默认能力；多插件治理不如 DeepSeek Harness；AgentHarness v2 在分析版本中仍有未实现接口。

## 4. DeepSeek Harness 的核心机制

### 4.1 Kernel 思想

DeepSeek Harness 把运行时组织成 Plugin Kernel：

```text
Kernel
  -> Service
  -> Event Bus
  -> Plugin
  -> Profile / Bundle
  -> Session
  -> Tool Pipeline
  -> Transport
```

模块不应该互相直接调用所有实现，而是通过服务、事件和生命周期连接。插件可以注册工具、监听事件、改变配置或提供新的运行能力。

### 4.2 Tool Pipeline

工具调用不只是 `execute()`，而是一个生命周期：

```text
发现工具
  -> 参数校验
  -> 权限检查
  -> before hook
  -> 执行
  -> 取消或超时处理
  -> 结果标准化
  -> after hook
  -> 追踪和审计
```

### 4.3 Session 和 Cancellation

DeepSeek Harness 更强调事件化 Session 和运行生命周期。取消不是简单地杀掉一个 Promise，而是要让模型请求、工具进程、子 Agent、流式传输和状态记录一起进入一致的终止状态。

### 4.4 优势和局限

优势：插件化、生命周期完整、取消和扩展能力强，适合平台化。

局限：内核概念多，学习成本和调试成本高；如果项目早期没有明确扩展需求，直接复制全部 Kernel 会造成过度设计。

## 5. BearAgent 的机制总结

### 5.1 定位

BearAgent 试图处于 Claude Code 和 Pi Agent 之间：保留 Coding Agent 的工具、权限和文件编辑能力，同时缩小代码规模，让项目可以作为个人的 Agent 基架。

### 5.2 预计运行链路

```text
CLI / API
  -> Agent.chat()
  -> Context Compiler
  -> Model Adapter
  -> Tool Call Parser
  -> Permission / Edit Guard
  -> Built-in Tool 或 MCP Tool
  -> Tool Result
  -> Session Store
  -> 继续 Loop 或结束
```

### 5.3 上下文三级压缩

```text
Level 1：任务进度和关键事件
Level 2：当前目标、阻塞问题、下一步动作
Level 3：工具使用经验和失败路径
```

页面同时支持模型主动 compact 和接近阈值自动 compact。设计重点是保留“接下来怎么做”，而不是只生成历史摘要。

### 5.4 自进化

```text
用户反馈
  -> pending window
  -> Extractor 提取候选能力
  -> 检索相似 Skill
  -> Add / Merge
  -> 生成新 Skill 版本
  -> 规则评测 + LLM Judge + 候选试跑
  -> Champion 晋升或回滚
```

最重要的原则是：候选能力必须经过证据、评测和版本治理，不能让模型直接覆盖生产 Skill。

## 6. 三者的综合比较

| 维度 | Pi | DeepSeek Harness | BearAgent | 我们的取舍 |
|---|---|---|---|---|
| 核心定位 | 精简 Coding Runtime | 插件化 Runtime Kernel | 轻量 Coding Harness | 可恢复、可治理的 Agent Runtime |
| Agent Loop | 成熟、简洁 | 被 Kernel 和服务包围 | 参考 Pi，偏 Python | 采用 Pi Loop，增加显式状态机 |
| Session | JSONL 树 | 事件化 Session | 页面强调保存恢复，细节未知 | JSONL 事件 + SQLite 索引 + Reducer |
| 并发 | lane 是 v2 方向 | Plugin/Service 生命周期 | 子 Agent | lane + 子 Agent budget |
| Tool | Tool + Extension | Pipeline + Plugin | MCP、权限、编辑保护 | Manifest + Policy + Executor |
| Compaction | 摘要和上下文压缩 | Runtime 能力 | 三级结构化状态 | 结构化状态 + 原始证据引用 |
| Memory | 非默认中心能力 | 可通过插件扩展 | 核心卖点 | 四层 Memory |
| Skill | 资源/扩展 | 插件和配置 | 自动注入和演化 | 检索、版本、评测、回滚 |
| 自进化 | 非核心 | 非默认中心 | 核心卖点 | 候选、影子运行、Champion |
| 适合学习 | 高 | 中 | 高 | 分阶段实现，不复制全部内核 |

## 7. 最终判断

真正值得学习的不是某个 Prompt，而是以下四个运行时问题：

1. 工具调用发生副作用时，如何审批和保护。
2. 上下文被压缩后，如何保持任务可继续执行。
3. Agent 崩溃或取消后，如何恢复而不重复副作用。
4. Agent 的能力变化后，如何评测、晋升和回滚。

这四点应当成为自研项目的主线。
