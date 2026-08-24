# 安全审查记录

M9 的安全专项检查记录。审查日期 2026-08-24，对应 schema 版本 7。

这份记录的写法是：每一项列出**在哪里判定**、**用什么证明**、以及**已知的边界**。最后一栏是关键——一份只列出「已覆盖」的安全记录会让读它的人高估防护范围，而高估比空白更危险。

审查范围：计划第 11.2 节点名的安全场景，以及第 15 节风险登记表里由代码控制的部分。运行方式：

```bash
uv run pytest tests/security          # 205 passed, 2 skipped
uv run atlas release-check --samples samples
```

两项 skip 是 Windows 未开启开发者模式时无法创建符号链接，测试自己跳过而不是假装通过。在 Linux/macOS 上这两项会实际执行。

## 1. 路径

**在哪里判定**：`policy/path_policy.py`，在任何文件被打开之前。工具实现里没有第二条路径解析。

**判定内容**：机制只有一条——**resolve 之后必须仍落在 `workspace_root` 之内**。这一条同时覆盖了 `..` 逃逸、绝对路径、Windows 盘符跳转和 UNC 路径，因为它不去枚举这些形式，而是判定解析后的结果，所以一种没人想到的写法也过不去。在此之上另有具名规则：`path_symlink`（拒绝路径中任何一段是符号链接，否则一个链接就能把路径偷运出去）、`path_denylisted`（deny glob 命中的敏感文件，如 `.env`、私钥、凭证存储）、`path_read_not_allowed` / `path_write_not_allowed`、`file_too_large`、`path_invalid`。

**证明**：`tests/security/test_session_paths.py`（26 个用例）、`tests/security/test_dangerous_commands.py` 里的敏感文件部分。deny 列表是**从策略模块读出来的**（`DEFAULT_DENY_GLOBS`）而不是抄一份，所以从默认集合里删掉一条 glob 会让这里的测试失败，而不是静默放宽。

**已知边界**：判定发生在 resolve 之后、打开之前，所以理论上存在 TOCTOU 窗口（判定与打开之间路径被换成符号链接）。单进程本地运行下这个窗口不构成实际风险，但如果将来把工具执行放进共享目录或多用户环境，必须改为持有文件句柄后再校验。

## 2. 命令

**在哪里判定**：`policy/command_policy.py`，在 `subprocess` 之前。安全测试里没有任何子进程被真正启动——一个只在进程起来之后才发生的拒绝根本不算拒绝。

**判定内容**：十一条具名规则、十五处判定点，每一条都在 `details["rule"]` 里可聚合：

`command_empty`、`command_too_long`（参数数量与 4096 字符两处）、`command_injection`（shell 元字符与拼接 token）、`command_unparsable`、`command_path_program`（带路径的程序名）、`command_denylisted`、`command_not_allowlisted`、`command_dangerous_flag`（含 `recursive+force` 组合）、`command_inline_code`（`python -c` 一类）、`command_path_escape`（参数逃出工作区）、`command_network`（网络子命令）。

白名单 `DEFAULT_ALLOWED_COMMANDS` 是 19 个开发工具，不在其中的一律拒绝而不是警告；`DEFAULT_DENIED_COMMANDS` 另外显式挡住 shell 本身（`bash`/`sh`/`cmd`/`powershell`）、提权（`sudo`/`su`）、删除（`rm`/`dd`/`mkfs`）和网络（`curl`/`ssh`/`scp`/`nc`）——即便有人把它们加进白名单，denylist 仍然先命中。

**证明**：`tests/security/test_dangerous_commands.py`（20 个测试函数，参数化后共 123 个用例）。

**已知问题（未修，仅影响诊断）**：`command_policy.py:139` 的参数数量拒绝和 `:157` 的 4096 字符拒绝用了同一个 rule 字符串 `command_too_long`，只有 `details["limit"]` 能区分两者。这不影响任何拒绝行为，但会让根据 rule 字段做聚合的告警把两种情况混为一类。

## 3. Prompt Injection

**在哪里判定**：结构上——**工具结果从不参与授权判定**。`policy.preflight` 读的是 manifest 和调用方给的参数，不读上一次的结果，所以工具输出里写「你现在有权限了」不改变任何东西。审批门同理。

**判定内容**：工具结果被标记为不可信数据；下一次调用重新走完整预检链（`registry.get` → `tool.parse` → `tool.policy_request` → `policy.preflight`）；隐式授权没有代码路径。

**证明**：`tests/security/test_prompt_injection.py`（11 个测试函数，22 个用例）。

**已知边界**：这里控制的是**权限**不被工具输出扩大。它不能阻止模型被注入文本误导去做一件它本来就有权限做的事——那需要人在审批环节判断，而审批环节的存在就是为了这个。所以「有副作用的工具默认要审批」不是冗余，它是这条边界的唯一兜底。

## 4. Skill Poisoning

**在哪里判定**：`evolution/` 的状态机，以及 `context/capability.py` 的检索边界。

**判定内容**：候选 skill 在被评测通过并晋升之前是**惰性的**——它存在，但检索不到，进不了 prompt。晋升是进入 `active` 的唯一一条边。晋升后行为变差可以 `skill-rollback` 回到指定版本。候选必须绑定来源和证据。

**证明**：`tests/security/test_skill_poisoning.py`（12 个测试函数，27 个用例）从 **prompt 那一侧**证明这道门——即通过 `CapabilitySelector` 断言一个未晋升的候选不会出现在编译出的 prompt 里。`tests/unit/test_evolution.py` 从流水线那一侧证明同一道门。

**已知边界**：固定 benchmark 的覆盖面决定了这道门的强度。一个只在 benchmark 覆盖不到的场景里有害的候选可以通过评测。pending window 和回滚是对这个残余风险的应对，不是消除。

## 5. MCP 越权

**在哪里判定**：`mcp/bridge.py` 的 `bridge_tools()`，在工具进入 Registry 之前。

**判定内容**：工具名被**重写**为 `mcp_<server>_<tool>` 而不是被采纳，所以外部工具不可能叫 `read_file`，也不可能盖住内置工具；scope 是 `spec.scopes & config.granted_scopes` 的交集，服务器要求超出授予范围的工具直接被拒而不是降级；翻译完成后走和内置工具完全相同的预检链；`requires_approval` 默认为真；凭证默认不继承进程环境。

**证明**：`tests/security/test_mcp_policy.py`（9 个测试函数）。核心断言是一个恶意 MCP 工具无法绕过 Policy——因为它根本没有第二条通路可走。

**已知边界**：MCP 服务器进程本身在这个运行时的信任边界之外，没有沙箱。`granted_scopes`、超时、`max_output_bytes` 和并发上限限制的是它能通过这个运行时做什么，不限制它在自己进程里做什么。连接一个不受信任的 MCP 服务器等于运行一个不受信任的程序。

## 6. 敏感信息落盘

**在哪里判定**：`tools/redaction.py`，在事件被写入日志的路径上。

**判定内容**：AWS 长期/临时密钥、OpenAI `sk-`、GitHub `gh[pousr]_`、Slack `xox[abposr]-`、JWT、`Bearer`/`Basic` 头、URL 内嵌凭证，以及 `SECRET_NAME_PATTERN` 后跟赋值的通用形状。

**证明**：`tests/security/test_prompt_injection.py` 的脱敏部分；`release-check` 的 `no_secrets_in_output` 扫描真实数据目录的日志与 artifact；`tests/replay/test_samples.py` 断言随仓库分发的每一行样例日志都不被脱敏函数改动（即不含凭证形状）。

**设计要点**：`no_secrets_in_output` 报告**位置而不是内容**，因为这项检查自己的输出也会被打印和归档。`tests/unit/test_ops_checklist.py` 用 AWS 官方文档里的示例 key 断言了这一点——检查结果的 JSON 和渲染文本里都不含那串字符。

**已知边界**：脱敏是基于形状的模式匹配，只能挡住已知形状。一个自定义格式的凭证会原样落盘。日志是追加式的，写进去的东西拿不出来——所以一旦发现漏了一个形状，正确处理是**轮换那个凭证**，然后补模式，不是编辑日志。

## 7. 事件完整性

**在哪里判定**：`events/store.py` 的读取路径（严格）与 `ops/verify.py`。

**判定内容**：seq 必须从 1 连续；`event_id` 和 `idempotency_key` 唯一；session id 必须与目录一致；schema 版本必须在 `SUPPORTED_SCHEMA_VERSIONS` 内，否则 `EventValidationError`。`atlas verify` 用退出码区分「派生状态漂移」（1，可重建）和「日志有损」（8，需恢复）。

**证明**：`tests/unit/test_ops_verify.py`（22）、`tests/unit/test_ops_migrate.py`（13）、`tests/integration/test_cli_ops.py`（22，含一条把损坏目录只用这几条命令走回健康的完整流程）。

**已知边界**：日志文件本身没有签名或 MAC。一个有本机文件写权限的人可以构造一份一致的假日志。这个运行时的威胁模型是「模型输出和工具结果不可信」，不是「本机文件系统不可信」。

## 8. 未覆盖的部分

明确记录，以免被读成已覆盖：

- **HTTP 传输层没有认证**。默认绑 loopback。绑到 `0.0.0.0` 等于把整个工作区交给网络。任何非本机部署必须在前面加一层认证代理。
- **工具执行没有沙箱**。`run_command` 的隔离完全由白名单和参数校验提供，没有 namespace、cgroup 或 seccomp。计划把沙箱列为后续功能。
- **没做过渗透测试**。上面每一项都是白盒的属性测试，不是对抗性测试。
- **OpenAI 兼容 adapter 没有对真实 endpoint 发过请求**，只有 fake provider 覆盖。这是功能性缺口，但也意味着与真实 provider 交互时的错误处理路径未经验证。
- **依赖供应链未审计**。版本已固定范围，但没有 SBOM，也没有做过依赖漏洞扫描。

## 结论

计划第 11.2 节列出的安全场景全部有对应的自动化证明，第 15 节八项风险全部在 `ops/checklist.py:RISK_REGISTER` 里有明确的监控、暂停和回滚动作，且每条动作引用的文件路径都由 `tests/unit/test_ops_checklist.py::test_every_risk_cites_a_file_that_exists` 校验存在——一个指向已删除模块的控制项是意图，不是控制。

第 8 节的每一项都是接受而非缓解。**内部试用可以进行，不适合处理生产凭证或暴露到不受信任的网络。**
