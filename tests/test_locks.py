"""Task 2.4: per-agent flock locks (design D5)."""

import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from conftest import ORIG_USER_HOME
from polyagent import herdr as H
from polyagent import locks
from polyagent.errors import (
    AgentBusyError,
    AgentNameConflictError,
    AskCancelledError,
    HerdrUnavailableError,
    InvalidConfigError,
)


class CountingCancel:
    """Never cancels; counts how many times the waiter polled (proves it really was waiting)."""

    def __init__(self):
        self.polls = 0

    def is_set(self):
        self.polls += 1
        return False


def test_lock_dir_fixed_and_private(lock_home, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/elsewhere")
    d = locks.lock_dir("svc-a")
    assert d == lock_home / ".local" / "state" / "polyagent" / "svc-a"
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_bad_names_rejected(lock_home):
    with pytest.raises(InvalidConfigError):
        locks.agent_lock("polyagent", "../x")
    with pytest.raises(InvalidConfigError):
        locks.lock_dir("default")


def test_threads_in_one_process_exclude_each_other(lock_home):
    held = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 1)
    order = []
    waiter = CountingCancel()

    def second():
        lk = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 10, cancel=waiter)
        order.append("second")
        lk.release()

    t = threading.Thread(target=second)
    t.start()
    deadline = time.monotonic() + 5
    while waiter.polls < 3 and time.monotonic() < deadline:  # the thread has polled and failed at least twice
        time.sleep(0.01)
    assert waiter.polls >= 3 and order == []
    order.append("first-release")
    held.release()
    t.join(5)
    assert order == ["first-release", "second"]


def test_two_handles_same_thread_are_exclusive(lock_home):
    a = locks.agent_lock("polyagent", "rev")
    b = locks.agent_lock("polyagent", "rev")
    assert a.try_acquire()
    assert not b.try_acquire()
    a.release()
    assert b.try_acquire()
    b.release()


def test_ask_deadline_raises_busy(lock_home):
    held = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 1)
    t0 = time.monotonic()
    with pytest.raises(AgentBusyError):
        locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 0.3)
    assert 0.25 < time.monotonic() - t0 < 2
    held.release()


def test_ask_wait_is_cancellable(lock_home):
    held = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 1)
    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()
    with pytest.raises(AskCancelledError):
        locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 10, cancel=ev)
    held.release()


def test_start_lock_is_single_nonblocking_attempt(lock_home):
    held = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 1)
    t0 = time.monotonic()
    with pytest.raises(AgentNameConflictError):
        locks.acquire_for_start("polyagent", "rev")
    assert time.monotonic() - t0 < 0.2
    held.release()
    locks.acquire_for_start("polyagent", "rev").release()


def test_lock_files_are_not_deleted(lock_home):
    lk = locks.acquire_for_start("polyagent", "rev")
    path = lk.path
    lk.release()
    assert path.exists()


_CHILD = r"""
import sys, time, pathlib
from polyagent import locks
locks.user_home = lambda: pathlib.Path(sys.argv[1])
lk = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 10)
print("LOCKED", flush=True)
sys.stdin.readline()
lk.release()
print("RELEASED", flush=True)
"""


def test_two_processes_exclude_each_other(lock_home):
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(lock_home)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(AgentNameConflictError):
            locks.acquire_for_start("polyagent", "rev")
        with pytest.raises(AgentBusyError):
            locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 0.2)
        child.stdin.write("\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "RELEASED"
        locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 2).release()
    finally:
        child.kill()
        child.wait(5)


def test_two_child_processes_serialise(lock_home):
    script = r"""
import sys, time, pathlib
from polyagent import locks
locks.user_home = lambda: pathlib.Path(sys.argv[1])
lk = locks.acquire_for_ask("polyagent", "rev", time.monotonic() + 10)
with open(sys.argv[2], "a") as f:
    f.write(f"start {sys.argv[3]}\n"); f.flush(); time.sleep(0.3); f.write(f"end {sys.argv[3]}\n")
lk.release()
"""
    log = lock_home / "order.log"
    procs = [subprocess.Popen([sys.executable, "-c", script, str(lock_home), str(log), str(i)]) for i in range(2)]
    for p in procs:
        assert p.wait(15) == 0
    # Each "start i" is immediately followed by its own "end i".
    seq = log.read_text().splitlines()
    assert len(seq) == 4
    assert seq[0].split()[1] == seq[1].split()[1] and seq[0].startswith("start") and seq[1].startswith("end")
    assert seq[2].split()[1] == seq[3].split()[1]


# ---- A1: agent locks can never collide with the server lock ----


def test_agent_named_server_does_not_share_the_server_lock(lock_home):
    agent = locks.acquire_for_start("polyagent", "server")
    try:
        assert agent.path != locks.server_lock("polyagent").path
        assert agent.path.parent.name == "agents"
        locks.acquire_server_lock("polyagent", 0.3).release()
    finally:
        agent.release()


def test_server_lock_timeout_code(lock_home):
    held = locks.acquire_server_lock("polyagent", 1)
    try:
        with pytest.raises(HerdrUnavailableError) as ei:
            locks.acquire_server_lock("polyagent", 0.1)
        assert ei.value.code == H.SERVER_LOCK_TIMEOUT
    finally:
        held.release()


def test_agent_lock_dir_is_private(lock_home):
    lk = locks.acquire_for_start("polyagent", "rev")
    lk.release()
    assert stat.S_IMODE(os.stat(lk.path.parent).st_mode) == 0o700


# ---- C14 ----


def test_unacquired_lock_cannot_be_used_as_context_manager(lock_home):
    with pytest.raises(RuntimeError):
        with locks.agent_lock("polyagent", "rev"):
            pass
    with locks.acquire_for_start("polyagent", "rev") as lk:
        assert lk.held
    assert not lk.held


# ---- C17 / T3: home directory source ----


def test_user_home_comes_from_passwd_not_env(monkeypatch, tmp_path):
    import pwd

    monkeypatch.setenv("HOME", str(tmp_path))
    assert ORIG_USER_HOME() == locks.Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert ORIG_USER_HOME() != tmp_path


def test_uid_without_passwd_entry_is_config_error(monkeypatch):
    import pwd

    def missing(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", missing)
    with pytest.raises(InvalidConfigError):
        ORIG_USER_HOME()
