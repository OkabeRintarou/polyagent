"""Task 13.2: are claude's asynchronous, background and form tools really gone? (POLYAGENT_INTEGRATION=1)

claude runs against the local Anthropic-compatible stub (standard library, 127.0.0.1), so no quota is used.
In both tool policies: the tool definitions claude sends to the "model", the names it lists in its system
text, and what its deferred-tool search (``ToolSearch``) returns for every denied name, by exact selection
and by keyword. Control: under full_auto the same search does find a deferred tool that is not denied
(``WebFetch``), so "no match" is evidence and not a broken search. Findings: tests/fixtures/README of
phase 7 and the design write-back (Skill, the plan-mode pair and DesignSync were added to the lists).
"""

import sys

import pytest

from polyagent import kinds as K
from polyagent.spec import ClaudeProvider

from .agentlab import require_kind
from .stubs import AnthropicStub, write_mcp_stub

pytestmark = pytest.mark.integration

TOKEN = "stub-token-not-a-secret-0003"
NO_MATCH = "No matching deferred tools found"
# Written out here, independent of the library's lists, so that dropping a name from them fails this test.
MUST_BE_DENIED = {"Agent", "Workflow", "CronCreate", "ScheduleWakeup", "Monitor", "SendMessage", "TaskCreate",
                  "EnterWorktree", "PushNotification", "Skill", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode"}
MUST_BE_DENIED_READ_ONLY = {"Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "DesignSync",
                            "ReportFindings"}
KEYWORDS = ("agent", "task", "cron", "schedule", "monitor", "worktree", "notification", "background", "plan",
            "skill", "design")


def _system_text(body):
    sysv = body.get("system")
    if isinstance(sysv, str):
        return sysv
    return "\n".join(b.get("text", "") for b in sysv or [] if isinstance(b, dict))


PLACEHOLDER = "DeferredToolPlaceholder"  # claude: "Reserved placeholder ...; never call this tool"


def _assert_only_read_tools(agent, stub, offered):
    assert offered - {PLACEHOLDER} == {"Read", "Grep", "Glob"}, offered
    for command in ("DO_PLACEHOLDER", "DO_TOOLSEARCH WebFetch"):
        reply = agent.ask(command, 120)
        print(command, "->", reply[:160])
        assert reply.startswith("TOOLRESULT ERR"), (command, reply)


def _search(agent, stub, query, kind="DO_TOOLSEARCH"):
    agent.ask(f"{kind} {query}", 120)
    (result,) = stub.records[-1]["tool_results"]
    assert not result["is_error"], result
    return result["content"]


@pytest.mark.parametrize("policy", [None, "read_only"])
def test_denied_tools_are_neither_offered_nor_searchable(agent_lab, policy):
    require_kind("claude", policy, login=False)
    stub = AnthropicStub()
    agent_lab.on_cleanup(stub.close)
    prov = ClaudeProvider.compatible(stub.url, auth_token=TOKEN, model="stub-model")
    agent = agent_lab.start("claude", agent_lab.work("tools"), policy, provider=prov)
    agent.ask("hello", 120)
    denied = set(K.CLAUDE_ASYNC_TOOLS) | set(K.CLAUDE_FORM_TOOLS) | MUST_BE_DENIED
    if policy == "read_only":
        denied |= set(K.CLAUDE_READ_ONLY_DENY) | MUST_BE_DENIED_READ_ONLY
    offered = {t.get("name") for r in stub.main_requests() for t in r["body"].get("tools") or []}
    assert offered and not offered & denied, offered & denied
    for r in stub.records:
        sysx = _system_text(r["body"])
        assert not [n for n in denied if f" {n}" in sysx or f"`{n}`" in sysx], "a denied tool is named"
    if policy == "read_only":
        # The working built-in tools are exactly these three (D8 invariant, task 13.33); next to them claude
        # 2.1.322 lists its non-functional `DeferredToolPlaceholder`. No search is offered, and neither the
        # search nor the placeholder does anything when called anyway (phase-9 finding).
        _assert_only_read_tools(agent, stub, offered)
        return
    assert "ToolSearch" in offered  # deferred loading is active under full_auto: the search is the real path
    for name in sorted(denied):
        assert _search(agent, stub, name) == NO_MATCH, name
    for word in KEYWORDS:
        found = _search(agent, stub, word, "DO_TOOLSEARCHKW")
        assert not any(f"'{n}'" in found for n in denied), (word, found)
    # Control: a deferred tool that is not denied is found by the same search.
    assert "WebFetch" in _search(agent, stub, "WebFetch")


def test_read_only_built_in_tool_list_keeps_the_mcp_allow_list(agent_lab):
    """Task 13.33: with `--tools Read,Grep,Glob` next to `--allowedTools`, `--restricted`, `dontAsk`,
    `--strict-mcp-config` and an MCP allow list, claude starts and answers; the built-in tools offered
    are exactly these three (no deferred-tool search); the allow-listed MCP tool works and the other
    tool of the same server is refused without reaching it. Local stubs only, no quota."""
    require_kind("claude", "read_only", login=False)
    stub = AnthropicStub()
    agent_lab.on_cleanup(stub.close)
    prov = ClaudeProvider.compatible(stub.url, auth_token=TOKEN, model="stub-model")
    stubdir = agent_lab.root / "mcp"
    stubdir.mkdir()
    config, calls = write_mcp_stub(stubdir, sys.executable)
    agent = agent_lab.start("claude", agent_lab.work("tools-mcp"), "read_only", provider=prov,
                            mcp_allow=["stub/ping"], mcp_config=str(config))
    assert agent.ask("hello", 120).startswith("ECHO")
    offered = {t.get("name") for r in stub.main_requests() for t in r["body"].get("tools") or []}
    print("offered:", sorted(offered))
    _assert_only_read_tools(agent, stub, {n for n in offered if not n.startswith("mcp__")})
    ping = agent.ask("DO_MCP_PING", 120)
    pong = agent.ask("DO_MCP_PONG", 120)
    print("ping:", ping[:120], "| pong:", pong[:120])
    assert ping.startswith("TOOLRESULT OK") and pong.startswith("TOOLRESULT ERR")
    called = calls.read_text().split() if calls.exists() else []
    assert "ping" in called and "pong" not in called
