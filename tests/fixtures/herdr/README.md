# herdr 行为核实记录

环境：herdr 0.9.5，Linux。所有命令都在随机命名的临时 session（`polyagent-test-4f62e93d`）中执行，
调用前剔除了 `HERDR_*` 环境变量（`HERDR_CONFIG_PATH` 除外）；结束后 `session stop` 加 `session delete`，
`herdr session list --json` 与测试前一致。下文用 `$S` 表示临时 session 名。

## 任务 2.1

### server 未运行时的探活

```
$ herdr --session $S workspace list            # rc=1，stdout 为空
stderr: {"id":"cli:workspace:list","error":{"code":"server_not_running","message":"no herdr server is running at ~/.config/herdr/sessions/$S/herdr.sock; run `herdr session attach $S` to start or attach it"}}
```

对不存在的 session 探活不会创建 session 目录。

### 错误输出格式

```
$ herdr --session $S workspace bogus           # rc=2，stderr 为纯文本用法说明，stdout 为空
$ herdr --session $S agent get nosuch          # rc=1
stderr: {"error":{"code":"agent_not_found","message":"agent target nosuch not found"},"id":"cli:agent:get"}
$ herdr --session $S workspace close w99       # rc=1
stderr: {"error":{"code":"workspace_not_found","message":"workspace w99 not found"},"id":"cli:workspace:close"}
```

### session 子命令

```
$ herdr --session $S session list --json       # rc=0；结果不包在 "result" 中
{"sessions":[{"default":true,"name":"default","running":true,...},{"default":false,"name":"$S","running":true,...},...]}
$ herdr session stop $S --json
{"session":{"default":false,"name":"$S","running":false,...},"stopped":true}
$ herdr session delete $S --json               # 只能删除已停止的 session
{"deleted":true,"session":{"default":false,"name":"$S","running":false,...}}
```

### label 往返（151 个 ASCII 字符，含 `:`）

```
$ herdr --session $S workspace create --cwd /tmp --no-focus \
    --label 'polyagent:1:abcdefghij-klmnopqrst_uvwxyz01234:copilot:ro:0b6f2c1e-9a7d-4c43-8f5e-2d1b3a4c5d6e:0123456789abcdef0123456789abcdef:1794804550:extra-pad-xyz'
{"id":"cli:workspace:create","result":{"root_pane":{"pane_id":"w1:p1",...},"tab":{...},"type":"workspace_created","workspace":{"label":"polyagent:1:abcdefghij-...:1794804550:extra-pad-xyz","workspace_id":"w1",...}}}
```

`workspace get w1` 与 `workspace list` 返回的 label 与输入逐字节相同。312 个字符的 label 同样无损保存。

### 重命名

```
$ herdr --session $S workspace rename w1 my renamed ws
{"id":"cli:workspace:rename","result":{"type":"workspace_info","workspace":{"label":"my renamed ws","workspace_id":"w1",...}}}
```

重命名整体替换 label，归属标记随之丢失。

### tokens（`workspace report-metadata`）

```
$ herdr --session $S workspace report-metadata --source polyagent w2 --token pa_kind=copilot
unknown option: polyagent                       # rc=2：--source 不能写在位置参数之前（与 --help 的用法行不符）
$ herdr --session $S workspace report-metadata w2 --source polyagent \
    --token pa_kind=copilot --token pa_policy=ro --token pa_sid=0b6f2c1e-9a7d-4c43-8f5e-2d1b3a4c5d6e
# rc=0，stdout 为空
$ herdr --session $S workspace get w2
{... "tokens":{"pa_kind":"copilot","pa_policy":"ro","pa_sid":"0b6f2c1e-..."}, "workspace_id":"w2"}
```

- 带 `--ttl-ms 3000` 写入的 token 在 5 秒后消失（`tokens` 字段整体缺省）。
- 不带 ttl 的 token 在 3 分钟内的三次检查中都在。
- 重命名 workspace 后 tokens 仍在。
- **server 重启后 tokens 全部丢失**（见下节）；`session.json` 快照中没有 tokens。

### server 重启后的 workspace

正常 `session stop` 后，session 目录中留下 `session.json` 与 `session-snapshots/`，socket 文件被移除。
再次运行 `herdr --session $S server` 后：

```
$ herdr --session $S workspace list
w1 'my renamed ws' tokens=None
w2 'renamed-w2'   tokens=None
$ herdr --session $S agent list
{"id":"cli:agent:list","result":{"agents":[],"type":"agent_list"}}
```

旧 workspace 连同 label 和 cwd 被恢复，pane 中是新的 shell，没有 agent。

### 残留 socket

对临时 session 的 server 发送 SIGKILL 后，`herdr.sock` 与 `herdr-client.sock` 留在 session 目录中；
此时 `workspace list` 返回 `server_not_running`，`session list` 中该 session 为 `"running":false`。
随后 `herdr --session $S server` 正常启动，并同样恢复了旧 workspace。

### 两个 server 同时启动

```
$ herdr --session $S server & herdr --session $S server &
# 第一个：正常运行（"herdr server running; ..."）
# 第二个：rc=1，stderr 为纯文本
error: herdr server is already running
api socket: ~/.config/herdr/sessions/$S/herdr.sock
```

最终只有一个 server 进程。

### 归属标记方案（D4 判定）

tokens 在 server 重启后不保留，不满足 D4 采用备选方案的条件，因此维持“全部字段编码在 label 中”：
`polyagent:1:<name>:<kind>:<ro|fa>:<sid|->:<disc|->:<created_unix>`。
server 重启后恢复出的 workspace 带原 label 但没有存活 agent，按孤儿回收。

## 阶段 1 返工补充（评审 C10/T5）

在另一个临时 session 中记录了两条命令成功时的输出形状：

```
$ herdr --session $S pane run w1:p1 'true'      # rc=0，stdout 为空
$ herdr --session $S workspace close w1         # rc=0
{"id":"cli:workspace:close","result":{"type":"ok"}}
```

`pane run` 成功时没有输出，因此库对 `pane run`、`workspace close`、`agent send-keys` 只看退出码，不解析 JSON。

## 任务 3.1（agent 启动相关，阶段 2）

环境：herdr 0.9.5；codex 0.167.0、copilot 1.0.112、claude 2.1.322。
全部在随机临时 session 中进行，agent 的配置目录隔离到 0700 临时目录（`CODEX_HOME`、`COPILOT_HOME`、
`CLAUDE_CONFIG_DIR`）。

### 启动命令的输入形式与 `AGENT_ARG` 原样传递

herdr 把启动命令作为一行 shell 输入敲进 pane，参数用 POSIX 单引号转义，例如：

```
$ codex -c 'polyagent_probe=a b (c) "q" '\''r'\'' $(touch PWNED1) ;touch PWNED2 `touch PWNED3` & | > x < * ? ~ $HOME \ end' --no-alt-screen
```

`pane process-info` 显示的 argv 与传入值逐字节相同（codex、claude、copilot 都一样），工作目录中没有出现
`PWNED*` 文件。copilot 的 `--name` 拒绝含 `"` 的值（这是 copilot 自己的校验，与 herdr 无关）。

### 别名与函数

因为启动命令经 shell 解释，pane 中的别名和函数会改变实际 argv：

| pane 中的定义 | `agent start` 后的 argv |
|---|---|
| `alias codex='codex --dangerously-bypass-approvals-and-sandbox'` | `codex --dangerously-bypass-approvals-and-sandbox --no-alt-screen` |
| `claude(){ command claude --dangerously-skip-permissions "$@"; }` | `claude --dangerously-skip-permissions` |
| `copilot(){ command copilot --yolo "$@"; }` | `copilot --yolo` |

执行 `unalias codex claude copilot; unset -f codex claude copilot` 后再启动，argv 恢复为 `codex --no-alt-screen`、`claude`、`copilot`。

### 准备脚本之后立即 `agent start`

连续 3 轮：`source <0600 文件> && rm -f <文件> && echo __pa_env_ok_$((1+1)) || echo __pa_env_fail_$((1+1))`，
`pane wait-output --regex '__pa_env_(ok|fail)_2'` 命中后立即 `agent start codex`：3/3 成功（约 3.8 s），
临时文件已删除，agent 进程环境中有脚本导出的变量。

### agent 退出后

对 agent 进程发 SIGTERM 后：`agent get <name>` → `agent_not_found`；`agent list` 不再包含它（也没有无名条目）；
workspace 仍在；pane 前台回到 `/bin/bash`。

### 名字冲突

```
$ herdr --session $S agent start dup --kind codex --pane w5:p1 ...
stderr: {"error":{"code":"agent_name_taken","message":"agent name dup is already used; candidates: terminal_id=... pane_id=w4:p1 workspace_id=w4 ..."}}
```

### `agent send-keys`

| 输入 | 结果 |
|---|---|
| `hello`、`/allow-all`、`x y` | `invalid_key`，没有输入 |
| `a b`（两个单字符参数） | 接受，输入框出现 `ab` |
| `ctrl+u`、`up`、`tab`、`Escape`、`esc`、`C-c` | 接受 |

herdr 接受单个可打印字符，因此可以逐字输入命令；库的 `send_keys` 只放行命名按键是必要的。
codex 空闲时收到 `C-c` 会直接退出（打印 `resume <id>`）。

### codex 的后台 app-server 进程

不带 `--no-daemon` 时，codex 0.167 会从 `CODEX_HOME/packages/app-server-daemon/` 启动两个 `codex app-server` 进程，
`workspace close` 和 `session stop` 之后它们仍在运行（脱离了 herdr 的进程树）。带 `--no-daemon` 时不产生这类进程。

### 其它

- 服务器环境中的 `XDG_CONFIG_HOME` 会让 herdr 自己把 session 目录移到 `$XDG_CONFIG_HOME/herdr/sessions/`，
  客户端（未设该变量）随即连不上 server。
- herdr 对 copilot 的状态识别：每次 `agent prompt --wait` 都返回 `agent_prompt_stalled`，且 copilot 工作中 `agent get` 仍报 `idle`。

### `XDG_CONFIG_HOME` 搬走 session 目录（任务 13.9，只用 herdr，herdr 0.9.5）

临时 session `$S`（`polyagent-test-` 加随机后缀），`$T` 为 `mktemp -d` 目录；命令均不带 `HERDR_*`（`HERDR_CONFIG_PATH` 除外），
前后 `herdr session list` 中的名字一致（只有测试前已有的 session）。

```
# 1. 以 XDG_CONFIG_HOME=$T 拉起 server
$ XDG_CONFIG_HOME=$T setsid herdr --session $S server &
$ ls $T/herdr/sessions
$S
$ ls ~/.config/herdr/sessions/$S
ls: cannot access '~/.config/herdr/sessions/$S': No such file or directory

# 2. 不带 XDG_CONFIG_HOME 的客户端找不到它
$ herdr --session $S workspace list
{"id":"cli:workspace:list","error":{"code":"server_not_running","message":"no herdr server is running at ~/.config/herdr/sessions/$S/herdr.sock; run `herdr session attach $S` to start or attach it"}}
[rc=1]
$ herdr session list --json        # 不含 $S

# 3. 带同一个 XDG_CONFIG_HOME 的客户端可以连上
$ XDG_CONFIG_HOME=$T herdr --session $S workspace list
{"id":"cli:workspace:list","result":{"type":"workspace_list","workspaces":[]}}
$ XDG_CONFIG_HOME=$T herdr session list --json   # 含 $S，running: true

# 4. 不带 XDG_CONFIG_HOME 再拉起同名 server：成功，同一个名字同时有两个 server 进程
$ setsid herdr --session $S server &
$ herdr --session $S workspace list
{"id":"cli:workspace:list","result":{"type":"workspace_list","workspaces":[]}}
$ pgrep -fc -- "--session $S server"
2

# 清理：两处各自 stop + delete
$ XDG_CONFIG_HOME=$T herdr session stop $S --json
{"session":{"default":false,"name":"$S","running":false,"session_dir":"$T/herdr/sessions/$S","socket_path":"$T/herdr/sessions/$S/herdr.sock"},"stopped":true}
$ XDG_CONFIG_HOME=$T herdr session delete $S --json
{"deleted":true,"session":{"default":false,"name":"$S",...,"session_dir":"$T/herdr/sessions/$S",...}}
$ herdr session stop $S --json
{"session":{"default":false,"name":"$S","running":false,"session_dir":"~/.config/herdr/sessions/$S",...},"stopped":true}
$ herdr session delete $S --json
{"deleted":true,...}
```

结论：herdr 的 session 目录（含 socket）是 `${XDG_CONFIG_HOME:-~/.config}/herdr/sessions/<name>/`。
环境不同的调用方对同一个名字会看到不同的 server，甚至各自拉起一个。库因此在所有 herdr 调用和 server 环境中剔除
`XDG_CONFIG_HOME`，并拒绝把它列为透传变量（design D1、D3）。

## 任务 13.13（部分）：按键名与返回结构（herdr 0.9.5，阶段 4 第二轮返工）

只用 herdr 和随机 `polyagent-test-*` session，没有启动任何 agent CLI；结束后 `session stop` + `delete`，session 列表前后都是
`default`、`racer`。

### 按键名

`agent send-keys` 只接受 `agent start` 得到的具名 agent：`pane report-agent` 登记的 pane 报 `agent_not_ready`
（“is not an active named agent”），假可执行文件（PATH 里一个名为 `pi` 的脚本）`agent start` 超时，未被识别为 agent。
所以按键名是在同一个 pane 上用 `pane send-keys` 核对的：pane 里 `stty raw -echo; cat -v`，每个键之后再发一个 `|`。

| 按键名 | `pane send-keys` | `cat -v` 看到的字节 |
|---|---|---|
| `enter` | 接受 | `^M` |
| `esc`（`escape` 同） | 接受 | `^[` |
| `tab` | 接受 | 制表符 |
| `up`、`down`、`left`、`right` | 接受 | `^[[A`、`^[[B`、`^[[D`、`^[[C`（`Down` 也接受） |
| `backspace` | 接受 | `^?` |
| `ctrl+u` | 接受 | `^U` |
| `ctrl+c`（`C-c` 同） | 接受 | `^C` |
| `bogus-key` | `invalid_key`（`unsupported key bogus-key`），没有写入任何字节 | — |

herdr 的说明（`herdr --skill`）：发送前先校验全部按键，再写入字节。`agent send-keys` 对同一组名字的核对见下文“任务 13.13（完成）”。

### 返回结构（只列库和假 herdr 用到的字段）

- `workspace create --cwd <含空格的目录> --label L`：`result.workspace.{workspace_id,label}`，
  `result.root_pane.{pane_id,workspace_id,cwd,agent_status}`；另有 `result.tab`、`result.type == "workspace_created"`。
  工作目录含空格时原样返回。
- `pane process-info --pane P`：`result.process_info.{pane_id,shell_pid,foreground_process_group_id,foreground_processes}`，
  `foreground_processes` 的元素是 `{pid,name,argv,cmdline,cwd}`；空闲的 bash pane 里前台进程就是 shell 自己（`pid == shell_pid`）。
- `agent list` / `agent get`（`pane report-agent` 登记的条目）：`agent`（kind 标签）、`agent_status`、`state_change_seq`、
  `cwd`、`foreground_cwd`、`pane_id`、`workspace_id`、`terminal_id`、`revision`；这种条目没有 `name`。具名 agent 的
  `name` 字段与 `agent start` 的返回结构仍待第 8 组核对。
- `agent explain`：没有检测到 agent 标签的目标返回错误码 `agent_explain_unavailable`（退出码 1）。库的超时错误在这种情况下
  不带诊断文本（D11），与“完成前的行为”一致。
- `agent explain <具名 agent>`（阶段 5，copilot 启动后停在信任对话框时）：输出是**纯文本**，不是 JSON，退出码 0：

  ```
  agent: copilot
  state: blocked
  manifest: remote:~/.local/state/herdr/agent-detection/remote/copilot.toml 2026.08.29.1
  rule: selection_blocker (region=whole_recent priority=300)
  evidence: " [Current]  Sessions   Issues   Pull requests   Gists\n\n╭───…│ …"
  ```

  `evidence:` 是规则匹配到的屏幕原文（截断），可能含 prompt 的回显；`manifest:` 含用户主目录下的路径。库的超时错误
  只保留 `agent:`、`state:`、`rule:` 三行（`agent._diagnostics`），其余由架构师决定（阶段 5 评审 A7）。

## 任务 13.13（完成）：具名 agent 上的按键名与返回结构（herdr 0.9.5，阶段 7）

用例：`tests/integration/test_herdr_contract.py::test_named_agent_contract`。在实验室的临时 session 里以 claude 对着本地的
Anthropic 兼容桩（标准库，127.0.0.1）启动一个具名 agent，不消耗额度、没有请求离开本机。结束后 session 列表前后都是
`default`、`racer`。

- `agent send-keys <具名 agent> <键>`：库用到的全部按键名（`agent.KEYS` 的值：`enter`、`esc`、`tab`、`up`、`down`、
  `left`、`right`、`backspace`、`ctrl+u`、`ctrl+c`）逐个发送，退出码都是 0。`bogus-key` 返回退出码 1，错误码 `invalid_key`。
- `agent get <名字>` 的 `result.agent` 与 `agent list` 中对应条目的键相同：
  `agent`（kind 标签）、`agent_status`、`cwd`、`focused`、`foreground_cwd`、`interactive_ready`、`name`、`pane_id`、
  `revision`、`state_change_seq`、`tab_id`、`terminal_id`、`terminal_title`、`terminal_title_stripped`、`workspace_id`。
  没有安装 herdr 自己的 integration 时**没有 `agent_session` 这个键**（不是 null）。库只用 `name`、`workspace_id`、
  `pane_id`、`agent_status`、`state_change_seq`，`agent_session` 用 `.get()` 读，缺省时按 D12 的其它方式绑定日志。
- `pane process-info --pane P` 的 `result.process_info`：`foreground_process_group_id`、`foreground_processes`、
  `pane_id`、`shell_pid`；`foreground_processes` 的元素：`argv`、`cmdline`、`cwd`、`name`、`pid`。agent 运行时前台进程里
  有一个 `pid != shell_pid` 的条目。
- 假 herdr（`tests/fakeclient.py`）的 `add_agent` 按上面的键返回条目，同样没有 `agent_session`。

## 任务 13.19：创建 workspace 时给 pane 的 shell 设环境变量（herdr 0.9.5，阶段 7）

`herdr workspace create --help`：`--env <KEY=VALUE>  Set an environment variable for the launched process`，可重复。

用例：`tests/integration/test_herdr_contract.py::test_workspace_create_env_reaches_the_pane_shell`。

```
$ herdr --session $S workspace create --cwd <dir> --label pa-env-probe --no-focus \
    --env 'PA_ENV_PROBE=hello world' --env HISTFILE=<临时根>/probe-history
$ herdr --session $S pane run <pane> 'echo "P=[$PA_ENV_PROBE] H=[${HISTFILE-unset}]"'
$ herdr --session $S pane read <pane> --source recent --lines 20
P=[hello world] H=[<临时根>/probe-history]
```

结论：这样设置的变量进入 pane 的 shell（值含空格也原样）。按 D6 和本任务的“完成后”，库创建每个 workspace 时都带
`--env HISTFILE=/dev/null`（`manager.PANE_ENV`），不执行准备脚本的启动同样不写 shell 历史；这个值不是凭据，出现在
argv 里没有问题。真实 pane 已核对：库启动的 agent 所在 pane 的 shell 环境里有 `HISTFILE=/dev/null`
（`test_named_agent_contract`）。

## 任务 13.17：准备脚本在 zsh、ksh 里的表现（阶段 4 第二轮返工）

本机没有这两种 shell，也没有 root；用 `apt-get download zsh ksh93u+m` 下载、`dpkg-deb -x` 解到临时目录使用
（zsh 5.9，ksh 93u+m/1.0.8）。不需要 agent。

1. 非交互（`sh -c`）与交互（`-i -c`）各跑一次 `tests/test_prep_shell.py` 的做法：
   - zsh：`command` 只执行外部命令，不执行内建命令（POSIX_BUILTINS 未开时的 zsh 行为）。`\command export K=…`、
     `\command unset …`、`\command unalias …`、`\command set +xv` 全部报 `command not found`。后果：只注入变量的
     `full_auto` 启动时变量没有导出，脚本仍以 0 结束（`__pa_env_ok`）；关闭回显的那一行无效，`set -v` 下导出行连同值显示在屏幕上。
     `read_only` 与显式 provider 由复核拦下（`permission_env`、`provider_env`）。
   - ksh93：清除、导出、复核都正常；但 `set -v` 下 `.` 读入整个脚本后先整段回显，再执行第一行的 `set +xv`，导出行连同值
     出现在屏幕上。
2. 真实 pane（临时 session，pane 里 `exec zsh -f` / `exec ksh`，手工 `set -v; . <脚本>`）：zsh 屏幕上出现
   `command not found: export` 和导出的值，变量未设置；ksh 屏幕上出现导出的值，变量已设置。两者的 `pane process-info`
   前台进程名分别是 `zsh`、`ksh`。

结论：两者都不通过，按 13.17 的“完成后”从接受的名单里去掉（`_launch.POSIX_SHELLS` 只剩 bash、sh、dash），
pane shell 是 zsh 或 ksh 时启动抛 `HerdrError(code="unsupported_shell")`（真实 pane 已确认）。
