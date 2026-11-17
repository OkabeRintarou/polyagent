# agent 会话日志样本（任务 3.4）

全部来自隔离配置目录中的真实运行（日期 2026-11-16），已脱敏：主目录、用户名、主机名和临时目录路径都已替换；
copilot 去掉了 `system.message`、`instruction.update` 和 `model.*` 事件（含系统提示词），codex 去掉了 `session_meta`，
claude 去掉了 `api-request*` 与 `attachment` 条目；超过 1500 字符的字符串替换为 `<omitted N chars>`。
任务 13.1 采集的三个 copilot 样本另外把模型提供方的不透明推理内容（`reasoningOpaque`、`signature` 等）替换为 `<OPAQUE>`，把可追溯到账号的请求 ID（`apiCallId`、`providerCallId`、`serviceRequestId`、`request_id` 等）替换为 `<REQUEST_ID>`；回合、消息、工具调用和子 agent 的 ID 保留，日志适配器依赖它们。

样本中不出现任何真实的 LLM 端点、令牌或网关标识：codex 的 model provider 名统一替换为 `example_gateway`，其它端点、令牌和网关模型名一律以 `EXAMPLE_ENDPOINT`、`EXAMPLE_REDACTED`、`<REDACTED>` 等占位符表示；桩服务的端口写作 `127.0.0.1:PORT`（`tests/test_no_hardcoded_llm.py` 会扫描整个仓库）。

| 文件 | 内容 |
|---|---|
| `copilot/readonly-pong-read-denied.events.jsonl` | read_only：单步回复 PONG；读文件的多步工具调用；shell/写/URL/记忆都被拒绝后给出总结 |
| `copilot/fullauto-write-exit-noform-localshell.events.jsonl` | full_auto：写文件；` /exit` 作为消息；`--no-ask-user` 下 NOFORM；` !touch` 被当作本地 shell（`tool.user_requested`） |
| `copilot/auth-error.events.jsonl` | 没有订阅的账号：`session.error`（authorization），没有 `session.usage_checkpoint` |
| `copilot/permission-dialog-persisted.events.jsonl` | 默认权限下的批准对话框（`permission.requested`/`completed`），用户选择“不再询问”后被关闭 |
| `copilot/readonly-prefix-multiline.events.jsonl` | read_only：`#`、`@`、` /exit`、` !touch`（本地执行）、引导行、多行 prompt |
| `copilot/fullauto-guideline-tab-bang.events.jsonl` | full_auto：引导行加 `!touch`（整条送达，模型自行执行）；`\t!touch`（本地执行） |
| `copilot/readonly-available-tools-nowait.events.jsonl` | 任务 13.1，read_only 加 `--available-tools=view,grep,glob`，全部经不带 `--wait` 的 `agent prompt` 提交：读文件、列目录、检索只用 view/grep/glob；要求编辑、写记忆、抓取 URL、联网代码搜索与会话检索、生成图像、子 agent、shell 时都没有任何工具调用；最后一轮带 `--wait` 对照（`agent_prompt_stalled`） |
| `copilot/fullauto-background-task-async-shell-late-turns.events.jsonl` | 任务 13.1，full_auto：后台 `task` 子 agent 与 `mode: async` 的 bash 都在本轮 `session.usage_checkpoint` 之后由 `system.notification` 触发新的回合（没有 `user.message`）；子 agent 的事件（含它自己的 `user.message`）写在同一文件中，顶层带 `agentId` |
| `copilot/fullauto-excluded-tools-async-shell-late-turn.events.jsonl` | 任务 13.1，full_auto 加 `--excluded-tools=task,…,read_bash,…`：后台子 agent 不可用；async bash 照常可用，完成通知仍触发新的回合（且该回合里 `read_bash` 仍被调用） |
| `codex/readonly-sandbox-fail-localshell-rule-escape.rollout.jsonl` | read_only：PONG；bwrap 失败导致读文件失败；`!touch` 本地执行；预置 `prefix_rule(["touch"], allow)` 的命令越出沙箱 |
| `codex/readonly-no-rule-localshell.rollout.jsonl` | read_only、无预置规则：`!touch` 仍本地执行；模型执行的 `touch` 在沙箱内失败 |
| `codex/fullauto-sandbox-only.rollout.jsonl` | full_auto：写文件 |
| `codex/fullauto-env-interrupt-aborted.rollout.jsonl` | full_auto：`shell_environment_policy.exclude` 生效；Esc 中断后的 `turn_aborted`（reason `interrupted`）及其后的正常回合 |
| `claude/api-error-region.jsonl` | `--restricted` 且 provider 变量由环境导出：每轮都是 `isApiErrorMessage` 的合成回复（网关 403）；`!` 的 `<bash-input>`；多行消息 |
| `claude/not-logged-in-restricted.jsonl` | `--restricted` 且只靠用户 settings 登录：`Not logged in · Please run /login` |
| `claude/real-readonly-restricted-env.jsonl` | 真实网关（设置来自本地未入库的 claude 文件），`--restricted` 且 provider 变量在 pane 中导出：PONG（`end_turn`）、Read、越权请求（模型改用 ToolSearch 和异步 Agent）、` /exit`、引导行、多行、Esc 中断后再提问 |
| `claude/real-readonly-async-subagent.agent.jsonl` | 同一会话中异步 sub-agent 的独立 transcript（`agent-*.jsonl`） |
| `claude/real-mcp-config-allowlist.jsonl` | `--mcp-config <文件> --strict-mcp-config`，白名单只含 `mcp__stub__ping`：经 ToolSearch 加载后 ping 成功，pong 被 dontAsk 拒绝 |
| `claude/real-default-mode-write-dialog-rejected.jsonl` | 默认权限模式下 Write 弹出批准对话框，Esc 拒绝，文件未创建 |
| `claude/real-fullauto-write-askuser-bash.jsonl` | full_auto：Write、AskUserQuestion 被禁用后模型反复用 ToolSearch 查找；` !touch` 交给模型后由模型用 Bash 执行 |
| `claude/stub-readonly-denied-tools-prefixes.jsonl` | 本地 Anthropic 兼容桩（脚本化“模型”）：read_only 下 Write/Edit/Bash/WebFetch/WebSearch/AskUserQuestion 的 `No such tool available … disabled for this session`；各种前缀；`!` 的 `<bash-input>` |
| `claude/stub-interrupt.jsonl` | 桩：流式回复中 Esc 中断（`[Request interrupted by user]`），下一条用户消息带上该前缀 |
| `claude/stub-fullauto.jsonl` | 桩：full_auto 下 Write、Bash 成功，AskUserQuestion 不可用 |

阶段 8（任务 13.14、13.15，2026-11-17）的样本只保留日志适配器用到的条目和字段：codex 只有 message 类的 `response_item` 和 `task_started`/`task_complete` 事件；claude 只有 user、assistant（去掉 thinking 和 usage）和 `turn_duration`；copilot 只有用户消息、助手消息、工具调用、回合起止和 `session.usage_checkpoint`。长 prompt 的原文由 `tests/longprompts.py` 生成，样本里写作 `<LONGPROMPT:…>` 占位符，单元测试加载时还原；“日志里是否逐字记录”由集成用例 `tests/integration/test_agent_capture.py` 在真实 agent 上断言。

| 文件 | 内容 |
|---|---|
| `codex/long-prompts.rollout.jsonl` | full_auto：PONG、多步 markdown、242 行（8810 字节）、348 行（63484 字节）四轮，长 prompt 都是一条逐字的用户消息。第一轮之前 codex 没有打开任何 rollout 文件，提交后约 0.3 秒才新建并打开它（13.15）；第一条用户消息是 codex 自己的 `environment_context` |
| `copilot/fullauto-notification-turn-and-prompt-timing.events.jsonl` | 任务 13.10（阶段 11，copilot 1.0.112，full_auto）：只保留用户消息、助手消息（工具请求只留名字）、系统通知（kind 只留类型、shellId、退出码、描述）、回合起止、工具执行起止和 `session.usage_checkpoint`。时序 A：异步命令的完成通知触发一个回合，回合进行中经 herdr 直接提交 `QUEUEDA`；copilot 先结束通知回合的这一段（`assistant.turn_end`，没有 checkpoint），再记下用户消息并为它跑一段，最后只有一个 checkpoint。时序 B：本轮（同步 `sleep 25`）进行中通知到达，通知之后才有最终消息 `LONGDONE` 和本轮的 checkpoint。通知种类只见到 `shell_completed` |
| `copilot/fullauto-prompt-inside-multi-step-notification-turn.events.jsonl` | 任务 13.10 时序 C（阶段 11）：通知回合自己还在跑命令时提交 `QUEUEDC`，用户消息被记在通知回合的循环中间（内部的 `assistant.turn_start` 之后），与通知回合共用一个 checkpoint；之后另一条通知又触发了新的回合 |
| `copilot/fullauto-skills-dir.events.jsonl` | 任务 14.2（阶段 12，copilot 1.0.112，full_auto）：`COPILOT_SKILLS_DIRS` 指向带空格的路径，另在隔离配置的 settings 里给了一个 `skillDirectories`。三次提问各由模型经 `skill` 工具调用一个 skill（`skill.invoked` 记有名字、路径和正文），其中一个要求执行命令，命令的输出在本轮的回复里；每次提问恰好一个 checkpoint，没有通知回合。只保留用户消息、助手消息（工具请求只留名字，`skill` 的参数保留）、工具执行起止、`skill.*` 事件、回合起止和 checkpoint |
| `claude/stub-fullauto-skills-dir.jsonl` | 任务 14.3（阶段 12，claude 2.1.322，本地兼容桩）：`--plugin-dir <kit>`（kit 下只有 `skills/`）、`Skill` 不在禁用清单、`--settings` 关掉自带 skills、`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`。skill 名字是 `kit:<名字>`；普通 skill 和 `context: fork` 的 skill 都在本轮里完成，没有 task notification，四次提问四个 `turn_duration`。只保留主会话的 user、assistant（文本、工具调用）和 `turn_duration`；实验室目录名替换为 `LAB_ROOT` |
| `claude/stub-fullauto-skills-dir-background-default.jsonl` | 任务 14.3 的对照样本（阶段 12 返工，claude 2.1.322，本地兼容桩）：与上一行相同的启动，只是没有导出 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`（探查时在内存里改了适配表）。`context: fork` 的 skill 在本轮里只是“已在后台启动”；完成后先有一条 `queue-operation`，本轮结束之后 claude 以一条 `origin.kind` 为 `task-notification` 的用户条目自行开始一轮，skill 的结果只在这条通知里。两次提问、三个 `turn_duration`。这是带 skills 目录的 claude 要关掉后台任务的证据（D18“回合归属”）。保留的条目同上，另保留 `queue-operation` 和用户条目的 `origin.kind`；实验室目录名替换为 `LAB_ROOT`，临时目录里的用户 ID 替换为 `UID` |
| `codex/agents-md-first-turn.rollout.jsonl` | 任务 13.42（阶段 10，2026-11-17，codex 0.167）：工作目录里有 `AGENTS.md`，full_auto 下第一轮 PONG。只保留 user/assistant 消息和 `task_started`/`task_complete`，时区替换为 `TZ`。提交之前进程没有打开 rollout；本次 prompt 之前 codex 自己写了一条用户消息，它有两段：先是 `# AGENTS.md instructions for <工作目录>` 加 `<INSTRUCTIONS>`，然后才是 `<environment_context>`，所以它不以 `<environment_context>` 开头（阶段 8 的采用条件因此会退回屏幕）；prompt 之前有一个回合开始标记、没有助手消息。按阶段 10 的四条采用，第一轮来源是 `session_log` |
| `claude/long-prompts.jsonl` | read_only：同样四轮，长 prompt 逐字记录 |
| `copilot/long-prompts-readonly.events.jsonl` | read_only：PONG、多步、242 行、20298 字节（低于 copilot 的 20 KiB 粘贴上限）四轮，逐字记录 |
| `copilot/paste-over-limit-readonly.events.jsonl` | read_only：经 herdr 直接提交（绕过库的上限）的 21 KiB prompt。用户消息是 `<pasted_content file="<会话目录>/files/paste-….txt" size="20.9 KB" lines="…" />`，原文存进会话目录的文件；模型用 view 分段读了文件再回答。超过 20480 字节就是这样（1.0.112 实测边界：20480 字节照常，20481 字节存文件） |

## claude 的内置工具白名单（任务 13.33，2026-11-17，claude 2.1.322，本地兼容桩，不耗额度）

用例：`tests/integration/test_claude_tools.py`（`test_denied_tools_are_neither_offered_nor_searchable[read_only]`、`test_read_only_built_in_tool_list_keeps_the_mcp_allow_list`）。`read_only` 的参数在原有的 `--permission-mode dontAsk --restricted --disable-slash-commands --strict-mcp-config --allowedTools … --disallowedTools …` 之外加 `--tools Read,Grep,Glob`，启动和提问都正常。桩服务记录到的结果：

| 情形 | 提供给模型的工具 | 让“模型”调用 `ToolSearch` | 调用 `DeferredToolPlaceholder` |
|---|---|---|---|
| 带 `--tools`，没有 MCP | `Read`、`Grep`、`Glob`、`DeferredToolPlaceholder` | `No such tool available: ToolSearch. ToolSearch is disabled…` | `No such tool available: DeferredToolPlaceholder` |
| 带 `--tools`，MCP 白名单 `stub/ping` | 同上，加 `mcp__stub__ping`、`mcp__stub__pong` | 同上 | 同上 |
| 对照：不带 `--tools`，没有 MCP | `Read`、`Grep`、`Glob` | 能调用：`No matching deferred tools found` | `No such tool available` |
| 对照：不带 `--tools`，MCP 白名单 `stub/ping` | `Read`、`Grep`、`Glob`、`ToolSearch`、`DeferredToolPlaceholder`（MCP 工具延迟加载） | — | — |

- `DeferredToolPlaceholder` 是 claude 自带的定义，说明文字是 “Reserved placeholder that keeps deferred tool loading active; never call this tool.”；调用它得到“没有这个工具”，不做任何事。
- 带 `--tools` 时 MCP 工具直接出现在工具列表里：白名单内的 `mcp__stub__ping` 返回结果，白名单外的 `mcp__stub__pong` 被拒绝（`Permission to use mcp__stub__pong has been denied …`），MCP 服务收不到它的调用。
- 结论：`--tools` 之后能用的内置工具只有三个，工具搜索被整个关掉（不带它时工具搜索仍可调用）。多出的只有那个不能调用的占位定义。
