"""Behaviour that needs real child processes (review T5). Every "herdr" here is a throwaway script."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import threading
import time

import pytest

from conftest import script
from polyagent import herdr as H
from polyagent import locks
from polyagent import manager as M
from polyagent.errors import AskCancelledError, HerdrNotFoundError, HerdrUnavailableError

S = "polyagent-unit"


def nfds() -> int:
    gc.collect()
    return len(os.listdir("/proc/self/fd"))


def no_children_left() -> bool:
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def test_timeout_kills_and_reaps_without_fd_leak(tmp_path):
    c = H.HerdrClient(S, script(tmp_path, "exec sleep 30\n"))
    with pytest.raises(HerdrUnavailableError):
        c.call("workspace", "list", limit=0.1)
    before = nfds()
    t0 = time.monotonic()
    for _ in range(5):
        with pytest.raises(HerdrUnavailableError) as ei:
            c.call("workspace", "list", limit=0.1)
        assert ei.value.code == H.UNRESPONSIVE
    assert time.monotonic() - t0 < 4
    assert nfds() <= before
    assert no_children_left()


def test_cancel_kills_and_reaps_real_child(tmp_path):
    c = H.HerdrClient(S, script(tmp_path, "exec sleep 30\n"))
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    t0 = time.monotonic()
    with pytest.raises(AskCancelledError):
        c.agent_wait("rev", 600_000, cancel=ev)
    assert time.monotonic() - t0 < 3
    assert no_children_left()


def test_overrun_bound_when_grandchild_holds_pipes(tmp_path):
    """C6: a wrapper whose child keeps the pipes open must not add a long extra wait."""
    c = H.HerdrClient(S, script(tmp_path, "sleep 3\nexit 0\n"))
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError):
        c.call("workspace", "list", limit=0.3)
    assert time.monotonic() - t0 < 0.3 + 2 * H.KILL_GRACE + 0.5


def test_non_utf8_output_is_replaced(tmp_path):
    """C5."""
    c = H.HerdrClient(S, script(tmp_path, "printf 'ok \\377\\376 tail'\n"))
    assert c.pane_read("w1:p1") == "ok \ufffd\ufffd tail"


def test_vanished_binary_is_herdr_not_found(tmp_path):
    """C3."""
    b = script(tmp_path, "exit 0\n")
    c = H.HerdrClient(S, b)
    os.unlink(b)
    with pytest.raises(HerdrNotFoundError):
        c.workspace_list()


def test_version_output_from_real_script(tmp_path):
    assert H.check_version(script(tmp_path, "echo 'herdr 0.9.5'\n")) == (0, 9, 5)


# ---- locks with real processes ----------------------------------------------


def test_failed_lock_attempts_do_not_leak_fds():
    held = locks.acquire_for_start(S, "rev")
    before = nfds()
    for _ in range(200):
        assert locks.try_lock(S, "rev") is None
    assert nfds() == before
    held.release()


def test_lock_fd_not_inherited_by_children():
    held = locks.acquire_for_start(S, "rev")
    child = subprocess.Popen(["sleep", "5"], close_fds=False)
    try:
        held.release()
        again = locks.try_lock(S, "rev")
        assert again is not None
        again.release()
    finally:
        child.kill()
        child.wait(5)


def test_lock_released_when_holder_is_killed():
    home = locks.user_home()
    code = (
        "import sys, time, pathlib\nfrom polyagent import locks\n"
        "locks.user_home = lambda: pathlib.Path(sys.argv[1])\n"
        f"locks.acquire_for_start({S!r}, 'rev')\nprint('L', flush=True)\ntime.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(home)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "L"
        assert locks.try_lock(S, "rev") is None
        child.kill()
        child.wait(5)
        lk = locks.try_lock(S, "rev")
        assert lk is not None
        lk.release()
    finally:
        child.kill()
        child.wait(5)
        child.stdout.close()


# ---- server start with a scripted herdr --------------------------------------

STATEFUL = r"""
D="$(dirname "$0")"
echo "$*" >> "$D/calls.log"
if [ "$1" = "--version" ]; then echo "herdr 0.9.5"; exit 0; fi
shift 2
case "$1" in
  server) SERVER_BODY ;;
  session) echo '{"sessions":[]}' ;;
  workspace)
    if [ -e "$D/running" ]; then
      echo '{"id":"x","result":{"type":"workspace_list","workspaces":[]}}'
    else
      echo '{"id":"x","error":{"code":"server_not_running","message":"no server"}}' >&2; exit 1
    fi ;;
esac
"""


def calls(tmp_path):
    return (tmp_path / "bin" / "calls.log").read_text().splitlines()


def test_two_threads_spawn_one_server_and_it_is_reaped(tmp_path):
    b = script(tmp_path, STATEFUL.replace("SERVER_BODY", 'sleep 0.4; : > "$D/running"; exec sleep 1'))
    errs = []

    def build():
        try:
            M.PolyAgent(session=S, herdr_path=b, server_start_timeout=10)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=build) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert errs == []
    assert [c for c in calls(tmp_path) if c.endswith(" server")] == [f"--session {S} server"]
    deadline = time.monotonic() + 5
    while not no_children_left() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert no_children_left()


def test_server_exiting_at_once_fails_fast(tmp_path):
    """C7 with a real process."""
    b = script(tmp_path, STATEFUL.replace("SERVER_BODY", "echo 'config error' >&2; exit 3"))
    t0 = time.monotonic()
    with pytest.raises(HerdrUnavailableError) as ei:
        M.PolyAgent(session=S, herdr_path=b, server_start_timeout=10)
    assert time.monotonic() - t0 < 3
    assert ei.value.code == H.SERVER_START_FAILED and "status 3" in str(ei.value)


def test_relative_herdr_path_works_for_server_spawn(tmp_path, monkeypatch):
    """C4: the server is spawned with cwd=home, so the binary path must be absolute."""
    script(tmp_path, STATEFUL.replace("SERVER_BODY", ': > "$D/running"; exec sleep 1'))
    monkeypatch.chdir(tmp_path)
    M.PolyAgent(session=S, herdr_path="bin/herdr", server_start_timeout=5)
    assert f"--session {S} server" in calls(tmp_path)
