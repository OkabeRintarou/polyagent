"""Task 12.2: an end-to-end discussion on real agents (POLYAGENT_INTEGRATION=1; consumes model quota).

claude coordinates, one claude and one copilot take part, on a small sample repository, with an
ambiguous requirement. No member names a tool policy; the claude members carry an explicit provider
(the lab's). A second run is cancelled during the dispatch phase. Runs in the safety lab (task 8.1).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from polyagent import DEFAULT_TEMPLATE, Discussion, DiscussionRecord, Participant
from polyagent.template import instance_errors

from .agentlab import require_kind

pytestmark = pytest.mark.integration

REPO_FILES = {
    "README.md": "# greeter\n\nA tiny library that greets people.\n",
    "greeter/__init__.py": "from .core import greet\n",
    "greeter/core.py": 'def greet(name):\n    return "Hello, " + name + "!"\n',
    "tests/test_core.py": 'from greeter import greet\n\n\ndef test_greet():\n    assert greet("Ada") == "Hello, Ada!"\n',
}
REQUIREMENT = ("Make the greeting configurable so that it can be more polite. Keep the change small and say how "
               "it should be tested.")
ROLES = {"reviewer": "Reviews the design for risks and backwards compatibility; answers briefly.",
         "builder": "Proposes the concrete code change; answers briefly."}


class TimedFactory:
    """The lab's PolyAgent, recording when each member's start ran and with which policy it came up."""

    def __init__(self, pa):
        self.pa = pa
        self.spans: dict[str, tuple[float, float]] = {}
        self.policies: dict[str, str] = {}
        self.lock = threading.Lock()

    def start(self, spec, cwd, **kw):
        t0 = time.monotonic()
        try:
            agent = self.pa.start(spec, cwd, **kw)
        finally:
            with self.lock:
                self.spans[kw["name"]] = (t0, time.monotonic())
        with self.lock:
            self.policies[kw["name"]] = agent.policy
        return agent


def _digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


def _discussion(lab, factory, repo, **kw) -> Discussion:
    coordinator = lab.spec("claude")  # no policy: members always run read_only
    participants = [Participant("reviewer", ROLES["reviewer"], lab.spec("claude")),
                    Participant("builder", ROLES["builder"], lab.spec("copilot"))]
    assert all(p.spec.policy is None for p in participants) and coordinator.policy is None
    assert coordinator.provider is not None and participants[0].spec.provider is not None
    return Discussion(factory, coordinator, participants, str(repo), max_rounds=2, turn_timeout=300,
                      total_timeout=900, summary_timeout=300, start_timeout=120, **kw)


def _leftovers(lab, discussion_id: str) -> tuple[list[str], list[str]]:
    """Agents of this discussion still listed, and lab processes other than the lab's herdr server."""
    agents = [a.name for a in lab.pa.list() if a.name.endswith(discussion_id[:8])]
    procs = [cmd for pid, cmd in lab.owned() if "herdr" not in cmd.split(" ")[0]]
    return agents, procs


def _settled_leftovers(lab, discussion_id: str) -> tuple[list[str], list[str]]:
    deadline = time.monotonic() + 15  # closed panes' shells take a moment to exit
    while True:
        left = _leftovers(lab, discussion_id)
        if left == ([], []) or time.monotonic() > deadline:
            return left
        time.sleep(0.5)


def test_discussion_end_to_end_and_cancel(agent_lab):
    for kind in ("claude", "copilot"):
        require_kind(kind, "read_only")
        agent_lab.prepare(kind)
    repo = agent_lab.work("greeter", git=True, files=REPO_FILES)
    before = _digest(repo)

    # -- a full run
    factory = TimedFactory(agent_lab.pa)
    d = _discussion(agent_lab, factory, repo)
    known_id = d.id
    events = []
    rec = d.run(REQUIREMENT, events.append)
    summary = {"status": rec.status, "end_reason": rec.end_reason, "error": rec.error,
               "messages": [(m.type, m.sender, m.status, m.reply_source) for m in rec.messages],
               "invalid_outputs": [m.content["errors"] for m in rec.messages if m.type == "coordinator_output"]}
    print(json.dumps(summary, indent=1))
    assert rec.end_reason != "internal_error", rec.error
    assert rec.status == "completed", summary
    assert rec.id == known_id == events[0].discussion_id
    names = {f"{m}-{known_id[:8]}" for m in ("coordinator", "reviewer", "builder")}
    assert set(factory.policies) == names and set(factory.policies.values()) == {"read_only"}
    spans = list(factory.spans.values())  # the three starts overlap in time (parallel start)
    assert max(s for s, _ in spans) < min(e for _, e in spans), factory.spans
    assert rec.brief is not None and rec.brief["assumptions"], rec.brief
    assert rec.summary is not None and instance_errors(DEFAULT_TEMPLATE.schema, rec.summary) == []
    assert DiscussionRecord.from_dict(json.loads(json.dumps(rec.to_dict()))) == rec
    replies = [m for m in rec.messages if m.type == "reply"]
    assert replies and all(m.reply_source in ("session_log", "screen") for m in replies if m.status == "ok")
    assert {m.sender for m in replies if m.status == "ok"} <= {"reviewer", "builder"}
    coordinator_msgs = [m for m in rec.messages if m.sender == "coordinator"]
    assert coordinator_msgs and all(m.reply_source == "session_log" for m in coordinator_msgs
                                    if m.status == "ok"), [(m.type, m.reply_source) for m in coordinator_msgs]
    assert _settled_leftovers(agent_lab, known_id) == ([], [])
    assert _digest(repo) == before

    # -- a run cancelled while dispatching
    factory = TimedFactory(agent_lab.pa)
    d = _discussion(agent_lab, factory, repo)

    def on_event(e):
        if e.type == "message" and e.payload["message"]["type"] == "dispatch":
            d.cancel()

    rec = d.run(REQUIREMENT, on_event)
    print(json.dumps({"status": rec.status, "end_reason": rec.end_reason,
                      "messages": [(m.type, m.sender, m.status) for m in rec.messages]}, indent=1))
    assert rec.end_reason != "internal_error", rec.error
    assert rec.status == "cancelled" and rec.end_reason == "cancelled" and rec.summary is None
    assert any(m.type == "dispatch" for m in rec.messages)
    assert _settled_leftovers(agent_lab, d.id) == ([], [])
    assert _digest(repo) == before
