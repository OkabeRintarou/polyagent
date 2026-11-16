"""Task 8.7: claude providers against a local Anthropic-compatible stub (POLYAGENT_INTEGRATION=1).

The stub (standard library, 127.0.0.1) plays the model, so no quota is used. Credentials and header
values here are made up for the test.
"""

import subprocess

import pytest

from polyagent.spec import ClaudeProvider

from .agentlab import require_kind
from .stubs import AnthropicStub

pytestmark = pytest.mark.integration

TOKEN = "stub-token-not-a-secret-0001"


def stub(lab):
    s = AnthropicStub()
    lab.on_cleanup(s.close)
    return s


def claude_procs(lab, agent):
    info = lab.pa.herdr.pane_process_info(agent.pane_id)["process_info"]
    return [p["pid"] for p in info.get("foreground_processes") or [] if p.get("pid") != info.get("shell_pid")]


def proc_env(pid):
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read()
    except OSError:
        return {}
    return dict(item.decode(errors="replace").split("=", 1) for item in raw.split(b"\0") if b"=" in item)


def envs(lab, agent):
    """The claude processes' environment; there must be one that can be read (review T3)."""
    merged = {}
    for pid in claude_procs(lab, agent):
        merged.update(proc_env(pid))
    assert merged.get("PATH"), f"no readable environment for agent {agent.name}"
    return merged


def keys_sent(lab, name):
    return [c[5:] for c in lab.calls() if c[2:4] == ["agent", "send-keys"] and c[4] == name]


def test_compatible_endpoint_gets_the_requests_and_the_model(agent_lab):
    """Scenario: 兼容端点."""
    require_kind("claude", login=False)
    s = stub(agent_lab)
    agent = agent_lab.start("claude", agent_lab.work("compat"),
                            provider=ClaudeProvider.compatible(s.url, auth_token=TOKEN, model="stub-model-x"))
    assert "ECHO" in agent.ask("hello stub", 120)
    assert s.main_requests() and all(r["model"] == "stub-model-x" for r in s.main_requests())


def test_bedrock_and_compatible_instances_keep_their_own_variables(agent_lab):
    """Scenario: 多实例不同 provider."""
    require_kind("claude", login=False)
    s = stub(agent_lab)
    compat = agent_lab.start("claude", agent_lab.work("compat"),
                             provider=ClaudeProvider.compatible(s.url, auth_token=TOKEN, model="stub-model-x"))
    bedrock = agent_lab.start("claude", agent_lab.work("bedrock"), provider=ClaudeProvider.bedrock("us-east-1"))
    ce, be = envs(agent_lab, compat), envs(agent_lab, bedrock)
    assert ce.get("ANTHROPIC_BASE_URL") == s.url and "CLAUDE_CODE_USE_BEDROCK" not in ce and "AWS_REGION" not in ce
    assert be.get("CLAUDE_CODE_USE_BEDROCK") == "1" and be.get("AWS_REGION") == "us-east-1"
    assert "ANTHROPIC_BASE_URL" not in be and "ANTHROPIC_AUTH_TOKEN" not in be


def test_inherited_backend_and_headers_are_cleared(lab_factory):
    """Scenarios: 继承环境不干扰 / 继承的自定义请求头."""
    require_kind("claude", login=False)
    lab = lab_factory(server_env={"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_CUSTOM_HEADERS": "X-Pa-Inherited: leaked"})
    s = stub(lab)
    agent = lab.start("claude", lab.work("compat"),
                      provider=ClaudeProvider.compatible(s.url, auth_token=TOKEN, model="stub-model-x"))
    e = envs(lab, agent)
    assert "CLAUDE_CODE_USE_BEDROCK" not in e and "ANTHROPIC_CUSTOM_HEADERS" not in e
    agent.ask("hello stub", 120)
    assert s.records and all("x-pa-inherited" not in r["header_names"] for r in s.records)


def test_credential_dialog_follows_the_variable(agent_lab):
    """D9: the API-key confirmation is answered for an API key; a token raises no such dialog."""
    require_kind("claude", login=False)
    s = stub(agent_lab)
    keyed = agent_lab.start("claude", agent_lab.work("keyed"),
                            provider=ClaudeProvider.compatible(s.url, api_key=TOKEN, model="stub-model-x"))
    tokened = agent_lab.start("claude", agent_lab.work("tokened"),
                              provider=ClaudeProvider.compatible(s.url, auth_token=TOKEN, model="stub-model-x"))
    assert ["up", "enter"] in keys_sent(agent_lab, keyed.name)
    assert ["up", "enter"] not in keys_sent(agent_lab, tokened.name)
    assert "ECHO" in keyed.ask("hello keyed", 120)


def test_read_only_with_an_explicit_provider(agent_lab):
    """Scenario: 显式提供凭据."""
    require_kind("claude", "read_only", login=False)
    s = stub(agent_lab)
    agent = agent_lab.start("claude", agent_lab.work("ro"), "read_only",
                            provider=ClaudeProvider.compatible(s.url, auth_token=TOKEN, model="stub-model-x"))
    assert "ECHO" in agent.ask("hello read only", 120)


def test_custom_header_only_on_its_instance(agent_lab):
    """Scenario: 自定义请求头 — only that instance's requests carry it; its value is in no argv."""
    require_kind("claude", login=False)
    with_header, without = stub(agent_lab), stub(agent_lab)
    value = "pa-header-value-7c1e"
    a = agent_lab.start("claude", agent_lab.work("hdr"), provider=ClaudeProvider.compatible(
        with_header.url, auth_token=TOKEN, model="stub-model-x", custom_headers={"X-Pa-Test": value}))
    b = agent_lab.start("claude", agent_lab.work("nohdr"), provider=ClaudeProvider.compatible(
        without.url, auth_token=TOKEN, model="stub-model-x"))
    a.ask("hello a", 120)
    b.ask("hello b", 120)
    assert with_header.main_requests() and all(r["test_headers"].get("x-pa-test") == value
                                               for r in with_header.main_requests())
    assert without.records and all("x-pa-test" not in r["header_names"] for r in without.records)
    in_ps = value in subprocess.run(["ps", "-eww", "-o", "args"], capture_output=True, text=True).stdout
    assert not in_ps, "the header value is in a process command line"  # never print the process list (T8)
