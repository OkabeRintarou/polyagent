"""Task 7.6: one prompt at a time per agent (D5), across threads, handles and processes."""

import json
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from polyagent import agent as A
from polyagent.errors import AgentBusyError

import test_ask as T


@pytest.fixture(autouse=True)
def fast(monkeypatch, lock_home):
    monkeypatch.setattr(A, "POLL_INTERVAL", 0.005)


def slow_claude(path, events, delay=0.2):
    """on_prompt: the turn takes ``delay``; records (start, end, text) of every submission."""
    lock = threading.Lock()

    def on_prompt(fake, name, text, wait):
        start = time.monotonic()
        T.claude_user(path, text)
        fake.set_status(name, "working")

        def finish():
            with lock:
                T.claude_end(path, f"reply to {text}")
                fake.set_status(name, "idle")
                events.append((start, time.monotonic(), text))

        T.later(delay, finish)

    return on_prompt


def handle(c, tmp_path, path):
    a = A.Agent(c, name="a", kind="claude", policy="full_auto", workspace_id="w1", pane_id="w1:p1",
                label="LABEL", session_id="sid")
    a._log_path = str(path)
    return a


def run_two(asks):
    results, errors = {}, []

    def go(key, fn):
        try:
            results[key] = fn()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go, args=(k, fn)) for k, fn in asks.items()]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    return results, errors


def assert_serial(events):
    events = sorted(events)
    assert len(events) == 2 and events[1][0] >= events[0][1], events


def test_two_threads_same_handle(tmp_path):
    """Scenario: 并发提问同一 agent."""
    c, agent, path = T.make(tmp_path)
    events = []
    c.on_prompt = slow_claude(path, events)
    results, errors = run_two({"x": lambda: agent.ask("x", 10), "y": lambda: agent.ask("y", 10)})
    assert errors == [] and results == {"x": "reply to x", "y": "reply to y"}
    assert_serial(events)


def test_two_handles_same_name(tmp_path):
    c, agent, path = T.make(tmp_path)
    other = handle(c, tmp_path, path)
    events = []
    c.on_prompt = slow_claude(path, events)
    results, errors = run_two({"x": lambda: agent.ask("x", 10), "y": lambda: other.ask("y", 10)})
    assert errors == [] and results == {"x": "reply to x", "y": "reply to y"}
    assert_serial(events)


def test_waiting_for_the_turn_times_out(tmp_path):
    """Scenario: 等待超时 — the second ask never gets its turn and submits nothing."""
    c, agent, path = T.make(tmp_path)
    events = []
    c.on_prompt = slow_claude(path, events, delay=0.6)
    results, errors = run_two({"x": lambda: agent.ask("x", 10),
                               "y": lambda: (time.sleep(0.05), agent.ask("y", 0.2))[1]})
    assert results == {"x": "reply to x"}
    assert len(errors) == 1 and isinstance(errors[0], AgentBusyError)
    assert [a[1] for a in c.args_of("agent_prompt")] == ["x"]


FAKE_HERDR = r"""#!/usr/bin/env python3
import fcntl, json, os, sys, time
D = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[3:]
state = os.path.join(D, "status")
def status():
    try:
        return open(state).read().strip() or "idle"
    except FileNotFoundError:
        return "idle"
def agent():
    return {"name": "a", "agent_status": status(), "state_change_seq": 1, "workspace_id": "w1", "pane_id": "w1:p1"}
if args[:2] == ["agent", "get"] or args[:2] == ["agent", "wait"]:
    print(json.dumps({"id": "x", "result": {"agent": agent()}})); sys.exit(0)
if args[:2] == ["agent", "prompt"]:
    text = args[3]
    with open(os.path.join(D, "events"), "a") as f:
        f.write(json.dumps(["start", text, time.time()]) + "\n")
    log = os.path.join(D, "log.jsonl")
    with open(log, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
    time.sleep(0.4)
    with open(log, "a") as f:
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
                                                              "content": [{"type": "text", "text": "reply to " + text}]}}) + "\n")
        f.write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
    with open(os.path.join(D, "events"), "a") as f:
        f.write(json.dumps(["end", text, time.time()]) + "\n")
    print(json.dumps({"id": "x", "result": {"agent": agent()}})); sys.exit(0)
if args[:2] == ["agent", "read"]:
    sys.exit(0)
print(json.dumps({"id": "x", "result": {}}))
"""

CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from polyagent import locks, herdr as H
    from polyagent.agent import Agent
    locks.user_home = lambda: Path(sys.argv[1])
    a = Agent(H.HerdrClient("polyagent", sys.argv[2]), name="a", kind="claude", policy="full_auto",
              workspace_id="w1", pane_id="w1:p1", label="L", session_id="sid")
    a._log_path = sys.argv[3]
    print(a.ask(sys.argv[4], 20), flush=True)
    """
)


def test_two_processes_serialise(tmp_path, lock_home):
    """Scenario: 跨进程提问同一 agent — a fake herdr executable records the submission order."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    herdr = bindir / "herdr"
    herdr.write_text(FAKE_HERDR)
    herdr.chmod(0o755)
    log = bindir / "log.jsonl"
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(lock_home), str(herdr), str(log), f"p{i}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert sorted(o[0].strip() for o in outs) == ["reply to p0", "reply to p1"]
    events = [json.loads(line) for line in (bindir / "events").read_text().splitlines()]
    kinds = [e[0] for e in events]
    assert kinds == ["start", "end", "start", "end"], events
    assert events[0][1] == events[1][1] and events[2][1] == events[3][1]
