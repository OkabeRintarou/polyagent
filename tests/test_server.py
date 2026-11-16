"""Task 2.5: server probe, locked start, minimal environment, stop re-check, restart rules (design D3)."""

import subprocess
import threading
import time

import pytest

from conftest import Resp, err
from polyagent import herdr as H
from polyagent import locks
from polyagent import manager as M
from polyagent.errors import HerdrError, HerdrUnavailableError, InvalidConfigError
from polyagent.manager import PolyAgent


def server_spawns(fake):
    return [(a, kw) for a, kw in fake.calls if a[3:] == ["server"]]


def test_server_already_running_is_reused(fake):
    """Scenario: server 已运行."""
    PolyAgent()
    assert server_spawns(fake) == []


def test_server_not_running_is_started_detached(fake, lock_home):
    """Scenario: server 未运行."""
    fake.running = False
    PolyAgent(session="svc-a")
    spawns = server_spawns(fake)
    assert len(spawns) == 1
    argv, kw = spawns[0]
    assert argv[1:] == ["--session", "svc-a", "server"]
    assert kw["start_new_session"] is True
    assert kw["stdin"] is M.subprocess.DEVNULL and kw["stdout"] is M.subprocess.DEVNULL
    assert kw["stderr"] is M.subprocess.DEVNULL
    assert kw["cwd"] == str(lock_home)
    assert fake.running
    assert (lock_home / ".local/state/polyagent/svc-a/server.lock").exists()


def test_server_start_timeout(fake):
    """Scenario: server 启动超时."""
    fake.running = False
    fake.server_starts = False
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError) as ei:
        PolyAgent(session="svc-a", server_start_timeout=0.5)
    assert time.monotonic() - t0 < 3
    assert ei.value.code == H.SERVER_START_TIMEOUT
    assert "svc-a" in str(ei.value) and "herdr-server.log" in str(ei.value)


def test_recheck_under_lock_avoids_second_server(fake):
    """Concurrent start: another process brought the server up while we waited for server.lock."""
    fake.running = False
    probes = []

    def handler(argv):
        if argv[3:5] == ["workspace", "list"]:
            probes.append(1)
            if len(probes) == 2:  # first probe under the lock
                fake.running = True
        return None

    fake.handler = handler
    PolyAgent()
    assert server_spawns(fake) == []


def test_server_lock_is_held_while_starting(fake):
    fake.running = False
    seen = []

    def on_server():
        probe = locks.server_lock("polyagent")
        seen.append(probe.try_acquire())
        probe.release()

    fake.on_server = on_server
    PolyAgent()
    assert seen == [False]


def test_waits_for_foreign_server_lock_then_fails(fake, monkeypatch):
    monkeypatch.setattr(M, "SERVER_LOCK_SLACK", 0.1)
    fake.running = False
    held = locks.acquire_server_lock("polyagent", 1)
    try:
        with pytest.raises(HerdrUnavailableError) as ei:
            PolyAgent(server_start_timeout=0.3)
        assert ei.value.code == H.SERVER_LOCK_TIMEOUT
        assert server_spawns(fake) == []
    finally:
        held.release()


def test_lock_wait_timeout_rechecks_server(fake, monkeypatch):
    """C8: the other process finishes starting the server just as our lock wait expires."""
    monkeypatch.setattr(M, "SERVER_LOCK_SLACK", 0.1)
    fake.running = False
    held = locks.acquire_server_lock("polyagent", 1)
    timer = threading.Timer(0.25, lambda: setattr(fake, "running", True))
    timer.start()
    try:
        PolyAgent(server_start_timeout=0.3)  # lock never released, but the server is up
    finally:
        timer.cancel()
        held.release()
    assert server_spawns(fake) == []


def test_server_exiting_at_once_fails_fast_with_status(fake, monkeypatch):
    """C7: no need to wait the full start timeout when the server process is already gone."""
    monkeypatch.setattr(M, "SERVER_EXIT_GRACE", 0.4)
    fake.running = False
    fake.server_starts = False
    fake.server_exit = 1
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError) as ei:
        PolyAgent(server_start_timeout=10)
    assert 0.4 <= time.monotonic() - t0 < 2
    assert ei.value.code == H.SERVER_START_FAILED
    assert "status 1" in str(ei.value) and "/fake/sessions/polyagent/herdr-server.log" in str(ei.value)


def test_server_exiting_because_another_won_the_race_is_success(fake):
    fake.running = False
    fake.server_exit = 1  # "already running"
    PolyAgent()


def test_hanging_probe_during_start_reports_start_timeout_with_session(fake):
    """C7: an unresponsive probe while starting becomes a start timeout naming the session."""
    fake.running = False
    fake.server_starts = False
    spawned = []
    fake.on_server = lambda: spawned.append(1)
    fake.handler = lambda argv: Resp(hang=True) if spawned and argv[3:5] == ["workspace", "list"] else None
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError) as ei:
        PolyAgent(session="svc-b", server_start_timeout=0.6)
    assert time.monotonic() - t0 < 3
    assert ei.value.code == H.SERVER_START_TIMEOUT and "svc-b" in str(ei.value)
    probe_limits = [p.timeouts[0] for a, p in zip(fake.argvs, fake.procs) if a[3:5] == ["workspace", "list"]][2:]  # after the two pre-spawn probes
    assert probe_limits and max(probe_limits) <= 0.6


def test_server_popen_oserror_is_unavailable(fake, monkeypatch):
    """C3: Popen failures while starting the server are library errors."""
    fake.running = False
    orig = fake.popen

    def popen(argv, **kw):
        if list(argv)[3:] == ["server"]:
            raise OSError(12, "Cannot allocate memory")
        return orig(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(HerdrUnavailableError) as ei:
        PolyAgent()
    assert ei.value.code == H.LAUNCH_FAILED


def test_caller_env_not_leaked(fake, monkeypatch):
    """Scenario: 调用方环境不泄漏."""
    fake.running = False
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "true")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("https_proxy", "http://proxy:3128")
    PolyAgent()
    env = server_spawns(fake)[0][1]["env"]
    assert "DATABASE_URL" not in env and "COPILOT_ALLOW_ALL" not in env and "HERDR_PANE_ID" not in env
    assert env["LC_ALL"] == "C.UTF-8" and env["https_proxy"] == "http://proxy:3128"
    assert "PATH" in env


def test_explicit_passthrough(fake, monkeypatch):
    """Scenario: 显式透传."""
    fake.running = False
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    PolyAgent(server_env_passthrough=["GH_TOKEN"])
    assert server_spawns(fake)[0][1]["env"]["GH_TOKEN"] == "ghp_x"


@pytest.mark.parametrize(
    "name",
    ["COPILOT_ALLOW_ALL", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CURSOR_API_KEY",
     "BASH_ENV", "ENV", "NODE_OPTIONS", "LD_PRELOAD", "HERDR_SOCKET_PATH", "1BAD", "A-B"],
)
def test_passthrough_of_permission_variables_rejected(fake, name):
    """Scenario: 透传权限开关变量."""
    fake.running = False
    with pytest.raises(InvalidConfigError):
        PolyAgent(server_env_passthrough=[name])
    assert fake.calls == []


def test_passthrough_must_be_sequence(fake):
    with pytest.raises(InvalidConfigError):
        PolyAgent(server_env_passthrough="GH_TOKEN")


def test_stop_server(fake):
    """Scenario: 停止命名 session."""
    pa = PolyAgent()
    pa.stop_server()
    assert [a[3:] for a in fake.argvs if a[3:4] == ["session"]] == [
        ["session", "list", "--json"],
        ["session", "stop", "polyagent", "--json"],
    ]
    assert not fake.running


def test_stop_refuses_when_default_flag_missing(fake):
    pa = PolyAgent()
    fake.sessions_default = None
    with pytest.raises(HerdrError):
        pa.stop_server()
    assert not [a for a in fake.argvs if a[3:5] == ["session", "stop"]]


def test_stop_does_nothing_when_session_not_listed(fake):
    pa = PolyAgent()
    fake.session_listed = False
    pa.stop_server()
    assert not [a for a in fake.argvs if a[3:5] == ["session", "stop"]]


def test_stop_rechecks_default_flag(fake):
    """Scenario: 停止前再校验."""
    pa = PolyAgent()
    fake.sessions_default = True
    with pytest.raises(HerdrError) as ei:
        pa.stop_server()
    assert ei.value.code == "default_session_refused"
    assert not [a for a in fake.argvs if a[3:5] == ["session", "stop"]]
    assert fake.running


def test_handle_operations_do_not_restart_server(fake):
    """Placeholder for "server 被停止后提问": the client layer never starts a server.

    The scenario itself needs the Agent handle (task groups 6/7)."""
    pa = PolyAgent()
    fake.running = False
    n = len(fake.calls)
    with pytest.raises(HerdrUnavailableError):
        pa.herdr.agent_prompt("rev", "hi", 10_000)
    assert len(fake.calls) == n + 1 and server_spawns(fake) == []
    assert not fake.running


def test_entry_operations_restart_server_once(fake):
    """Entry ops restart the server once on server_not_running (old-handle part: groups 6/7)."""
    pa = PolyAgent()
    fake.running = False
    assert pa.cleanup() == []
    assert len(server_spawns(fake)) == 1 and fake.running


def test_entry_operation_does_not_restart_on_other_unavailable_codes(fake):
    pa = PolyAgent()
    fake.handler = lambda argv: err("protocol_mismatch", "x") if argv[3:5] == ["workspace", "list"] else None
    with pytest.raises(HerdrUnavailableError):
        pa.cleanup()
    assert server_spawns(fake) == []


def test_lost_race_against_a_server_that_answers_later(fake):
    """N2: our child exits ("already running") and the other server only answers after a while."""
    fake.running = False
    fake.server_starts = False
    fake.server_exit = 1
    probes = {"n": 0}
    spawned = []
    fake.on_server = lambda: spawned.append(1)

    def handler(argv):
        if spawned and argv[3:5] == ["workspace", "list"]:
            probes["n"] += 1
            if probes["n"] == 5:  # ~1 s after our child exited
                fake.running = True
        return None

    fake.handler = handler
    PolyAgent(server_start_timeout=10)
    assert probes["n"] >= 5


def test_log_hint_session_list_is_bounded(fake):
    """N3: a hanging `session list` while building the error hint must not add ~30 s."""
    fake.running = False
    fake.server_starts = False
    fake.handler = lambda argv: Resp(hang=True) if argv[3:5] == ["session", "list"] else None
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError) as ei:
        PolyAgent(server_start_timeout=0.5)
    assert time.monotonic() - t0 < 0.5 + M.HINT_LIMIT + 1
    assert "herdr-server.log" in str(ei.value)
    hint_calls = [p for a, p in zip(fake.argvs, fake.procs) if a[3:5] == ["session", "list"]]
    assert hint_calls and all(t <= M.HINT_LIMIT + 0.01 for t in hint_calls[0].timeouts)


def test_no_hint_call_when_the_cli_itself_hangs(fake):
    """N3: after an unresponsive probe the hint does not call herdr again."""
    fake.running = False
    fake.server_starts = False
    spawned = []
    fake.on_server = lambda: spawned.append(1)
    fake.handler = lambda argv: Resp(hang=True) if spawned and argv[3:5] == ["workspace", "list"] else None
    with pytest.raises(HerdrUnavailableError):
        PolyAgent(server_start_timeout=0.5)
    assert not [a for a in fake.argvs if a[3:5] == ["session", "list"]]
