# 变更记录

版本号形式为 `0.M.0`，M 是里程碑序号。`0.` 前缀表示公开接口仍可能变化；固定下来的只有事件 schema 的向后兼容承诺，见每个版本的 schema 一节。

事件 schema 的策略是**只进不改**：新版本可以新增事件类型和带默认值的字段，不可以重定义或移除已有的。所以任何受支持版本写下的日志在今天的构建上原样可读，`SUPPORTED_SCHEMA_VERSIONS` 只增不减。这条承诺由 `src/atlas_harness/events/compat.py` 声明、由 `samples/` 里每个版本一份的冻结日志证明。

## 未发布 — 意图识别的数据合同（M10 第一步）

Schema 版本 8（新增 `intent_classified`）。这一步只动数据合同，不含任何分类器——`docs/intent.md` 第 9 节把 M10 拆成四步，正是为了让「加事件类型」这件不可撤回的事尽早被样例日志和兼容性检查覆盖，且与分类逻辑对不对无关。

### 新增

- `intent_classified` 事件：一次意图分类的完整记录。弃权也写，因为「taxonomy 覆盖不到用户实际在问什么」只能从弃权率上看出来，一份只留下自信答案的日志回答不了这个问题。`classifiers_abstained` 与 `degraded` 分开：一路跑完了但证据不够，不等于一路没跑起来。
- `SessionState.last_intent`：最新一条意图信号的投影，只给 `atlas session show` 和人看，不给任何决策路径读。新的胜出，弃权也算——弃权是当前答案，留着上一个自信标签等于把过期结论当成还生效的。
- `samples/sessions/ses_schema_v8/`：v8 冻结日志，含一条自信分类和一条弃权分类。
- `src/atlas_harness/model/providers/anthropic_messages.py`：原生 Anthropic Messages 适配器（`POST /messages`），`ATLAS_MODEL_PROVIDER=anthropic` 或 `anthropic_messages` 启用。方言差异不止 URL：`x-api-key` 加必需的 `anthropic-version`、system 提示是顶层参数而不是消息、`max_tokens` 必填、具名 SSE 事件、工具参数以 `input_json_delta` 的文本碎片到达。最容易被漏掉的一条是**连续的工具结果必须合并进同一个 user 轮次**——角色必须交替，而一批并行工具调用会产出多条 `tool` 消息，逐条发出去会被 API 拒绝。schema 版本不变：加 provider 不动数据合同。
- `src/atlas_harness/model/providers/_http.py`：两个 HTTP adapter 共享的故障分类、退避和 `stream_with_retry`。抽出来的理由是「**只在第一个事件送达调用方之前重试**」这条规则不能有两份实现——一旦某个 adapter 把它写错，重复的文本会被当成模型真的说了两遍，而这种缺陷在单 adapter 的测试里看不出来。`RETRYABLE_STATUS_CODES` 补了 529（不在任何 RFC 里，是 Anthropic 的 `overloaded_error`，也是这个方言最常见的瞬时故障）。
- `ATLAS_MODEL_ANTHROPIC_VERSION`（默认 `2023-06-01`）：这个 header 选的是一份线格式合同，不是版本号装饰，所以跟随新版必须是显式动作。
- `catalog.py` 新增四个 Anthropic 模型的能力条目（`claude-sonnet-4-5`、`claude-opus-4-1`、`claude-haiku-4-5`、`claude-3-5-haiku-latest`）。

### 修复

- `state_hash()` 此前把 `schema_version` 算进指纹，而这个字段取自 `CURRENT_SCHEMA_VERSION`、记录的是谁折的而不是日志说了什么。后果是每次 bump 版本都会让所有未改动的旧日志换一个哈希，于是 `samples/expected.json` 只能跟着改写——而一份为了让测试通过而重算的基线，度量的是重算本身，不是兼容性。现在它被排除在指纹之外，`HASH_EXCLUDED_FIELDS` 是唯一的例外名单，规则是「日志没说的排除，说了的都算进来」。

  **`expected.json` 里八个哈希因此一次性重算。**这是这个文件唯一一次被允许重写，代价换来的是它此后真正冻结：v9、v10 再也不会碰它。旧哈希不可能在新规则下复现，所以重算无法回避；能选的只是把基线冻在哪条规则上。`last_intent` 留在指纹内，因为它是从事件折出来的——折错了必须挂门禁。

- `agent/loop.py:tool_declarations()` 此前直接产出 OpenAI 的 `{"type": "function", "function": {...}}`，与 `model/protocol.py` 自己写下的合同矛盾。这是分层缺陷而不是风格问题：它等于要求每个新方言的 adapter 先学会读 OpenAI 的方言，再翻译成自己的。现在它产出与方言无关的 `{name, description, input_schema}`，渲染成各自的线格式是 adapter 的事。改动面在落地前先量过：8 处形状断言，无生产行为变化。

### 测试

新增 42 项：`tests/unit/test_reducer.py` 5 项（含 `test_a_version_bump_alone_does_not_change_an_old_logs_hash`，把上面那条修复钉在机械断言上，而不是等冻结样例在下一个版本才发现），`tests/replay/test_samples.py` 因 v8 样例加入而多出 3 项，`tests/unit/test_model_anthropic_provider.py` 34 项（全部走注入的 `MockTransport`，不碰网络：请求形状、流解析、六种 stop reason、跨两帧的 usage 累计，以及五类故障各自的报告方式），另有两项断言把方言归属钉住——`test_declarations_are_free_of_any_provider_dialect` 和 `test_neutral_declarations_are_wrapped_in_the_function_envelope`。门禁 1402 passed / 3 skipped，三项 skip 仍是环境性的。

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
- `atlas model-check`：对配置好的 provider 发一次真实请求并打印裁决。这是唯一需要网络的命令，也是唯一能分辨「base URL、key 和模型名三者能配合工作」的手段——注入 transport 的测试分不出对的 endpoint 和错的。不写任何事件：连通性检查不是一次会话，必须能安全地对不属于它的数据目录运行。密钥不进输出，报告只说配了没配。裁决即退出码：缺 key 2、超时 18、其他 provider 故障 17。
- `atlas eval run [<dataset>...]`：跑固定任务集，任何任务失败就非零退出，给 CI 读。和 `skill-evaluate` 是同一套测量，只是没有候选——回答问题的是当前生效的 Skill 库。数据集名写错会被拒绝而不是跳过，否则一次跑零个任务的运行会退出 0。
- `src/atlas_harness/model/probe.py` 与 `tests/unit/test_model_probe.py`：探测逻辑放在 model 层而不是 CLI 里，所以同一次往返既能由命令驱动，也能由带 `live` marker 的用例驱动。新增 `live` pytest marker，默认跳过，门禁保持离线自足。报告里的错误信息先按形状脱敏、再按**值**去掉这次实际用的 key——`redact` 只认识 `sk-`、`ghp_`、JWT 这类已知形状，而自建网关签发的 key 可以是任何形状并把它原样写进错误体，这一步覆盖的正是形状匹配覆盖不到的那一类。

### 测试

新增 269 个用例：`test_event_compat.py`（98）、`test_ops_verify.py`（22）、`test_ops_migrate.py`（13）、`test_ops_backup.py`（20）、`test_ops_checklist.py`（30）、`tests/replay/test_samples.py`（43）、`tests/integration/test_cli_ops.py`（22）、`test_model_probe.py`（13 + 1 个 `live`）、`tests/integration/test_cli_eval.py`（7）。安全专项 `tests/security/` 达到 205 passed / 2 skipped（两项 skip 是 Windows 未开开发者模式无法建符号链接）。`tests/performance/` 9 项。

门禁总计 1093 passed / 1 skipped（`tests/unit` + `tests/integration`）与 260 passed / 2 skipped（`tests/replay` + `tests/security`）。三项 skip 都是环境性的，不是被绕过的断言：一项缺凭证，两项缺 Windows 符号链接权限。

### 修复

- `ops/migrate.py` 的索引重建曾把日志里的**全部**事件传给 `insert=`，而 `event_id` 与 `idempotency_key` 是唯一的，所以重新插入一行已存在的记录会让整个重建事务失败。改为只插索引尚未持有的行（`insert=fresh`）。

### 已知限制

见 `docs/security-review.md` 第 8 节。要点：HTTP 传输层无认证（默认绑 loopback）、工具执行无沙箱、未做渗透测试、真实 provider 的行为不在自动化门禁内（`atlas model-check` 可按需验证，但需要凭证因而默认跳过）、依赖供应链未审计。**内部试用可行，不适合处理生产凭证或暴露到不受信任的网络。**

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
