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

环境：herdr 0.9.5；codex 0.167.0、copilot 1.0.112、claude 2.1.322、cursor-agent 2026.11.10。
全部在随机临时 session 中进行，agent 的配置目录隔离到 0700 临时目录（`CODEX_HOME`、`COPILOT_HOME`、
`CLAUDE_CONFIG_DIR`、`CURSOR_CONFIG_DIR`/`CURSOR_DATA_DIR`，cursor 另在 pane 内导出 `XDG_CONFIG_HOME`）。

### 启动命令的输入形式与 `AGENT_ARG` 原样传递

herdr 把启动命令作为一行 shell 输入敲进 pane，参数用 POSIX 单引号转义，例如：

```
$ codex -c 'polyagent_probe=a b (c) "q" '\''r'\'' $(touch PWNED1) ;touch PWNED2 `touch PWNED3` & | > x < * ? ~ $HOME \ end' --no-alt-screen
```

`pane process-info` 显示的 argv 与传入值逐字节相同（codex、claude、copilot、cursor 都一样），工作目录中没有出现
`PWNED*` 文件。copilot 的 `--name` 拒绝含 `"` 的值（这是 copilot 自己的校验，与 herdr 无关）。
cursor 的前台进程是 `cursor-agent --use-system-ca <…>/index.js <args>`。

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
  客户端（未设该变量）随即连不上 server。cursor 的隔离因此只能在 pane 内导出 `XDG_CONFIG_HOME`。
- herdr 对 copilot 的状态识别：每次 `agent prompt --wait` 都返回 `agent_prompt_stalled`，且 copilot 工作中 `agent get` 仍报 `idle`。
- herdr 对 cursor：启动时目录信任对话框仍在屏幕上，`agent start` 已返回就绪。

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
