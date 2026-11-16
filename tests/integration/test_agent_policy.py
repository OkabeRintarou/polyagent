"""Task 8.4: tool policies against real agents (POLYAGENT_INTEGRATION=1; consumes model quota).

`read_only` runs only where the adapter table says it is verified (claude, copilot); for the other
kinds the test asserts the refusal. The tool-list checks use the local Anthropic-compatible stub (no
quota): it records which tools claude offers to the model.
"""

import json
import sys

import pytest

from polyagent import kinds as K
from polyagent.errors import PolicyNotSupportedError
from polyagent.manager import Marker
from polyagent.spec import ClaudeProvider

from .agentlab import require_kind
from .stubs import AnthropicStub, ProbeServer, write_mcp_stub

pytestmark = pytest.mark.integration

RO_KINDS = [k for k in K.KINDS if K.adapter(k).capability(K.READ_ONLY).state == K.VERIFIED]
NOT_RO_KINDS = [k for k in K.KINDS if k not in RO_KINDS]
NOTE = "notes.txt"
# Tools that write, execute, reach the network or start sub-agents / background work (D8).
FORBIDDEN_CLAUDE_TOOLS = {"Write", "Edit", "NotebookEdit", "Bash", "BashOutput", "KillShell", "WebFetch", "WebSearch",
                          "AskUserQuestion", *K.CLAUDE_ASYNC_TOOLS}


def memory_files(home):
    """Persistent-memory locations: CLAUDE.md files, `memory`/`memories` directories, `memor*` files."""
    return {p for p in home.rglob("*") if p.is_file() and "plugins" not in p.parts and (
        p.name == "CLAUDE.md" or {"memory", "memories"} & set(p.parts) or p.name.lower().startswith("memor"))}


def snapshot(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*") if p.is_file() and ".git" not in p.parts}


@pytest.mark.parametrize("kind", ["codex", "copilot", "claude", "cursor"])
def test_default_policy_is_full_auto(agent_lab, kind):
    """Default: the agent edits files without any approval dialog; the marker records full_auto."""
    require_kind(kind)
    cwd = agent_lab.work(kind, files={NOTE: "old\n"})
    agent = agent_lab.start(kind, cwd)
    assert agent.policy == "full_auto"
    label = agent_lab.pa.herdr.workspace_get(agent.workspace_id)["label"]
    assert Marker.parse(label).policy == "full_auto"
    agent.ask(f"Replace the whole content of the file {NOTE} with the single line: new. Then reply DONE.", 300)
    assert (cwd / NOTE).read_text().strip() == "new"


@pytest.mark.parametrize("kind", NOT_RO_KINDS)
def test_read_only_is_refused_where_not_verified(agent_lab, kind):
    require_kind(kind)
    with pytest.raises(PolicyNotSupportedError):
        agent_lab.pa.start(agent_lab.spec(kind, "read_only"), agent_lab.work(kind))
    assert agent_lab.pa.herdr.workspace_list() == []


@pytest.mark.parametrize("kind", RO_KINDS)
def test_read_only_reads_but_does_not_write_fetch_or_remember(agent_lab, kind):
    """Scenarios: 只读下读取 / 改文件被拒绝 / 联网被拒绝 / 持久记忆 (agent-tool-policy)."""
    require_kind(kind, "read_only")
    cwd = agent_lab.work(kind, files={NOTE: "the code word is HERON-7\n"})
    probe = ProbeServer()
    agent_lab.on_cleanup(probe.close)
    agent = agent_lab.start(kind, cwd, "read_only")
    before = snapshot(cwd)
    home = agent_lab.homes["CLAUDE_CONFIG_DIR" if kind == "claude" else "COPILOT_HOME"]
    memory_before = memory_files(home)

    assert "HERON-7" in agent.ask(f"Read {NOTE} and tell me the code word it contains.", 300)
    agent.ask(f"Overwrite {NOTE} with the text: changed. Also create a new file called made.txt. "
              "If you cannot, just say so.", 300)
    agent.ask(f"Fetch the URL {probe.url} and tell me what it returns. If you cannot, just say so.", 300)
    agent.ask("Remember permanently, in your persistent memory, that my favourite colour is TEAL-QUOKKA. "
              "If you cannot, just say so.", 300)
    assert snapshot(cwd) == before
    assert probe.hits == 0
    # Transcripts, input history and the session index record every prompt; memory locations must not.
    assert memory_files(home) == memory_before
    assert not any(b"TEAL-QUOKKA" in p.read_bytes() for p in memory_files(home) | memory_files(cwd))


def test_claude_read_only_offers_only_read_tools(agent_lab):
    """Scenarios: 只开放只读工具 / 异步子任务 — the tool list claude sends to the (stub) model."""
    require_kind("claude", "read_only", login=False)
    stub = AnthropicStub()
    agent_lab.on_cleanup(stub.close)
    prov = ClaudeProvider.compatible(stub.url, auth_token="stub-token-not-a-secret", model="stub-model")
    agent = agent_lab.start("claude", agent_lab.work("claude-stub"), "read_only", provider=prov)
    assert "ECHO" in agent.ask("hello there", 120)
    offered = {t for r in stub.main_requests() for t in r["tools"]}
    assert offered and {"Read", "Grep", "Glob"} & offered
    assert offered & FORBIDDEN_CLAUDE_TOOLS == set(), offered & FORBIDDEN_CLAUDE_TOOLS
    agent.ask(f"DO_WRITE {agent_lab.root}/w/claude-stub/x.txt", 120)
    assert not (agent_lab.root / "w" / "claude-stub" / "x.txt").exists()


def test_copilot_read_only_log_shows_read_and_search_tools_only(agent_lab):
    """Scenario: 工具白名单之外的内置工具 — copilot has no stub; its session log is the evidence."""
    require_kind("copilot", "read_only")
    cwd = agent_lab.work("copilot-tools", files={NOTE: "the code word is HERON-7\n", "sub/other.txt": "x\n"})
    agent = agent_lab.start("copilot", cwd, "read_only")
    agent.ask("List the files here, search them for the word HERON, then try to run the shell command `id` "
              "and to write a file. Report what worked.", 300)
    log = agent_lab.homes["COPILOT_HOME"] / "session-state" / agent._session_id / "events.jsonl"
    names = set()
    for line in log.read_text().splitlines():
        e = json.loads(line)
        if e.get("type") == "assistant.message":
            names |= {t.get("name") for t in (e.get("data") or {}).get("toolRequests") or []}
    assert names and names <= {"view", "grep", "glob", "report_intent"}, names


def test_claude_mcp_allow_list(agent_lab):
    """Scenarios: MCP 白名单 / 工具级条目 / 仓库自带的 MCP 配置 (claude only)."""
    require_kind("claude", "read_only")
    cwd = agent_lab.work("claude-mcp", git=True)
    stubdir = agent_lab.root / "mcp"
    stubdir.mkdir()
    config, calls = write_mcp_stub(stubdir, sys.executable)
    repo_calls = stubdir / "repo-calls.log"
    (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"repo": {
        "command": sys.executable, "args": [str(stubdir / "mcp_stub.py"), str(repo_calls)]}}}))
    agent = agent_lab.start("claude", cwd, "read_only", mcp_allow=["stub/ping"], mcp_config=str(config))
    agent.ask("Use the MCP tool `ping` of the server named stub and report its result. Then use the MCP tool "
              "`pong` of the same server and report its result. Then use any tool of a server named repo.", 300)
    called = calls.read_text().split() if calls.exists() else []
    assert "ping" in called and "pong" not in called
    assert not repo_calls.exists()


def test_copilot_full_auto_background_command(agent_lab):
    """Scenario: 无法禁用的后台命令 — the reply is this turn's final message; the next ask gets its own."""
    require_kind("copilot")
    cwd = agent_lab.work("copilot-bg")
    agent = agent_lab.start("copilot", cwd)
    first = agent.ask("Start the shell command `sleep 8; echo finished > bg.txt` in the background (async, do not "
                      "wait for it) and then immediately reply with exactly one word: STARTED", 300)
    assert "STARTED" in first
    second = agent.ask("Reply with exactly one word: SECOND", 300)
    assert "SECOND" in second and "STARTED" not in second
