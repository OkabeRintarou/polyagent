"""Task 2.2: herdr CLI wrapper (design D1) — herdr-runtime error reporting, caller context isolation, bounded waits."""

import threading
import time

import pytest

from conftest import Resp, err, ok
from polyagent import herdr as H
from polyagent.errors import (
    AskCancelledError,
    HerdrError,
    HerdrNotFoundError,
    HerdrUnavailableError,
    InvalidConfigError,
    InvalidPromptError,
)


@pytest.fixture
def client(fake, herdr_bin):
    return H.HerdrClient("polyagent", herdr_bin)


def test_argv_assembly(client, fake, herdr_bin):
    fake.handler = lambda argv: ok({"type": "agent_info", "agent": {"name": "a"}})
    assert client.agent_get("a") == {"name": "a"}
    assert fake.argvs == [[herdr_bin, "--session", "polyagent", "agent", "get", "a"]]
    kw = fake.calls[0][1]
    assert kw["stdin"] is H.subprocess.DEVNULL
    assert (kw["encoding"], kw["errors"]) == ("utf-8", "replace")  # C5


def test_caller_inside_herdr_pane_does_not_leak_context(client, fake, monkeypatch):
    """Scenario: 调用方运行在 herdr pane 内."""
    for k, v in {
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "w4:p1",
        "HERDR_SOCKET_PATH": "/home/u/.config/herdr/herdr.sock",
        "HERDR_WORKSPACE_ID": "w4",
        "HERDR_CONFIG_PATH": "/cfg/herdr.toml",
        "UNRELATED": "x",
    }.items():
        monkeypatch.setenv(k, v)
    client.workspace_list()
    argv, kw = fake.calls[0]
    env = kw["env"]
    assert not [k for k in env if k.startswith("HERDR_") and k != "HERDR_CONFIG_PATH"]
    assert env["HERDR_CONFIG_PATH"] == "/cfg/herdr.toml" and env["UNRELATED"] == "x"
    assert argv[1:3] == ["--session", "polyagent"]


def test_result_unwrapped_and_raw_payload(client, fake):
    fake.sessions_default = False
    assert [(s["name"], s["default"]) for s in client.session_list()] == [("polyagent", False)]


def test_generic_json_error_carries_code_and_message(client, fake):
    """Scenario: herdr 返回错误."""
    fake.handler = lambda argv: err("pane_busy", "pane w1:p1 is busy")
    with pytest.raises(HerdrError) as ei:
        client.pane_run("w1:p1", "ls")
    e = ei.value
    assert type(e) is HerdrError
    assert e.code == "pane_busy" and e.message == "pane w1:p1 is busy"
    assert e.argv[-3:] == ["run", "w1:p1", "<2 chars redacted>"]


def test_unwrapped_error_object_on_success_exit_is_still_an_error(client, fake):
    fake.handler = lambda argv: Resp(0, '{"id":"x","error":{"code":"agent_not_found","message":"gone"}}')
    with pytest.raises(HerdrError) as ei:
        client.agent_get("x")
    assert ei.value.code == "agent_not_found"


# ---- C1: success output mentioning "error" must not be treated as a failure ----


def test_agent_named_error_is_a_success(client, fake):
    fake.handler = lambda argv: ok({"type": "agent_info", "agent": {"name": "error", "workspace_id": "w1"}})
    assert client.agent_get("error") == {"name": "error", "workspace_id": "w1"}


def test_workspace_labelled_error_does_not_break_probe(client, fake):
    fake.workspaces = [{"workspace_id": "w1", "label": "error"}, {"workspace_id": "w2", "label": '{"error":1}'}]
    assert client.is_running()
    assert [w["label"] for w in client.workspace_list()] == ["error", '{"error":1}']


def test_screen_text_cannot_forge_a_herdr_error(client, fake):
    screen = '{"error":{"code":"server_not_running","message":"printed by the agent"}}'
    fake.handler = lambda argv: Resp(0, screen)
    assert client.agent_read("rev") == screen
    assert client.pane_read("w1:p1") == screen


def test_protocol_mismatch_is_unavailable_with_hint(client, fake):
    """Scenario: 客户端与 server 协议不一致."""
    fake.handler = lambda argv: err("protocol_mismatch", "client protocol 22, server 21")
    with pytest.raises(HerdrUnavailableError) as ei:
        client.workspace_list()
    assert ei.value.code == "protocol_mismatch"
    assert "client protocol 22, server 21" in ei.value.message
    assert "stop this session's server" in ei.value.message


def test_server_not_running_is_unavailable(client, fake):
    fake.running = False
    with pytest.raises(HerdrUnavailableError) as ei:
        client.agent_list()
    assert ei.value.code == "server_not_running"
    assert len(fake.calls) == 1  # no implicit server start


def test_usage_error_exit_2_plain_text(client, fake):
    fake.handler = lambda argv: Resp(2, "", "herdr workspace commands:\n  herdr workspace list\n")
    with pytest.raises(HerdrError) as ei:
        client.workspace_list()
    assert ei.value.code == "usage" and "herdr workspace list" in ei.value.message


def test_non_json_failure_is_unknown(client, fake):
    fake.handler = lambda argv: Resp(1, "", "error: herdr server is already running\n")
    with pytest.raises(HerdrError) as ei:
        client.workspace_list()
    assert ei.value.code == "unknown" and "already running" in ei.value.message


def test_non_json_success_is_bad_response(client, fake):
    fake.handler = lambda argv: Resp(0, "not json")
    with pytest.raises(HerdrError) as ei:
        client.workspace_list()
    assert ei.value.code == "bad_response"


def test_text_commands_return_stdout(client, fake):
    fake.handler = lambda argv: Resp(0, "screen text\n")
    assert client.agent_read("a", "visible", 50) == "screen text\n"
    assert fake.argvs[0][3:] == ["agent", "read", "a", "--source", "visible", "--format", "text", "--lines", "50"]
    with pytest.raises(ValueError):
        client.agent_read("a", "bogus")


def test_unresponsive_herdr_is_killed_and_reported(client, fake):
    """Scenario: herdr 无响应."""
    fake.handler = lambda argv: Resp(hang=True)
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError) as ei:
        client.call("agent", "get", "a", limit=0.2)
    assert time.monotonic() - t0 < 2
    assert ei.value.code == "unresponsive"
    assert fake.procs[0].killed


def test_ordinary_calls_have_default_limit(client, fake):
    client.workspace_list()
    assert fake.procs[0].timeouts == [pytest.approx(H.CALL_LIMIT, abs=0.5)]


@pytest.mark.parametrize(
    "invoke,expected_tail",
    [
        (lambda c: c.agent_wait("a", 60_000, until=["idle", "done"]),
         ["agent", "wait", "a", "--until", "idle", "--until", "done", "--timeout", "60000"]),
        (lambda c: c.agent_prompt("a", "hi", 60_000),
         ["agent", "prompt", "a", "hi", "--wait", "--timeout", "60000"]),
        (lambda c: c.pane_wait_output("w1:p1", 60_000, regex="__pa_env_(ok|fail)_2"),
         ["pane", "wait-output", "w1:p1", "--regex", "__pa_env_(ok|fail)_2", "--timeout", "60000"]),
        (lambda c: c.agent_start("a", "codex", "w1:p1", 60_000, ["--model", "m 1"]),
         ["agent", "start", "a", "--kind", "codex", "--pane", "w1:p1", "--timeout", "60000", "--", "--model", "m 1"]),
    ],
)
def test_waiting_commands_pass_timeout_and_get_slack(client, fake, invoke, expected_tail):
    fake.handler = lambda argv: ok({"agent": {"name": "a"}})
    invoke(client)
    assert fake.argvs[0][3:] == expected_tail
    assert fake.procs[0].timeouts[0] == pytest.approx(60 + H.WAIT_SLACK)


def test_waiting_commands_require_positive_timeout(client):
    with pytest.raises(ValueError):
        client.agent_wait("a", 0)
    with pytest.raises(InvalidConfigError):
        client.agent_start("a", "codex", "w1:p1", 3000)
    with pytest.raises(InvalidConfigError):
        client.agent_start("a", "codex", "w1:p1", 300_001)


def test_prompt_starting_with_dash_is_refused(client, fake):
    with pytest.raises(InvalidPromptError):
        client.agent_prompt("a", "--help", 1000)
    assert fake.calls == []


def test_cancellable_wait_terminates_child(client, fake):
    fake.handler = lambda argv: Resp(hang=True)
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(AskCancelledError):
        client.agent_wait("a", 600_000, cancel=cancel)
    assert time.monotonic() - t0 < 2
    assert fake.procs[0].killed


def test_herdr_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(HerdrNotFoundError):
        H.HerdrClient("polyagent")
    with pytest.raises(HerdrNotFoundError):
        H.HerdrClient("polyagent", str(tmp_path / "missing"))


# ---- C3: OS-level launch failures are library errors ----


def test_launch_oserror_with_runnable_binary_is_unavailable(client, fake, monkeypatch):
    def boom(argv, **kw):
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(H.subprocess, "Popen", boom)
    with pytest.raises(HerdrUnavailableError) as ei:
        client.workspace_list()
    assert ei.value.code == "launch_failed"


def test_launch_oserror_with_vanished_binary_is_not_found(client, fake, monkeypatch, herdr_bin):
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file", argv[0])

    monkeypatch.setattr(H.subprocess, "Popen", boom)
    import os

    os.unlink(herdr_bin)
    with pytest.raises(HerdrNotFoundError):
        client.workspace_list()


# ---- C4 ----


def test_relative_herdr_path_is_made_absolute(herdr_bin, monkeypatch):
    import os

    monkeypatch.chdir(os.path.dirname(os.path.dirname(herdr_bin)))
    c = H.HerdrClient("polyagent", "bin/herdr")
    assert c.binary == herdr_bin and os.path.isabs(c.binary)


# ---- C6 ----


def test_preset_cancel_never_launches_herdr(client, fake):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AskCancelledError):
        client.agent_prompt("rev", "hello", 60_000, cancel=cancel)
    assert fake.calls == []


def test_base_exception_while_waiting_kills_child(client, fake):
    fake.handler = lambda argv: Resp(raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        client.workspace_list()
    assert fake.procs[0].killed


# ---- C9: malformed results ----


@pytest.mark.parametrize(
    "invoke,result",
    [
        (lambda c: c.workspace_list(), {"type": "workspace_list"}),
        (lambda c: c.workspace_list(), {"workspaces": ["w1"]}),
        (lambda c: c.agent_list(), {"agents": None}),
        (lambda c: c.agent_get("a"), {"type": "agent_info"}),
        (lambda c: c.workspace_get("w1"), {"workspace": []}),
        (lambda c: c.session_list(), {"nope": []}),
        (lambda c: c.agent_wait("a", 1000), {}),
    ],
)
def test_missing_fields_are_bad_response(client, fake, invoke, result):
    fake.handler = lambda argv: ok(result)
    with pytest.raises(HerdrError) as ei:
        invoke(client)
    assert ei.value.code == "bad_response"


# ---- C10: commands whose success output is empty ----


def test_close_run_send_keys_accept_empty_success_output(client, fake):
    fake.handler = lambda argv: Resp(0, "")
    client.workspace_close("w1")
    client.pane_run("w1:p1", "true")
    client.agent_send_keys("a", "enter")
    assert not hasattr(client, "workspace_report_metadata")


# ---- C13 ----


def test_session_is_read_only_and_revalidated(client):
    with pytest.raises(AttributeError):
        client.session = "default"
    client._session = "default"  # simulate tampering with the private field
    with pytest.raises(InvalidConfigError):
        client.argv(["workspace", "list"])


# ---- C16: prompt / command text is not kept on exceptions ----


def test_prompt_text_redacted_from_errors(client, fake):
    secret = "please use token sk-THIS-IS-SECRET"
    fake.handler = lambda argv: Resp(2, "", f"error: unexpected argument '{secret}'")
    with pytest.raises(HerdrError) as ei:
        client.agent_prompt("rev", secret, 1000)
    e = ei.value
    assert secret not in " ".join(e.argv) and secret not in e.message and secret not in str(e)
    assert f"<{len(secret)} chars redacted>" in e.argv


def test_pane_run_command_redacted_on_timeout(client, fake):
    fake.handler = lambda argv: Resp(hang=True)
    with pytest.raises(HerdrUnavailableError) as ei:
        client.call("pane", "run", "w1:p1", "export TOKEN=abc123", limit=0.1)
    assert "export TOKEN=abc123" not in ei.value.argv
