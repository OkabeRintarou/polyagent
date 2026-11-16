# 屏幕样本（任务 3.3）

`agent read --source visible` 或 `pane read --source visible` 的输出，已按 `../logs/README.md` 的规则脱敏。

| 文件 | 说明 | 应答 |
|---|---|---|
| `copilot/trust-dialog.txt` | 目录信任：`1. Yes` / `2. Yes, and remember this folder…` / `3. No (Esc)` | Enter 选第 1 项（只信任本次会话） |
| `copilot/permission-approval-dialog.txt` | 默认权限下的 shell 批准对话框 | Esc 拒绝 |
| `copilot/permission-approval-dialog-option2-selected.txt` | 光标移到“不再询问”后的同一对话框 | （核实用：选择后写入 `permissions-config.json`） |
| `copilot/help-overlay-after-raw-slash-help.txt` | 原样发送 `/help` 打开的帮助浮层；其后的 prompt 全部丢失 | Esc 关闭 |
| `codex/trust-dialog.txt` | “Trust this folder? … Your trust decision will be saved.” | Enter 会写入 `config.toml`；用 `-c 'projects={"<dir>"={trust_level="trusted"}}'` 可不出现且不写盘 |
| `codex/status-after-raw-slash-status.txt` | 原样发送 `/status` 的输出 | 不是模态 |
| `claude/trust-dialog.txt` | “Quick safety check … ❯ No, exit / Yes, I trust this folder” | Down + Enter；信任写入 `<配置目录>/.claude.json` 的 `projects.<dir>.hasTrustDialogAccepted` |
| `claude/bypass-permissions-dialog.txt` | `--dangerously-skip-permissions` 的确认：“❯ No, exit / Yes, I accept” | Down + Enter；Esc 等于退出 |
| `claude/api-key-confirmation-dialog.txt` | provider 凭据确认：“Detected a custom API key … ❯ No (recommended)”（虚构的测试密钥） | Up + Enter 选 Yes；用 `ANTHROPIC_AUTH_TOKEN` 时不出现 |
| `claude/write-approval-dialog.txt` | 默认权限模式下 Write 的批准对话框 | Esc 拒绝，回合结束，文件未创建 |
| `cursor/trust-dialog.txt` | “[a] Trust this workspace / [q] Quit”；`agent start` 此时已报告就绪 | `a`；用 `--trust` 不出现，但会写入 `<数据目录>/projects/<编码 cwd>/.workspace-trusted` |
| `cursor/websearch-approval-dialog.txt` | ask 模式下的 Web Search 批准：“Allow search (y) / Run Everything (shift+tab) / Skip (esc or n)” | Esc 拒绝 |
| `cursor/websearch-rejected-after-esc.txt` | 拒绝后该轮继续并给出总结 | — |
| `cursor/slash-quit-on-second-line.txt` | 引导行之后第二行的 `/quit` 被执行（“Session ended.”） | — |
