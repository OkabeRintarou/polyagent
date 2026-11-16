"""Task 13.13: herdr contract checks on a named agent (POLYAGENT_INTEGRATION=1; no model quota).

claude runs against the local Anthropic-compatible stub (standard library, 127.0.0.1), so a real named
agent exists in the lab's temporary session without any request leaving the machine. The structures
seen here are recorded in ``tests/fixtures/herdr/README.md``; the in-memory fake herdr
(``tests/fakeclient.py``) returns the same fields.
"""

import json

import pytest

from polyagent import agent as A
from polyagent.spec import ClaudeProvider

from .agentlab import require_kind
from .stubs import AnthropicStub

pytestmark = pytest.mark.integration

TOKEN = "stub-token-not-a-secret-0002"


def _json(out):
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


def test_named_agent_contract(agent_lab):
    require_kind("claude", login=False)
    stub = AnthropicStub()
    agent_lab.on_cleanup(stub.close)
    agent = agent_lab.start("claude", agent_lab.work("contract"),
                            provider=ClaudeProvider.compatible(stub.url, auth_token=TOKEN, model="stub-model-x"))

    got = _json(agent_lab.herdr("agent", "get", agent.name))["result"]["agent"]
    for key in ("name", "workspace_id", "pane_id", "agent_status", "state_change_seq", "cwd"):
        assert key in got, f"agent get lacks {key}: {sorted(got)}"
    # Without herdr's own integration for the kind the session field is absent, not null; the library
    # reads it with .get() and falls back to the open rollout file (codex) or the session id it chose.
    assert got.get("agent_session") is None
    assert (got["name"], got["workspace_id"], got["pane_id"]) == (agent.name, agent.workspace_id, agent.pane_id)
    assert isinstance(got["state_change_seq"], int) and got["agent_status"] in ("idle", "done", "working", "blocked",
                                                                                  "unknown")
    listed = _json(agent_lab.herdr("agent", "list"))["result"]["agents"]
    mine = [a for a in listed if a.get("name") == agent.name]
    assert len(mine) == 1 and {"name", "workspace_id", "pane_id", "agent_status", "state_change_seq"} <= set(mine[0])

    info = _json(agent_lab.herdr("pane", "process-info", "--pane", agent.pane_id))["result"]["process_info"]
    procs = info["foreground_processes"]
    assert isinstance(info["shell_pid"], int) and procs
    assert all({"pid", "name", "argv"} <= set(p) for p in procs)
    assert any(p["pid"] != info["shell_pid"] for p in procs), "the agent is not the pane's foreground process"
    # Task 13.19: the library creates every pane with HISTFILE=/dev/null for its shell.
    shell_env = open(f"/proc/{info['shell_pid']}/environ", "rb").read().split(b"\0")
    assert b"HISTFILE=/dev/null" in shell_env

    # Every named key the library sends (agent.KEYS) is accepted by `agent send-keys` on a named agent;
    # ctrl+c last (claude only arms its exit on the first one). An unknown name is refused before any byte.
    bad = agent_lab.herdr("agent", "send-keys", agent.name, "bogus-key")
    assert bad.returncode == 1 and json.loads(bad.stderr)["error"]["code"] == "invalid_key"
    order = sorted(set(A.KEYS.values()) - {"ctrl+c"}) + ["ctrl+c"]
    for key in order:
        out = agent_lab.herdr("agent", "send-keys", agent.name, key)
        assert out.returncode == 0, f"{key}: {out.stderr[-300:]}"
    print("agent get keys:", sorted(got))
    print("agent list entry keys:", sorted(mine[0]))
    print("process-info keys:", sorted(info), "foreground keys:", sorted(procs[0]))


def test_workspace_create_env_reaches_the_pane_shell(agent_lab):
    """Task 13.19: `workspace create --env K=V` sets the variable for the pane's shell (herdr 0.9.5)."""
    import time

    w = agent_lab.work("envprobe")
    hist = agent_lab.root / "probe-history"
    created = _json(agent_lab.herdr("workspace", "create", "--cwd", str(w), "--label", "pa-env-probe", "--no-focus",
                                    "--env", "PA_ENV_PROBE=hello world", "--env", f"HISTFILE={hist}"))["result"]
    pane, wid = created["root_pane"]["pane_id"], created["workspace"]["workspace_id"]
    try:
        time.sleep(1.0)
        assert agent_lab.herdr("pane", "run", pane, 'echo "P=[$PA_ENV_PROBE] H=[${HISTFILE-unset}]"').returncode == 0
        deadline, text = time.monotonic() + 10, ""
        while time.monotonic() < deadline:
            text = agent_lab.herdr("pane", "read", pane, "--source", "recent", "--lines", "20").stdout
            if "P=[hello world]" in text:
                break
            time.sleep(0.2)
        assert f"P=[hello world] H=[{hist}]" in text
    finally:
        agent_lab.herdr("workspace", "close", wid)
