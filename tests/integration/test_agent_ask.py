"""Task 8.3: ask and reply against real agents (POLYAGENT_INTEGRATION=1; consumes a little model quota)."""

import pytest

from polyagent.errors import AgentTimeoutError

from .agentlab import require_kind

pytestmark = pytest.mark.integration

PONG = "Reply with exactly one word: PONG"
FILES = {"alpha.txt": "the alpha value is 41\n", "beta.txt": "the beta value is 17\n"}
MULTI = ("Use your file tools to read alpha.txt and beta.txt in the current directory, then answer with a "
         "markdown bullet list with one bullet per file, each giving the file name in bold and its number.")


@pytest.mark.parametrize("kind", ["codex", "copilot", "claude"])
def test_pong_and_a_multi_step_markdown_answer(agent_lab, kind):
    """Scenarios: 正常回复 / 会话日志可用 / 多步工具调用的回合."""
    require_kind(kind)
    agent = agent_lab.start(kind, agent_lab.work(kind, files=FILES))
    source = "session_log"
    first = agent.ask_reply(PONG, 180)
    # codex opens its rollout only after the first message; the library binds it after submitting (D12,
    # task 13.15), so the first turn too comes from the session log.
    assert "PONG" in first.text and first.source == source
    second = agent.ask_reply(MULTI, 300)
    assert second.source == source
    assert "41" in second.text and "17" in second.text
    # the final message verbatim, markdown kept
    assert "**" in second.text and any(line.lstrip().startswith(("-", "*")) for line in second.text.splitlines())


# copilot: a timeout so short that the first prompt is not in the session log yet when the second ask
# starts; the submission record (task 7.10, D11) makes the second ask wait for it. claude: long enough for
# the delivery to be confirmed.
FIRST_TIMEOUT = {"claude": 8, "copilot": 1}


@pytest.mark.parametrize("kind", ["claude", "copilot"])
def test_timeout_then_ask_again_then_wait_reply(agent_lab, kind):
    """Scenarios: 超时 / 超时后再次提问 / 超时后取回该轮回复 / 工作中的 agent 被报告为空闲 (copilot) /
    上一次提交还没有出现在会话日志里 (copilot, task 7.10). Answers depend on the model (review T7): rerun
    once before treating a failure as a regression."""
    require_kind(kind)
    agent = agent_lab.start(kind, agent_lab.work(kind, files=FILES))
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("Run the shell command `sleep 15` and wait for it to finish. Then: " + MULTI, FIRST_TIMEOUT[kind])
    pending = ei.value.pending
    assert pending and pending["source"] == "session_log"
    # The first turn is still running: the second prompt goes in only after it, and its reply is its own.
    second = agent.ask("Reply with exactly one word: SECOND", 300)
    assert "SECOND" in second and "41" not in second
    first = agent_lab.pa.attach(agent.name).wait_reply(pending, 300)
    assert "41" in first.text and "17" in first.text and first.source == "session_log"
