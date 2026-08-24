# 变更记录

版本号形式为 `0.M.0`，M 是里程碑序号。`0.` 前缀表示公开接口仍可能变化；固定下来的只有事件 schema 的向后兼容承诺，见每个版本的 schema 一节。

事件 schema 的策略是**只进不改**：新版本可以新增事件类型和带默认值的字段，不可以重定义或移除已有的。所以任何受支持版本写下的日志在今天的构建上原样可读，`SUPPORTED_SCHEMA_VERSIONS` 只增不减。这条承诺由 `src/atlas_harness/events/compat.py` 声明、由 `samples/` 里每个版本一份的冻结日志证明。

## 0.9.0 — 稳定性、安全和发布（M9）

Schema 版本 7，未变。M9 不新增事件类型，这是刻意的：一个稳定性里程碑如果自己改了数据合同，它交付的正是它要消除的风险。

### 新增

- `atlas verify`：检查日志、索引行和 artifact 是否互相一致，不写任何东西。退出码即操作指令——0 一致，1 只是派生状态漂移（`atlas reindex` 可修），8 日志本身有损（只能恢复）。
- `atlas reindex`：从日志重建 SQLite 索引。每个 session 一个 `BEGIN IMMEDIATE` 事务，先删旧行再插日志的行。日志读不出来的 session 被拒绝而不是部分索引。
- `atlas backup` / `atlas backup-check` / `atlas restore`：备份、离线重新校验、以及会**证明恢复出来的日志在今天的构建上仍折出同一个 state hash** 的恢复。清单文件最后写入，所以被打断的备份是可识别地不完整。恢复进非空目录需要 `--force`。
- `atlas release-check`：十项由代码算出的发布检查，加上八项风险的监控/暂停/回滚动作。只读。
- `src/atlas_harness/events/compat.py`：把 schema 版本历史和五条兼容性规则写成可查询的数据，不再只是 README 里的一段话。
- `src/atlas_harness/ops/`：`verify`、`migrate`、`backup`、`checklist` 四个模块，CLI 和 HTTP 之外的第三个调用面。
- `samples/`：七份冻结日志（schema v1 到 v7）加 `expected.json`。其中 `ses_release_demo` 是计划第 13 节要求的可回放 demo——一次审批、一次工具结果外置为 artifact、一次测试失败后修好。
- `docs/runbook.md`、`docs/recovery.md`、`docs/security-review.md`：运行手册、故障恢复手册、安全审查记录。
- `tests/performance/`：大日志、长 session、并发只读工具的规模检查，带 `performance` marker，按计划第 11.1 节在里程碑结束时运行而不进每次 PR 的门禁。

### 测试

新增 248 个用例，全部通过：`test_event_compat.py`（98）、`test_ops_verify.py`（22）、`test_ops_migrate.py`（13）、`test_ops_backup.py`（20）、`test_ops_checklist.py`（30）、`tests/replay/test_samples.py`（43）、`tests/integration/test_cli_ops.py`（22）。安全专项 `tests/security/` 达到 205 passed / 2 skipped（两项 skip 是 Windows 未开开发者模式无法建符号链接）。`tests/performance/` 9 项。

### 修复

- `ops/migrate.py` 的索引重建曾把日志里的**全部**事件传给 `insert=`，而 `event_id` 与 `idempotency_key` 是唯一的，所以重新插入一行已存在的记录会让整个重建事务失败。改为只插索引尚未持有的行（`insert=fresh`）。

### 已知限制

见 `docs/security-review.md` 第 8 节。要点：HTTP 传输层无认证（默认绑 loopback）、工具执行无沙箱、未做渗透测试、OpenAI 兼容 adapter 未对真实 endpoint 发过请求、依赖供应链未审计。**内部试用可行，不适合处理生产凭证或暴露到不受信任的网络。**

## 0.8.0 — MCP、子 Agent、HTTP 和观测台（M8）

Schema 版本 7（新增 `mcp_server_connected`、`mcp_server_disconnected`、`mcp_tools_registered`、`subagent_task_started`、`subagent_task_finished`）。

三个入口和一个出口，共用已有的 Registry、Policy 和 Trace。MCP 工具名被重写为 `mcp_<server>_<tool>` 而非被采纳，scope 取声明与授予的交集；子 Agent 的任务合同在启动前落盘且只会被收窄；HTTP 路由只调用应用服务，错误体与 CLI 一致；`atlas export` 产出 trace / audit / metrics / replay-report 四个产物。

## 0.7.0 — Pending Window 和受控自进化（M7）

Schema 版本 6。候选 skill 必须绑定来源和证据，经七项指标的固定 benchmark 评测后才能晋升；shadow run 不写激活；晋升与回滚都是新事件。

## 0.6.0 — Memory、Skill 和来源追踪（M6）

Schema 版本 5。四层 Memory、版本化 Skill、稳定的检索排序和可解释的选择流水线。

## 0.5.0 — 上下文编译器和结构化压缩（M5）

Schema 版本 4。五个固定槽位、70/85/95 三个阈值、九字段结构化摘要、大输出外置为 artifact。

## 0.4.0 — 会话恢复、快照和 Lane（M4）

Schema 版本 3。快照、lane 与分支、崩溃恢复；幂等读取可重放，未完成的非幂等副作用进入 `suspended`。

## 0.3.0 — 模型适配层和 Agent 循环（M3）

Schema 版本 2。Provider adapter、fake provider、一次完整的 model → tool → result → model 循环。

## 0.2.0 — 工具、Schema、Scope 和 Policy（M2）

Schema 版本 1。四个内置工具，全部经过 schema 校验、scope、policy 预检和审批。

## 0.1.0 — 事件日志与骨架（M0–M1）

Schema 版本 1。JSONL 追加日志作为唯一事实来源，SQLite 索引作为可重建的缓存，reducer 折叠出 session 投影。
