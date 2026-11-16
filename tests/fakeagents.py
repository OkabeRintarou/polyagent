"""Fake agents for discussion-engine unit tests (design D17: the engine is tested with fake agents).

The fakes are not looser than the real handle where it matters to the engine: every prompt passes
the real prompt sanitiser of the agent's kind, a turn longer than the given timeout raises
``AgentTimeoutError`` after advancing the clock by exactly the timeout, and a closed agent refuses asks.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from polyagent import kinds as K
from polyagent._rules import validate_agent_name, validate_discussion_id
from polyagent._prompt import sanitize
from polyagent.agent import Reply
from polyagent.errors import AgentNotFoundError, AgentTimeoutError, AskCancelledError


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.lock = threading.Lock()

    def time(self) -> float:
        with self.lock:
            return self.t

    def advance_to(self, t: float) -> None:
        with self.lock:
            self.t = max(self.t, t)


@dataclass
class Turn:
    """One scripted turn: ``reply`` text (or an exception to raise), its ``duration`` on the fake
    clock, and the reply ``source``. ``wait_cancel`` blocks (real time) until the cancel event."""

    reply: Any = ""
    duration: float = 1.0
    source: str = "session_log"
    wait_cancel: bool = False
    barrier: threading.Barrier | None = None


Script = Callable[["FakeAgent", str], Any]


@dataclass
class FakeAgent:
    member: str
    name: str
    kind: str
    spec: Any
    clock: FakeClock
    script: list[Any] | Script = field(default_factory=list)
    interrupt_error: BaseException | None = None
    close_errors: list[BaseException] = field(default_factory=list)  # raised by successive close() calls
    prompts: list[str] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    interrupts: int = 0
    closed: bool = False
    close_calls: int = 0
    gone: threading.Event = field(default_factory=threading.Event)

    def _next(self, prompt: str) -> Turn:
        if callable(self.script):
            step = self.script(self, prompt)
        elif self.script:
            step = self.script.pop(0)
        else:
            step = f"default reply from {self.member}"
        if callable(step) and not isinstance(step, Turn):
            step = step(self, prompt)
        return step if isinstance(step, Turn) else Turn(step)

    def ask_reply(self, text: str, timeout: float, cancel: threading.Event | None = None) -> Reply:
        if self.closed:
            raise AgentNotFoundError(f"agent {self.name} has been closed")
        if not (isinstance(timeout, float) and timeout > 0):  # pytest.fail is a BaseException: never absorbed
            pytest.fail(f"ask timeout must be a positive float, got {timeout!r}")
        sanitize(K.adapter(self.kind), text)  # raises like the real handle
        self.prompts.append(text)
        self.timeouts.append(timeout)
        self.threads.append(threading.current_thread().name)
        start = self.clock.time()
        turn = self._next(text)
        if turn.barrier is not None:
            turn.barrier.wait()
        if turn.wait_cancel:
            if cancel is None:
                pytest.fail("a waiting turn needs the cancel event")
            deadline = time.monotonic() + 10
            while not cancel.is_set() and not self.gone.is_set():
                if time.monotonic() > deadline:
                    pytest.fail("the test never cancelled")
                time.sleep(0.005)
            if self.gone.is_set() and not cancel.is_set():  # like the real handle: close() ends the ask
                raise AgentNotFoundError(f"agent {self.name} has been closed")
            raise AskCancelledError(f"ask to {self.name} cancelled")
        if cancel is not None and cancel.is_set():
            raise AskCancelledError(f"ask to {self.name} cancelled")
        if turn.duration > timeout:
            self.clock.advance_to(start + timeout)
            raise AgentTimeoutError(f"agent {self.name} did not finish within {timeout:g}s", status="working")
        self.clock.advance_to(start + turn.duration)
        if isinstance(turn.reply, BaseException):
            raise turn.reply
        return Reply(text=turn.reply, source=turn.source, duration=turn.duration)

    def interrupt(self) -> None:
        if self.closed:
            raise AgentNotFoundError(f"agent {self.name} has been closed")
        self.interrupts += 1
        if self.interrupt_error is not None:
            raise self.interrupt_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_errors:
            raise self.close_errors.pop(0)
        self.closed = True
        self.gone.set()


class FakeFactory:
    """Stands in for ``PolyAgent``: ``start`` returns a :class:`FakeAgent` per member."""

    def __init__(self, clock: FakeClock, scripts: dict[str, Any] | None = None,
                 start_errors: dict[str, BaseException] | None = None):
        self.clock = clock
        self.scripts = scripts or {}
        self.start_errors = start_errors or {}
        self.calls: list[dict[str, Any]] = []
        self.agents: dict[str, FakeAgent] = {}
        self.interrupt_errors: dict[str, BaseException] = {}
        self.close_errors: dict[str, list[BaseException]] = {}
        self.lock = threading.Lock()

    def start(self, spec, cwd, name=None, start_timeout=60, *, _discussion_id=None, _default_policy="full_auto"):
        validate_agent_name(name)  # what the real PolyAgent.start checks (review T4)
        validate_discussion_id(_discussion_id)
        if not (_default_policy == "read_only" and spec.policy == "read_only"):
            pytest.fail("discussion members must be started read_only")
        member = name.rsplit("-", 1)[0]
        with self.lock:
            self.calls.append({"spec": spec, "cwd": cwd, "name": name, "start_timeout": start_timeout,
                               "discussion_id": _discussion_id, "default_policy": _default_policy,
                               "member": member})
        if member in self.start_errors:
            raise self.start_errors[member]
        agent = FakeAgent(member, name, spec.kind, spec, self.clock, self.scripts.get(member, []),
                          interrupt_error=self.interrupt_errors.get(member),
                          close_errors=list(self.close_errors.get(member, [])))
        with self.lock:
            self.agents[member] = agent
        return agent

    @property
    def started(self) -> int:
        return len(self.calls)


# ---- coordinator action texts -------------------------------------------------------------


def action(obj: dict[str, Any], prose: str = "Some reasoning the library must ignore.") -> str:
    return f"{prose}\n```json\n{json.dumps(obj, ensure_ascii=False)}\n```"


BRIEF = {"goal": "Design a cache", "constraints": ["stdlib only"], "assumptions": ["QPS is about 1000"],
         "acceptance_criteria": ["p99 < 10ms"]}


def brief(**override: Any) -> str:
    return action({"action": "brief", "brief": {**BRIEF, **override}})


def dispatch(*pairs: tuple[str, str] | tuple[str, str, str]) -> str:
    out = []
    for p in pairs:
        a = {"to": p[0], "question": p[1]}
        if len(p) > 2:
            a["context"] = p[2]  # type: ignore[misc]
        out.append(a)
    return action({"action": "dispatch", "assignments": out})


def finish(reason: str = "enough information") -> str:
    return action({"action": "finish", "reason": reason})


SUMMARY = {
    "title": "Cache design", "conclusion": "Use an LRU cache", "requirements": ["cache"],
    "assumptions": ["QPS 1000"], "decisions": [{"decision": "LRU", "rationale": "simple"}],
    "disagreements": [], "risks": [{"risk": "stale data"}], "action_items": [{"item": "prototype"}],
    "open_questions": [],
}


def summary(obj: dict[str, Any] | None = None) -> str:
    return action({"action": "summary", "summary": SUMMARY if obj is None else obj})
