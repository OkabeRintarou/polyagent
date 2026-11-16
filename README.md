# polyagent

用 Python 驱动本机已安装的编码 agent CLI（codex、copilot、cursor-agent、claude），并让多个 agent 在一个协调者的调度下围绕需求讨论、产出结构化总结。agent 由 [herdr](https://herdr.dev) 在独立命名的 session 中托管。库只提供 Python API（Python ≥ 3.10，运行时只依赖标准库），没有 CLI、HTTP 服务或数据库。

> **先读这一节：工具策略的默认值和前提**
>
> - **单独启动的 agent 默认 `full_auto`。** agent 可以创建、修改、删除文件，执行任意命令，访问 web，调用全部已配置的 MCP 工具，并能读到注入给它的环境变量和 provider 凭据。自动信任目录之后，仓库自带的 hooks 和配置也会生效。库在这一档不做任何限制，prompt 里夹带的外部文本可以诱导模型做这些事。
> - **建议**：以低权限的专用操作系统用户运行调用方服务和 herdr server；只注入必要的凭据；处理不受信任的仓库或外部输入时显式选择 `ToolPolicy.READ_ONLY`。
> - **讨论成员固定 `read_only`**，不能改为 `full_auto`（指定了就在构造 `Discussion` 时报参数错误）。
> - **`read_only` 的前提**：用户级 agent CLI 配置（hooks、插件、MCP、批准记录）和 shell 启动文件没有被篡改。同一操作系统用户下的 `full_auto` agent 能改写这些文件，此后启动的 `read_only` agent 和讨论成员会加载它们。**`full_auto` 的 agent 和只读讨论应分别用不同的操作系统用户运行**，至少为讨论使用专用用户。只换 herdr session 不起作用：session 隔离的是终端，不是用户的文件。

## 安装与准备

- Linux；已安装 herdr 0.9.5 或更高（`herdr` 在 `PATH` 上，或用 `herdr_path` 指定）以及要用的 agent CLI，并各自登录过。
- 运行库的用户必须在 passwd 里有条目，主目录可写：锁文件放在 `~/.local/state/polyagent/<session>/`。
- 需要准备 pane 环境的启动（`read_only`、cursor、显式 provider、注入变量）要求 pane 的 shell 是 bash、sh 或 dash。默认 shell 是 zsh 或 ksh 的用户用不了这些功能（实测两者都会让准备脚本无声失效或回显凭据）。

## 用法

```python
from polyagent import AgentSpec, ClaudeProvider, PolyAgent, ToolPolicy

pa = PolyAgent(session="my-service")            # 不存在时自动拉起无界面 server；default session 被拒绝

# 单独启动：默认 full_auto
coder = pa.start(AgentSpec(kind="codex"), "/srv/repo")
print(coder.ask("Run the tests and report the failures.", timeout=600))
coder.close()

# 只读：claude 必须给出显式的非官方 provider
provider = ClaudeProvider.compatible("https://llm.example.com", auth_token="<token>", model="<model>")
reader = pa.start(AgentSpec(kind="claude", policy=ToolPolicy.READ_ONLY, provider=provider), "/srv/repo")
reply = reader.ask_reply("Explain src/app.py.", timeout=300)
print(reply.text, reply.source)                   # source: "session_log" 或 "screen"
```

讨论（完整示例见 `help(polyagent.Discussion)`）：

```python
from polyagent import Discussion, Participant

d = Discussion(pa, coordinator=AgentSpec(kind="claude", provider=provider),
               participants=[Participant("critic", "Finds risks", AgentSpec(kind="claude", provider=provider)),
                             Participant("builder", "Proposes a design", AgentSpec(kind="copilot"))],
               cwd="/srv/repo")
record = d.run("Add a cache in front of the user API", on_event=lambda e: save(e.to_dict()))
```

`record.to_dict()` 只含 JSON 原生类型，可以直接存库；`DiscussionRecord.from_dict()` 还原，任何不合格的记录都报参数错误。

## 各 kind 的能力状态

**未验证按不支持处理**：某项能力没有经过实测验证时，库拒绝使用它（`PolicyNotSupportedError`），错误消息里写明对应的核实任务。

| kind | `read_only` | MCP 白名单 | 会话日志 | 可作讨论成员 | 可作协调者 |
|---|---|---|---|---|---|
| claude | 支持（需显式的非官方 provider） | 支持（`mcp_config` 给出定义） | 支持 | 是 | 是 |
| copilot | 支持 | 未验证 | 支持 | 是 | 是 |
| codex | 未验证（需主机允许非特权 user namespace，且用户级规则目录为空） | 不支持 | 支持 | 否（`read_only` 未验证） | 否 |
| cursor | 不支持 | 不支持 | 无（回复走屏幕截取） | 否（不支持多行 prompt） | 否 |

各 kind 的差异：

- **claude**：`read_only` 用 `--restricted` 启动，会连同用户 settings 里的登录一起忽略，所以要显式给出兼容端点、Bedrock 或 Vertex provider；官方登录在只读下暂不支持。两档下都不能用子 agent、工作流、后台任务、`Skill`、计划模式和提问表单（这些工具会让回复与提问对不上，或停下等人）。目录信任对话框的应答和 `full_auto` 的确认会写入用户配置。
- **copilot**：`read_only` 下模型只看得到读文件、搜索和列目录三个工具，它只会在回复里写出命令而不执行。两档下都不能用子 agent 和工作流。`full_auto` 的 copilot 以后台方式启动的命令，结果不会作为回复返回：需要结果时在 prompt 里要求它同步执行。被中断之后（中断键待验证）不能再提问，需要关闭后重新启动。目录信任只在本次会话有效，不持久保存。
- **codex**：库带 `--no-daemon` 启动，不留后台服务进程；目录信任只对本次运行有效，不写配置。
- **cursor**：只支持 `full_auto` 和单行 prompt，以 `/` 开头的 prompt 被拒绝；`--trust` 会持久保存目录信任；启动前库会阻止它自动升级用户的安装。

### 带仓库级配置的仓库

`read_only` 会从工作目录向上到仓库根（按解析符号链接后的真实路径确定，字面路径的那条链也一并检查）查找该 kind 会加载的仓库级可执行配置，排除不了就拒绝启动：

| 仓库里有 | claude | copilot | codex | cursor |
|---|---|---|---|---|
| `.mcp.json`、`.vscode/mcp.json` | 可以（参数排除项目级配置） | 拒绝 | — | — |
| `.github/hooks/`、`.github/copilot/settings.json`、`.claude/settings*.json` | 可以 | 拒绝 | — | — |
| `.codex/` | 可以 | 可以 | 拒绝 | — |
| `.cursor/` | 可以 | 可以 | 可以 | — |

“—”表示该 kind 的 `read_only` 本身不可用。讨论的全部成员（含协调者）都按这张表检查讨论的工作目录；在这类仓库里讨论往往只剩 claude 可用。

## 配置要点

- **`AgentSpec`**：`kind`（`cursor-agent` 是 `cursor` 的别名）、`model`（首字符必须是字母或数字）、`extra_args`、`env`、`provider`（仅 claude）、`policy`、`mcp_allow`、`mcp_config`、`auto_trust`。
  - `extra_args` 两档下都不能含库保留的选项（库自己设置的、改变会话身份或运行方式的）和 `--`；以子命令或初始 prompt 改变运行方式不受支持。短选项和它的值要写成两个元素。`read_only` 下另有很小的白名单。
  - `mcp_allow` 条目写作 `server` 或 `server/tool`，名字匹配 `[A-Za-z0-9._-]+`，不含 `__`，不以 `_` 开头或结尾；只在 `read_only` 下有意义。白名单内的 MCP server 自身可能联网或写入，由调用方负责。
  - `env` 的值以 0600 临时文件注入 pane，不进入 argv。注意 `dataclasses.asdict(spec)` 的结果里有 `env` 的值：不要用会展开映射的编码器序列化配置对象。
- **`ClaudeProvider`** 只经类方法构造：`official()`；`compatible(base_url, *, auth_token=None, api_key=None, model=None, small_fast_model=None, custom_headers=None)`（`auth_token` 与 `api_key` 恰好给一个；`custom_headers` 可以有多个请求头，值按原样发送并按凭据对待）；`bedrock(region, *, profile=None, access_key_id=None, secret_access_key=None, session_token=None, bearer_token=None, model=None, small_fast_model=None)`；`vertex(region, project_id, *, credentials_file=None, model=None, small_fast_model=None)`。
  - 兼容端点的地址不能带用户信息、查询串或片段（凭据只能经 `auth_token`、`api_key`、`custom_headers` 给出）。
  - 显式 provider 会先清掉 pane 里继承的 `ANTHROPIC_*`、`CLAUDE_CODE_USE_*`、`AWS_*`、`GOOGLE_*`、`CLOUD_ML_REGION`，依赖这些环境变量的凭据来源（例如继承的 AWS 配置文件变量）因此不可用，要在 provider 里显式给出。
  - `repr`、`to_dict()`、`dataclasses.asdict()` 只有类型名；provider 对象可以 pickle，pickle 的结果**包含凭据**。
- **`PolyAgent(session="polyagent", herdr_path=None, server_start_timeout=15, server_env_passthrough=())`**：server 环境按最小清单构造（`PATH`、`HOME`、区域设置、终端、代理、`XDG_*`、`HERDR_CONFIG_PATH`），调用方进程里的其它变量要经 `server_env_passthrough` 显式透传或经 `AgentSpec.env` 注入；会改变权限或在每个 pane 的 shell 里执行的名字（`BASH_ENV`、`PROMPT_COMMAND`、各 CLI 的配置变量族等）不能透传。库的 herdr 调用不带 `XDG_CONFIG_HOME`（它会改变 herdr 的 session 目录）；herdr 配置文件的位置用 `HERDR_CONFIG_PATH` 指定。

## 提问

- `ask(text, timeout)` / `ask_reply(...)`：超时必须是有限正数，**建议不短于 30 秒**。更短的超时之后立即再次提问，可能得到“agent 忙碌”或“未找到回复”。同一 agent 的提问在线程、句柄和进程之间串行。
- 超时抛 `AgentTimeoutError`，其 `pending` 可交给 `wait_reply(pending, timeout)` 继续等待同一轮的回复；`pending` 必须用在同一个 agent、同一个会话上。会话日志绑定的回合不带 prompt 文本；屏幕截取的回合带 prompt 首行的前 60 个字符（用来在屏幕上定位回显）。超时错误的诊断文本只有 herdr 报告的状态和规则名。
- `cancel` 事件在 prompt 提交之前置位时什么都不发送，agent 正在进行的上一轮不受影响。
- 回复优先取自会话日志（codex、copilot、claude），来源标注在 `Reply.source`。屏幕截取目前只用于 cursor 和 codex：它取的是渲染后的文本，无法识别 agent 级错误；屏幕底部认不出输入区时，提交前抛“agent 忙碌”、结束后抛“未找到回复”，不返回文本。claude 和 copilot 的会话日志不可用时提问报“未找到回复”。
- `full_auto` 下 agent 停在对话框时抛 `AgentBlockedError`（带屏幕），用 `dismiss()` 以拒绝键关掉对话框后可继续提问；`read_only` 下对话框自动拒绝。`send_keys()` 只接受命名按键（`enter`、`esc`、`tab`、方向键、`backspace`、`ctrl+c`、`ctrl+u`），任意文本只能经 `ask` 发送。
- agent 能读到自身进程环境里的变量和凭据（`read_only` 下 copilot 和 codex 对 shell 工具隐藏注入的变量）。

## 终端环境与 shell

- 准备 pane 环境时，库清掉这个 pane 的提示符钩子（`PROMPT_COMMAND`、`PS0`）和 DEBUG/RETURN/ERR trap，依赖它们的 shell 集成在 agent 的 pane 里不起作用。
- 库创建的每个 pane 的 shell 都以 `HISTFILE=/dev/null` 启动，准备脚本另外取消历史文件，herdr 敲入的命令行（不含凭据）不会进入运行 server 的用户的 shell 历史。
- 各 kind 的目录信任：copilot、codex 只对本次会话/运行有效；claude、cursor 会持久保存。可用 `auto_trust=False` 关闭自动信任。

## 部署与运维

- **server 生命周期**：`PolyAgent` 构造时探测不到 server 就在后台拉起一个，它脱离调用方的进程组，调用方退出后继续运行。用 systemd 部署时二选一：服务单元设 `KillMode=process`（停服务时只结束主进程，server 和 agent 保留）；或为 herdr server 单独写一个单元（`herdr --session <名字> server`），调用方服务依赖它。默认的 `KillMode=control-group` 会在停服务时连带杀掉 server 和全部 agent。
- `stop_server()` 结束这个 session 的 server，**所有进程**在该 session 里的 agent 都会被杀掉。之后再次拉起，旧 workspace 会作为孤儿回来，调用 `cleanup()` 关闭。
- herdr 升级后旧 server 仍在运行时，调用报 `protocol_mismatch`（`HerdrUnavailableError`）：停止该 session 的 server（`herdr session stop <名字>`）后重新拉起。库不会自动重启 server，那会杀掉全部 agent。
- **孤儿回收**：库创建的 workspace 带归属标记。`cleanup()` 关闭 agent 已经不在的 workspace；`cleanup(discussion_id=...)` 关闭某次讨论的全部 agent；`cleanup(older_than=...)` 按创建时间关闭。重命名 workspace 会抹掉归属标记，使它脱离库管理。
- **资源**：实测空闲时单个 agent 进程的常驻内存，claude 约 210 MiB，copilot 约 350 MiB（本机，claude 2.1.322、copilot 1.0.112）。库不限制并发数量，按机器内存自行限流。

## 讨论

- 一个 `Discussion` 只能运行一次（配置错误抛出的那次不算）。`Discussion.id` 在运行之前就能读到：运行讨论的进程异常退出时，用 `PolyAgent.cleanup(discussion_id=...)` 回收。关闭 agent 失败只记 WARNING 日志（logger 名 `polyagent`）；需要确定没有残留时，每次运行之后调用一次 `cleanup(discussion_id=...)`。
- `cancel()` 可以从任意线程调用；运行之前调用，`run()` 立即返回 `cancelled` 记录且不启动 agent；成员启动期间调用，要等各成员的启动返回。
- `keep_agents=True` 时，无论完成、失败、取消还是内部错误，agent 都保留；只有成员启动失败时库仍关闭已启动的成员。
- 结束原因：`finished`、`budget_exhausted`、`coordinator_invalid`、`participants_unavailable`（这四种可能是 `completed`）、`summary_invalid`、`coordinator_failed`、`start_failed`、`callback_error`、`cancelled`，以及 `internal_error`：库自身的缺陷或没有覆盖到的输入，请连同记录里的 `error` 一起报告。
- **脱敏**：成员的环境变量值、provider 凭据值和请求头的值（不少于 8 个字符）在记录、事件和成员之间转发的文本里都替换为 `***`，只做逐字匹配。所以不要把普通词语作为变量值注入讨论成员，否则讨论内容里的这个词也会被替换。协调者输入里库生成的分隔行带每场讨论随机生成的标记，这只降低参与者伪造回复块的成功率，不构成保证。
- **记录里的消息**：`brief` 的 `content` 是需求摘要对象；`dispatch` 是 `{"question", "context"?}`（总时长恰好耗尽、没有执行的分派状态为 `cancelled`）；`reply` 是回复全文（非 `ok` 时是错误说明）；`finish` 是 `{"reason"}`；`summary` 是总结对象；`coordinator_output` 是未通过校验的协调者输出 `{"output", "errors"}`。
- **总结模板**（`SummaryTemplate`）的 schema 是 JSON Schema 的子集：关键字只有 `type`、`properties`、`required`、`items`、`enum`、`description`；每个节点都要有 `type`；`array` 要有 `items`；对象封闭；嵌套不超过 32 层；写作要求加 schema 合计不超过 16 KiB。`validate()` 返回全部问题，不抛异常。

## 测试

- 单元测试：`uv run pytest`（不需要 herdr，也不启动 agent）。
- 集成测试：`POLYAGENT_INTEGRATION=1 uv run pytest -m integration`。它们使用随机命名的临时 herdr session，消耗各 agent 的模型额度（claude 的 provider 用例走本地桩，不耗额度）。
  - 登录态是从用户配置**复制**到临时隔离目录的；某些 CLI 会在使用时轮换令牌，复制品里的令牌可能因此让用户的原登录失效或被刷新。
  - 启动真实 cursor 的用例默认不运行：cursor 即使隔离了配置目录，仍会写 `~/.cursor/sandbox-policies`、安装目录下的 `.running/`、`~/.cursor/ai-tracking`、`~/.cursor/skills-cursor`、`~/.cursor/managed`。要运行另设 `POLYAGENT_INTEGRATION_CURSOR=1`。
  - 测试进程被强杀后，下一次运行会在开始时报告遗留（带标记的进程、`/tmp/pa-it-*`、`polyagent-test-*` session、`~/.cache/polyagent-test-backup`）并中止，按提示手工处理：先结束进程，再 `herdr session stop <名字>` 与 `herdr session delete <名字>`，按备份里的 `manifest.json` 恢复后删除备份。
