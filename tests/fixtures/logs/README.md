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
| `cursor/transcript-pong-interrupted.jsonl` | `CURSOR_DATA_DIR/projects/<编码 cwd>/agent-transcripts/<id>/<id>.jsonl`：PONG，以及 Esc 中断后的 `turn_ended` `status: aborted` |
