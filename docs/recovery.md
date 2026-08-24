# 故障恢复手册

按症状索引。每一节的结构是：**你看到了什么 → 它意味着什么 → 做什么 → 怎么确认修好了。**

贯穿全部场景的一条规则：**日志赢。** `sessions/<id>/events.jsonl` 是唯一事实来源；索引、快照和所有投影都是缓存。任何时候，正确的动作是让派生状态服从日志，而不是反过来。这条规则的代价是它划出了一条硬边界——日志本身丢了的东西，没有任何工具能凭空补回来。承认这一点，才不会用一次「修复」把损坏变成静默的错误答案。

## 1. `atlas verify` 退出 1，说 `verdict: repairable`

**意味着**：索引和日志不一致，日志完好。计划第 15 节命名的 `SQLite/JSONL 不一致` 风险。常见成因是进程被 kill 在写索引和写日志之间，或者索引文件被外部工具动过。

```bash
uv run atlas reindex
uv run atlas verify        # 必须回到 0
```

不需要停机之外的任何准备，也不需要备份：重建只读日志、只写索引。

**确认**：`atlas verify` 退出 0；`atlas sessions` 列出的数量与 `sessions/` 下的目录数一致。

## 2. `atlas verify` 退出 8，findings 里有 `seq_gap`

**意味着**：日志缺了至少一条事件。**这不是索引问题，`reindex` 不会修它，也不应该修它**——从一份读不出来的源重建派生状态只会得到一份看起来完整的假数据，所以 `reindex` 会把这个 session 报为 `skipped` 并让整条命令退出 1。

```bash
uv run atlas verify --session ses_xxx --json | jq '.findings'   # 先确定缺口位置
uv run atlas backup --out /tmp/atlas-before-restore             # 保住当前状态，包括损坏的那份
uv run atlas backup-check /backup/atlas-<最近一份>                # 确认要用的备份是好的
uv run atlas restore /backup/atlas-<最近一份> --target /tmp/probe
uv run atlas verify --session ses_xxx                          # 在 /tmp/probe 上验证（改 ATLAS_DATA_DIR）
```

先恢复到 `/tmp/probe` 而不是直接盖回生产目录，是因为 `restore` 会证明恢复出来的日志在**今天的构建**上仍然折出备份时记录的同一个 state hash；这一步在临时目录上做，比在唯一的生产目录上做便宜得多。确认无误后再往生产目录恢复：

```bash
uv run atlas restore /backup/atlas-<最近一份> --force    # --force 才允许写进非空目录
uv run atlas verify
```

`--force` 是刻意的门：同一个 session id 的两段历史绝不能意外交错。

**代价要说清**：从备份恢复会丢掉备份之后的所有事件。如果缺口只在一个 session 上而其他 session 在备份之后仍有进展，用 `--session` 单独备份和恢复那一个，不要整目录回滚。

**确认**：`atlas verify` 退出 0；`atlas inspect ses_xxx` 的最后一个 seq 与 `restore` 报告里的 `last_seq` 相同。

## 3. `restore` 说 `verdict: incompatible` / `STATE HASH CHANGED`

**意味着**：字节是对的（校验和已经过了），但这份日志在今天的构建上折出了和备份时不同的结果。这是**向后兼容被破坏**，不是数据损坏。

**不要**继续用这个恢复结果服务流量。它是一个正确的告警，说明 reducer、payload 模型或 schema 的某个改动改变了一份已有日志的读法。

```bash
uv run pytest tests/replay/test_samples.py -q     # 冻结样例会指出是哪一版读法变了
git log -p --  src/atlas_harness/events/reducer.py src/atlas_harness/events/models.py
```

处理顺序是：回滚到能正确折叠这份日志的那个构建，恢复服务，然后再修改动。`samples/expected.json` 里的 hash 不许重新生成——重新生成会把这个告警变成沉默。

## 4. `backup-check` 退出 1

**意味着**：这份备份已经不可用了。`corrupt` 表示某个文件的内容和清单里的校验和不符；`missing` 表示清单里的文件不在了；`unlisted` 只是清单没提到的多余文件，**不影响可用性**（通常是两次备份写在同一个目录里留下的残余）。

```bash
uv run atlas backup-check /backup/atlas-xxx --json | jq '.corrupt, .missing'
uv run atlas backup --out /backup/atlas-$(date +%Y%m%d-%H%M)   # 立刻重做一份
```

不要轮换掉上一份还没被证明是坏的备份。同时检查存储介质——一份好的备份变坏通常不是软件问题。

## 5. session 处于 `suspended`，`resume` 返回 409

**意味着**：恢复时发现有未完成的**非幂等**写操作。计划第 15 节命名的 `Replay Side Effect` 风险，而 `suspended` 是这个风险的设计应对，不是故障。

```bash
uv run atlas recover ses_xxx     # 只显示恢复会做什么，不写任何东西
```

`recover` 会列出待人工确认的 call。读它们，判断那次副作用是否真的发生过，然后：

```bash
uv run atlas resume ses_xxx      # 确认后
uv run atlas abort ses_xxx       # 或者放弃这次运行，关闭所有未完成 operation
```

往一个 suspended session 上直接 `run` 也会被拒（CLI 报错，HTTP 409）：新工作不能把一个待决问题埋掉。

## 6. 快照损坏

**意味着**：快照是加速手段，不是事实来源。

删掉损坏的快照文件即可，恢复会退回从事件重建。之后跑：

```bash
uv run atlas replay ses_xxx      # 完全从日志重建，不经过快照，也不经过模型
uv run atlas verify --session ses_xxx
```

如果 `replay` 的 state hash 和 `inspect` 一致，这个 session 是健康的。

## 7. 一个候选 Skill 晋升后行为变差

**意味着**：计划第 15 节命名的 `Skill Poisoning` 风险。

```bash
uv run atlas skills                              # 看当前 champion 是哪一版
uv run atlas skill-rollback <skill> --version N   # 让上一版重新生效
uv run atlas capabilities "<一个代表性任务>"        # 确认检索结果回到了预期
```

回滚是写一个新事件，不是删旧事件。晋升与回滚的历史全在日志里。

## 8. 日志里出现了凭证形状的字符串

**意味着**：脱敏漏了一个形状，而这份日志现在是一份敏感文件。

```bash
uv run atlas release-check --samples samples --json | jq '.checks[] | select(.name=="no_secrets_in_output")'
```

这项检查报告**位置而不是内容**，因为它自己的输出也会被打印和归档。处理顺序：先按位置定位并轮换掉那个凭证（它已经落盘，必须假设已泄露），再补 `src/atlas_harness/tools/redaction.py` 里缺的模式，再为这个形状加一条测试。不要手工编辑日志去「擦掉」它——那会同时破坏日志的追加不变量和它作为证据的价值；正确做法是轮换凭证，让落盘的那串字符失效。

## 9. 什么都读不出来 / 不确定发生了什么

固定的三条命令，按这个顺序：

```bash
uv run atlas doctor              # 配置对不对，是否指错了目录
uv run atlas verify --json       # 到底哪个 session、哪一处不一致
uv run atlas trace ses_xxx       # 这个 session 按顺序发生了什么
```

`doctor` 放第一位是因为最常见的「数据全没了」其实是 `ATLAS_DATA_DIR` 指错了目录。它不写任何运行时状态，所以在任何情况下都可以安全地先跑。
