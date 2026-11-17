"""Task 8.2: lifecycle against real agents (POLYAGENT_INTEGRATION=1; consumes a little model quota).

Agents run only in directories under the lab's mktemp root and are closed by the lab, also on failure.
"""

import json
import os
import secrets
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from .agentlab import require_kind

pytestmark = pytest.mark.integration

PONG = "Reply with exactly one word: PONG"


def agent_pids(lab, agent):
    info = lab.pa.herdr.pane_process_info(agent.pane_id)["process_info"]
    return [p["pid"] for p in info.get("foreground_processes") or [] if p.get("pid") != info.get("shell_pid")]


def environ(pid):
    try:
        return open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        return b""


def wait_for(cond, timeout=30.0, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def test_codex_secret_attach_close_and_no_daemon(agent_lab):
    """Scenarios: 注入凭据 / 服务重启后接管（另一个进程）/ 关闭 / 不留后台进程."""
    require_kind("codex")
    value = "pa-fake-cred-" + secrets.token_hex(8)
    agent = agent_lab.start("codex", agent_lab.work("codex"), env={"PA_FAKE_CREDENTIAL": value})
    pids = agent_pids(agent_lab, agent)
    assert pids, "no codex process in the pane"
    assert any(value.encode() in environ(p) for p in pids), "the variable did not reach the agent process"
    in_ps = value in subprocess.run(["ps", "-eww", "-o", "args"], capture_output=True, text=True).stdout
    assert not in_ps, "the value is in a process command line"  # never print the process list (T8)
    assert value not in agent.read("recent", 400) and value not in agent.read("visible")
    assert all(value not in " ".join(c) for c in agent_lab.calls())

    # Task 13.15: codex holds its rollout file only after the first message, so the first turn may come
    # from the screen (D12); the attached handle below must find the log.
    first = agent.ask_reply(PONG, 180)
    assert "PONG" in first.text and first.source in ("screen", "session_log")
    print(f"codex first turn source: {first.source}")

    script = textwrap.dedent(f"""
        import json
        from polyagent.manager import PolyAgent
        pa = PolyAgent(session={agent_lab.name!r}, herdr_path={str(agent_lab.shim)!r})
        r = pa.attach({agent.name!r}).ask_reply("Reply with exactly one word: PING", 180)
        print(json.dumps({{"text": r.text, "source": r.source}}))
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr[-2000:]
    reply = json.loads(out.stdout.strip().splitlines()[-1])
    assert "PING" in reply["text"] and reply["source"] == "session_log"

    codex_home = str(agent_lab.homes["CODEX_HOME"]).encode()
    agent.close()
    assert agent.name not in [a.name for a in agent_lab.pa.list()]

    def leftovers():
        return [cmd for pid, cmd in agent_lab.owned() if "codex" in cmd and codex_home in environ(pid)]
    assert wait_for(lambda: not leftovers(), 15), f"codex left processes behind: {leftovers()}"


def test_cleanup_closes_the_workspace_of_a_killed_agent(agent_lab):
    require_kind("codex")
    agent = agent_lab.start("codex", agent_lab.work("codex-kill"))
    for pid in agent_pids(agent_lab, agent):
        os.kill(pid, signal.SIGTERM)
    assert wait_for(lambda: agent.name not in [a.name for a in agent_lab.pa.list()], 30)
    assert agent.name in agent_lab.pa.cleanup()
    assert agent.workspace_id not in [w["workspace_id"] for w in agent_lab.pa.herdr.workspace_list()]


def test_claude_in_a_directory_with_spaces_and_after_attach(agent_lab):
    """Scenarios: 工作目录含特殊字符 / 接管后回复来源不变 (claude)."""
    require_kind("claude")
    cwd = agent_lab.work("dir with spaces @ü")
    agent = agent_lab.start("claude", cwd)
    first = agent.ask_reply(PONG, 180)
    assert "PONG" in first.text and first.source == "session_log"
    again = agent_lab.pa.attach(agent.name)
    second = again.ask_reply("Reply with exactly one word: PING", 180)
    assert "PING" in second.text and second.source == "session_log"


def test_copilot_trust_is_not_persisted(agent_lab):
    """Scenario: 信任不持久化 — the trust dialog is answered "this session only"."""
    require_kind("copilot")
    cwd = agent_lab.work("copilot-trust")
    agent_lab.start("copilot", cwd)  # started means the trust dialog was answered (D9)
    config = agent_lab.homes["COPILOT_HOME"] / "config.json"
    text = config.read_text() if config.exists() else ""
    assert str(cwd) not in text and os.path.realpath(cwd) not in text


@pytest.mark.parametrize("policy", [None, "read_only"])
def test_claude_runs_with_self_update_off(agent_lab, policy):
    """Task 13.39 / scenario 启动 claude: the claude process carries DISABLE_AUTOUPDATER=1 from the
    library's `workspace create --env` (both policies; the default start runs no preparation script).
    Only this one variable is read from the process environment. Started only: no prompt, no quota."""
    from .stubs import AnthropicStub
    from polyagent.spec import ClaudeProvider

    require_kind("claude", policy, login=False)
    kw = {"provider": None}
    if policy == "read_only":  # needs an explicit provider: the local stub (never asked here)
        stub = AnthropicStub()
        agent_lab.on_cleanup(stub.close)
        kw["provider"] = ClaudeProvider.compatible(stub.url, auth_token="stub-token-not-a-secret-0004", model="stub-model")
    agent = agent_lab.start("claude", agent_lab.work(f"upd-{policy}"), policy, **kw)
    created = [c for c in agent_lab.calls() if c[2:4] == ["workspace", "create"]]
    assert created and all("DISABLE_AUTOUPDATER=1" in c[c.index("--env") + 1:] for c in created[-1:])
    values = set()
    for pid in agent_pids(agent_lab, agent):
        for item in environ(pid).split(b"\0"):
            if item.startswith(b"DISABLE_AUTOUPDATER="):
                values.add(item.split(b"=", 1)[1])
    assert values == {b"1"}, values
    if policy is None:  # no preparation script for the default start
        assert not [c for c in agent_lab.calls() if c[2:4] == ["pane", "run"]]
    agent.close()



def test_copilot_cold_starts_take_their_first_prompt(lab_factory):
    """Task 13.20 (after the rule): cold starts of copilot, each in a fresh lab (fresh isolated
    configuration and working directory); the first prompt of each is delivered. A trust dialog drawn
    after start returned is looked for during the first seconds and its screen kept for the report.
    ``POLYAGENT_COLD_STARTS`` sets the count (default 3); each lab is torn down before the next one
    starts (review T4)."""
    require_kind("copilot", "read_only")
    runs = int(os.environ.get("POLYAGENT_COLD_STARTS", "3"))
    late = []
    for i in range(runs):
        lab = lab_factory()
        agent = lab.start("copilot", lab.work(f"cold{i}"), "read_only")
        for _ in range(12):
            screen = agent.read("visible")
            if "Confirm folder trust" in screen:
                late.append((i, screen))
                break
            time.sleep(0.25)
        assert "PONG" in agent.ask(PONG, 180), i
        agent.close()
        assert lab_factory.close(lab) == [], i
    for i, screen in late:
        print(f"late trust dialog after start {i}:\n{screen[-1500:]}")
    assert late == []


def test_copilot_interrupt_stops_a_long_command(agent_lab):
    """Task 13.3: interrupt() during copilot's synchronous `sleep 60` ends the turn well before the command
    would have (ctrl+c; the log shows the turn aborted) and the next prompt gets its reply; interrupt() on
    the idle agent afterwards sends nothing."""
    from polyagent.errors import AgentTimeoutError

    require_kind("copilot")
    agent = agent_lab.start("copilot", agent_lab.work("interrupt"))
    assert "PONG" in agent.ask(PONG, 180)
    t0 = time.monotonic()
    with pytest.raises(AgentTimeoutError):
        agent.ask("Use the bash tool in sync mode (not async) to run the command `sleep 60`, wait until it "
                  "finishes, then reply with exactly the word SLEPT.", 12)
    agent.interrupt()
    assert time.monotonic() - t0 < 45
    assert "AFTER" in agent.ask("Reply with exactly the word AFTER", 120)
    agent.interrupt()  # idle: nothing is sent, the agent stays
    assert "AGAIN" in agent.ask("Reply with exactly the word AGAIN", 120)
    agent.close()
