"""Task 2.7: runtime integration against a real herdr (POLYAGENT_INTEGRATION=1)."""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from polyagent.manager import Marker, PolyAgent

pytestmark = pytest.mark.integration

DISC = "0123456789abcdef0123456789abcdef"


def test_auto_start_then_workspace_list(temp_session):
    assert temp_session.server_pids() == []
    # No herdr_path: the binary is resolved through PATH, where the guarding shim comes first.
    pa = PolyAgent(session=temp_session.name, server_start_timeout=20)
    assert pa.herdr.binary == temp_session.shim
    assert pa.herdr.workspace_list() == []
    assert len(temp_session.server_pids()) == 1
    PolyAgent(session=temp_session.name, herdr_path=temp_session.shim)
    assert len(temp_session.server_pids()) == 1
    spawns = [c for c in temp_session.calls() if c[2:] == ["server"]]
    assert len(spawns) == 1


def test_two_processes_initialise_one_server(temp_session):
    start_at = time.time() + 1.5
    script = textwrap.dedent(
        f"""
        import time
        from polyagent.manager import PolyAgent
        time.sleep(max(0.0, {start_at!r} - time.time()))
        pa = PolyAgent(session={temp_session.name!r}, herdr_path={temp_session.shim!r}, server_start_timeout=20)
        pa.herdr.workspace_list()
        print("OK", flush=True)
        """
    )
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert all(o[0].strip() == "OK" for o in outs)
    assert len(temp_session.server_pids()) == 1
    spawns = [c for c in temp_session.calls() if c[2:] == ["server"]]
    assert len(spawns) == 1


def test_caller_with_xdg_config_home_uses_the_same_server(temp_session, tmp_path):
    """Scenario: 调用方环境不同 — real herdr would look for the session under $XDG_CONFIG_HOME."""
    script = textwrap.dedent(
        f"""
        from polyagent.manager import PolyAgent
        pa = PolyAgent(session={temp_session.name!r}, herdr_path={temp_session.shim!r}, server_start_timeout=20)
        print("OK", len(pa.herdr.workspace_list()), flush=True)
        """
    )
    base = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
    other = dict(base, XDG_CONFIG_HOME=str(tmp_path / "xdg-config"))
    for env in (base, other):
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0 and r.stdout.strip() == "OK 0", (r.stdout, r.stderr)
    assert len(temp_session.server_pids()) == 1
    assert len([c for c in temp_session.calls() if c[2:] == ["server"]]) == 1
    assert not (tmp_path / "xdg-config" / "herdr").exists()


def test_marker_and_cleanup_on_real_workspaces(temp_session, tmp_path):
    pa = PolyAgent(session=temp_session.name, herdr_path=temp_session.shim)
    now = int(time.time())
    orphan = Marker("orphan-a", "codex", "read_only", now)
    in_disc = Marker("critic-01234567", "claude", "read_only", now, session_id="0b6f2c1e-9a7d-4c43-8f5e-2d1b3a4c5d6e",
                     discussion_id=DISC)
    w_orphan = pa.herdr.workspace_create(str(tmp_path), orphan.encode())["workspace"]["workspace_id"]
    w_disc = pa.herdr.workspace_create(str(tmp_path), in_disc.encode())["workspace"]["workspace_id"]
    w_manual = pa.herdr.workspace_create(str(tmp_path), "manual")["workspace"]["workspace_id"]

    owned = {o.workspace_id: o for o in pa.owned_workspaces()}
    assert set(owned) == {w_orphan, w_disc}
    assert owned[w_orphan].marker == orphan and owned[w_disc].marker == in_disc
    assert not owned[w_orphan].live and not owned[w_disc].live

    assert pa.cleanup(discussion_id=DISC) == ["critic-01234567"]
    assert sorted(pa.cleanup()) == ["orphan-a"]
    remaining = [w["workspace_id"] for w in pa.herdr.workspace_list()]
    assert remaining == [w_manual]


def test_all_herdr_calls_carry_the_temp_session_and_server_is_reaped(temp_session):
    pa = PolyAgent(session=temp_session.name, herdr_path=temp_session.shim)
    pa.cleanup()
    pa.owned_workspaces()
    pa.stop_server()
    calls = temp_session.calls()
    assert calls, "no herdr calls recorded"
    assert all(c == ["--version"] or c[:2] == ["--session", temp_session.name] for c in calls)
    assert temp_session.server_pids() == []
    # The library's reaper thread must have collected the server: no child (not even a zombie)
    # is left. WNOWAIT observes without reaping, so this check cannot hide a missing reaper.
    deadline = time.monotonic() + 5
    gone = False
    while not gone and time.monotonic() < deadline:
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            time.sleep(0.1)
        except ChildProcessError:
            gone = True
    assert gone, "a child process of the test (e.g. an unreaped herdr server) is still around"
