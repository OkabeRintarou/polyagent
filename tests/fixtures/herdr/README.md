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
