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
| `claude/two-turns-pong-markdown.txt` | 阶段 8（13.16）：read_only claude 两轮之后（`recent-unwrapped`）。回显是 `❯ <prompt>`，与输入行同一个前缀；思考行 `Thought for Ns (ctrl+o to expand)`；回复以 `● ` 开头、markdown 已渲染；每轮之后一行 `✻ <动词> for Ns · done <时刻>`；页脚是两条 `─` 横线夹着 `❯ ` 输入行，下面是状态行 | — |
| `claude/long-prompt-echo-tail.txt` | 242 行 prompt 的回显末尾、回复和页脚；输入框下方提示 `paste again to expand` | — |
| `copilot/first-turn-pong.txt` | 阶段 8（13.16）：read_only copilot 第一轮之后。回显是 ` ❯ <prompt>`，行尾带时刻；第一轮之后才打印的启动通知（`● MCP Servers reloaded…`、`● Disabled tools…`）出现在回显和回复之间；思考块 `⌄ Thought for Ns` 加 `│ ` 行；回复 `● PONG`；页脚是工作目录行、两条 `─` 横线夹着 `❯` 输入行、状态行 | — |
| `copilot/long-prompt-echo-tail.txt` | 242 行 prompt 之后；屏幕右缘有一列滚动条字符 `┃` | — |
| `codex/first-turn-pong-then-markdown.txt` | 阶段 8（13.15）：full_auto codex 前两轮。第一轮的回显 `› <prompt>` 与输入行同一个前缀；回复 `• `；`Worked for Ns • <时刻>`；输入行 `› Ask Codex to do anything` 之后只有一条状态行 | — |
| `codex/phase2-echo-prefix-reconnecting.txt` | 阶段 2 的真实屏幕（13.31 入库，去掉了环境信息和命令行）：回显 `› Reply with exactly PONG.` 带输入行前缀；网络失败，没有回复；输入行之后只有一条状态行 | — |
| `codex/model-picker-dialog.txt` | 阶段 8（13.31）：`/model` 打开的选择对话框。光标行 `› 4. …` 是编号选项，最后一行是按键提示 `enter select · esc back`；按 D12 的四个条件不被认作输入区，Esc 之后屏幕回到可识别的输入区 | Esc 关闭 |
| `codex/status-{home,spaces,long,enter-title}-after-first-turn.txt` | 阶段 9（13.37 的“完成后”）：真实 codex 第一轮之后的屏幕。状态行第二段：pane 的 `HOME` 指到临时目录、工作目录在它下面时是 `~/proj`；带空格的目录原样显示；223 个字符的目录被按 pane 宽度截断，整行以 `…` 结尾；普通目录原样显示；会话标题取自第一轮，要过一会儿才画出来，`enter-title` 那份的标题是 `Handle enter key`。四份都按 D12 的四条认出 | — |
| `codex/guide-line-prefixes.txt` | 任务 13.8（阶段 11，codex 0.167，full_auto）：引导行之后第二行分别是 `?`、`!touch`（加“不要执行命令”）、`/exit`、`@data.txt`、`-v`、`/status`，六条都作为一条消息交给模型并得到回复，没有本地执行（工作目录里没有生成文件，日志里没有命令执行），`/exit`、`/status` 没有生效。同一次运行里，单行原样的 `#`、`$`、`?` 开头和前导空格加这三种字符开头的 prompt 也都原样交给了模型（codex 去掉了前导空格）。回显都以引导行开头；模型名替换为 `<model>` | — |
