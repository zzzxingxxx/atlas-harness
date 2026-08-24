# 运行手册

面向运行 AtlasHarness 的人，而不是面向读代码的人。每一节给出的是一条可以照抄的命令和一个可以据此下判断的退出码。

日志（`sessions/<id>/events.jsonl`）是唯一事实来源，SQLite 索引和所有投影都可以从它重建。**冲突时日志赢。** 这条规则决定了下面每一步的顺序：先确认日志，再修派生状态。

## 0. 环境

所有配置都从 `ATLAS_` 前缀的环境变量读取，没有全局 `--data-dir` 选项。

```bash
export ATLAS_DATA_DIR=/var/lib/atlas          # 默认 .atlas
export ATLAS_WORKSPACE_ROOT=/srv/project      # 工具的路径边界
uv run atlas doctor                           # 只校验配置，不创建任何运行时状态
```

`atlas doctor` 是唯一一个可以在空目录上安全运行的检查。它不写事件、不建索引，所以在部署脚本里放在最前面。

## 1. 每日巡检

```bash
uv run atlas verify
```

退出码就是操作指令，这是刻意的设计：

| 退出码 | 含义 | 动作 |
|---|---|---|
| 0 | 日志、索引、artifact 三者一致 | 无 |
| 1 | 只有派生状态漂移 | `atlas reindex` |
| 8 | 日志本身有损（seq 缺失、行不可解析、session id 不符） | 转第 4 节，从备份恢复 |

区分 1 和 8 是这个命令存在的理由。索引永远可重建，所以 1 不是事故；日志缺一条事件没有任何工具能凭空补回来，所以 8 是事故。不要用 `reindex` 去回应 8——它会正确地拒绝，因为从一份读不出来的源重建派生状态只会得到一份看起来完整的假数据。

```bash
uv run atlas verify --json | jq '.counts, .findings'    # 给监控用
uv run atlas verify --session ses_xxx                   # 只看一个 session
```

## 2. 备份

```bash
uv run atlas backup --out /backup/atlas-$(date +%Y%m%d)
```

`backup` 复制日志、artifact 和索引，然后**立刻重新校验自己写出的副本**。没有被校验过的备份只是一个「存在备份」的信念，所以这一步不留给操作者。清单文件 `backup-manifest.json` **最后**写入，因此一次被打断的备份是可识别地不完整，而不是可恢复地错误。

```bash
uv run atlas backup-check /backup/atlas-20260824    # 定时重新哈希，退出码 1 表示副本已损坏
```

`backup-check` 不需要数据目录，可以在离线的备份卷上跑。把它放进 cron，而不是只在需要恢复的那天才第一次发现备份是坏的。

索引可以不备：`--no-index` 产出的备份仍然是完整的，恢复时会自动重建。

## 3. 重建派生状态

```bash
uv run atlas reindex          # 退出码 1 表示有 session 被跳过，即部分成功
uv run atlas reindex --json | jq '.skipped'
```

每个 session 的重建是一个 `BEGIN IMMEDIATE` 事务，先删旧行再插日志的行，所以中途崩溃留下的索引要么整体是旧的、要么整体是新的，绝不会是两段历史的混合。

`reindex` 是这个运行时唯一的「迁移」。schema 策略是**只进不改**的：新版本只能新增事件类型和带默认值的字段，不能重定义已有的，所以任何受支持版本写下的日志在今天的构建上原样可读，没有需要改写的旧日志——而改写一份日志会摧毁它作为证据的全部价值。

## 4. 恢复

见 [故障恢复手册](recovery.md)。

## 5. 发布前

```bash
uv run atlas release-check --samples samples
```

十项检查，全部由代码算出来，退出码 1 表示 `not ready`。它读数据目录、读 `samples/`，**不写任何东西**——否则它就是一个没人敢对生产环境运行的工具，而生产环境正是它有意义的地方。

| 检查 | 它在问什么 |
|---|---|
| `schema_policy` | 版本历史和兼容性规则自身是否自洽 |
| `data_dir_verified` | 真实数据目录能否通过 `verify` |
| `backup_round_trip` | 这个目录能否备份、校验、恢复回来 |
| `sample_replay` | 冻结样例是否还折出提交时记录的 hash |
| `schema_coverage` | 每个声明支持的 schema 版本是否都有样例 |
| `demo_session` | 样例里是否真有一次审批、一次外置、一次失败后修好 |
| `no_secrets_in_output` | 日志和 artifact 里是否有凭证形状的字符串 |
| `side_effect_tools_gated` | 有副作用的工具是否都要审批 |
| `policy_denies_the_obvious` | 路径逃逸和危险命令的探针是否都被拒 |
| `risk_register` | 八项风险是否都有监控、暂停和回滚动作 |

`sample_replay` 是唯一一项会因为一次**有意的**改动而失败的检查，这正是它的用途。`samples/expected.json` 里的 hash 冻结在生成它的那次发布上；它失败意味着 reducer、payload 模型或 schema 的某个改动改变了一份已有日志的读法，也就是向后兼容被破坏了。**修法是解释这个改动，永远不是重新生成 hash。**

发布前还要按计划第 14 节人工确认两项无法被代码断言的事：从上一个版本的数据目录恢复 session 确实可用（`restore` 到一个临时目录后跑 `verify` 和 `inspect`），以及所有有副作用的工具的审批与 suspended 流程确实走通（`atlas tool-check` 加一次真实的 `atlas run`）。

## 6. 观测

```bash
uv run atlas trace ses_xxx                    # 按顺序的每一步
uv run atlas audit ses_xxx                    # 九个问责类别
uv run atlas export ses_xxx --out /tmp/bundle # trace/audit/metrics/replay-report
```

`replay-report.json` 的折叠是**非严格**的：这份报告恰恰是在日志看起来有损时才被人打开的，所以缺一个 seq 会变成报告里的一处 `gaps`，而不是变成让报告写不出来的异常。

HTTP 入口（`python -m atlas_harness.transport.http`）默认绑 loopback。这一层**没有认证**，绑到 `0.0.0.0` 等于把整个工作区交出去。

## 7. 例行节奏

| 频率 | 命令 | 失败时 |
|---|---|---|
| 每小时 | `atlas verify --json` | 1 → 报警并 `reindex`；8 → 立即呼人 |
| 每日 | `atlas backup --out …` | 退出码非 0 表示副本没通过自校验，不要轮换掉上一份 |
| 每周 | `atlas backup-check <最近一份>` | 立刻重做备份，并检查存储介质 |
| 每次发布 | `atlas release-check --samples samples` | 见第 5 节，`not ready` 就是不发 |
