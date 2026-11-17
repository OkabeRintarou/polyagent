"""Discussion engine (design D13): configuration checks, the ``starting → brief → dispatch → summary``
state machine, budgets, events, cancellation and cleanup.

The engine only needs two small protocols, so it can be driven by fake agents in tests:

* the factory (``PolyAgent`` at run time): ``start(spec, cwd, name=, start_timeout=, _discussion_id=,
  _default_policy=) -> agent``;
* the agent handle: ``ask_reply(text, timeout, cancel) -> Reply`` (``text``, ``source``),
  ``interrupt()`` and ``close()``.

It does not import ``herdr``, ``agent`` or ``manager``. The static checks that need no agent (D9 step 1)
use ``policy``, ``kinds`` and the prompt sanitiser, which are pure functions of the configuration.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import math
import os
import re
import secrets
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from .. import kinds as K
from .. import policy as P
from .._prompt import max_text_bytes, sanitize
from ..errors import (
    AgentBlockedError,
    AgentNotFoundError,
    AgentTimeoutError,
    AgentTurnAbortedError,
    AgentTurnError,
    AskCancelledError,
    InvalidConfigError,
    InvalidPromptError,
)
from ..spec import AgentSpec, ToolPolicy
from ..template import DEFAULT_TEMPLATE, SummaryTemplate
from . import protocol as PR
from .records import (
    DiscussionRecord,
    EndReason,
    Event,
    EventType,
    Member,
    Message,
    MessageStatus,
    MessageType,
    Phase,
    Redactor,
    Status,
    utc_now,
)

__all__ = ["Discussion", "Participant", "AgentFactory", "DiscussionAgent", "COORDINATOR"]

COORDINATOR = "coordinator"
PARTICIPANT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,22}")
MAX_ROLE_BYTES = 2000
MAX_ATTEMPTS = 3  # one output plus 2 retries (D13)
MAX_ERROR_MESSAGE = 2000
WAIT_SLICE = 0.1  # how often the run thread looks at the cancel flag while waiting for workers
READ_ONLY = ToolPolicy.READ_ONLY.value
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_monotonic = time.monotonic  # replaced by tests that drive a fake clock
log = logging.getLogger("polyagent")
MAX_CALLBACK_MESSAGE = 500  # room left for the earlier error when the finished callback fails (review C8)


class DiscussionAgent(Protocol):
    def ask_reply(self, text: str, timeout: float, cancel: threading.Event | None = None) -> Any: ...
    def interrupt(self) -> None: ...
    def close(self) -> None: ...


class AgentFactory(Protocol):
    def start(self, spec: AgentSpec, cwd: str, name: str | None = None, start_timeout: float = 60, *,
              _discussion_id: str | None = None, _default_policy: str = "full_auto") -> DiscussionAgent: ...


@dataclass(frozen=True)
class Participant:
    """One discussion participant: a unique ``name`` (``[a-z][a-z0-9_-]{0,22}``, not ``coordinator``),
    a role description and the agent to run. The tool policy is always ``read_only``."""

    name: str
    role: str
    spec: AgentSpec

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not PARTICIPANT_NAME_RE.fullmatch(self.name):
            raise InvalidConfigError(f"invalid participant name {self.name!r}: must match [a-z][a-z0-9_-]{{0,22}}")
        if self.name == COORDINATOR:
            raise InvalidConfigError(f"{COORDINATOR!r} is reserved for the coordinator")
        if not isinstance(self.role, str) or not self.role.strip():
            raise InvalidConfigError(f"participant {self.name!r} needs a non-empty role description")
        if _CONTROL_RE.search(self.role.replace("\r\n", "\n")):
            raise InvalidConfigError(f"the role of {self.name!r} contains control characters")
        if PR.utf8_safe(self.role) != self.role:
            raise InvalidConfigError(f"the role of {self.name!r} is not valid Unicode text (lone surrogate)")
        if len(self.role.encode("utf-8")) > MAX_ROLE_BYTES:
            raise InvalidConfigError(f"the role of {self.name!r} is longer than {MAX_ROLE_BYTES} bytes")
        if not isinstance(self.spec, AgentSpec):
            raise InvalidConfigError(f"participant {self.name!r}: spec must be an AgentSpec")


def _positive(value: object, what: str, *, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise InvalidConfigError(f"{what} must be a positive {'integer' if integer else 'number'}")
    try:
        ok = math.isfinite(value) and value > 0 and math.isfinite(float(value))  # type: ignore[arg-type,operator]
    except OverflowError:  # an int too large for a float (review C12)
        ok = False
    if not ok:
        raise InvalidConfigError(f"{what} must be a finite positive number")
    return value


def _member_spec(spec: object, who: str) -> AgentSpec:
    if not isinstance(spec, AgentSpec):
        raise InvalidConfigError(f"{who}: spec must be an AgentSpec")
    if spec.policy == ToolPolicy.FULL_AUTO.value:
        raise InvalidConfigError(f"{who}: discussion members always run read_only; full_auto is not allowed")
    return spec


def _error(e: BaseException) -> dict[str, str]:
    """Type and full message; ``finish`` redacts first and truncates afterwards (review N3)."""
    try:
        message = str(e)
    except Exception:  # an exception whose __str__ fails (review N2)
        message = ""
    return {"type": type(e).__name__, "message": message}


class _End(Exception):
    """Unwinds the state machine to the terminal state it carries."""

    def __init__(self, status: str, reason: str, error: dict[str, str] | None = None):
        super().__init__(reason)
        self.status, self.reason, self.error = status, reason, error


class _CallbackFailed(Exception):
    pass


MARKER_CHARS = 16  # D13: the separator marker, 16 hex characters
_BUDGET = object()  # the coordinator's ask was cut short by the total budget
_INVALID = object()  # three invalid outputs


@dataclass
class _Asked:
    name: str
    status: str
    text: str
    source: str | None
    started_at: str
    duration: float
    unavailable: bool
    finished: float


class Discussion:
    """A coordinator-led discussion between read-only agents (agent-discussion spec).

    Every member runs ``read_only``; ``full_auto`` is refused. Configuration errors raise
    ``InvalidConfigError`` here; :meth:`run` raises ``InvalidConfigError`` or
    ``PolicyNotSupportedError`` before starting any agent and otherwise always returns a
    :class:`DiscussionRecord` (D14), also for failures and cancellation. A ``Discussion`` runs once.

    Example::

        from polyagent import AgentSpec, ClaudeProvider, Discussion, Participant, PolyAgent

        provider = ClaudeProvider.compatible("https://llm.example.com", auth_token=TOKEN)
        claude = AgentSpec(kind="claude", provider=provider)
        d = Discussion(
            PolyAgent(session="discussions"),
            coordinator=claude,
            participants=[
                Participant("critic", "Finds risks and missing cases", claude),
                Participant("builder", "Proposes a concrete design", AgentSpec(kind="copilot")),
            ],
            cwd="/srv/repo",
            max_rounds=4,
        )
        print("reclaim with PolyAgent.cleanup(discussion_id=%r) if this process dies" % d.id)
        record = d.run("Add a cache in front of the user API", on_event=lambda e: db.save(e.to_dict()))
        if record.status == "completed":
            print(record.summary["conclusion"])

    ``cancel()`` may be called from another thread. Agents are closed when the run ends unless
    ``keep_agents=True`` (agents are always closed when a member fails to start)."""

    def __init__(
        self,
        polyagent: AgentFactory,
        coordinator: AgentSpec,
        participants: Sequence[Participant],
        cwd: str | os.PathLike[str],
        template: SummaryTemplate | None = None,
        max_rounds: int = 5,
        turn_timeout: float = 600,
        total_timeout: float = 3600,
        summary_timeout: float = 300,
        start_timeout: float = 60,
        keep_agents: bool = False,
        reply_limit_bytes: int = 20_000,
    ):
        if not callable(getattr(polyagent, "start", None)):
            raise InvalidConfigError("polyagent must provide start(spec, cwd, ...) (a PolyAgent)")
        self._factory = polyagent
        if isinstance(coordinator, AgentSpec) and K.adapter(coordinator.kind).capability(K.LOG_ADAPTER).state != K.VERIFIED:
            raise InvalidConfigError(f"{coordinator.kind} cannot be the coordinator: its replies cannot be read "
                                     "from a session log")
        self._coordinator = _member_spec(coordinator, COORDINATOR)
        # One coordinator input: 64 KiB, or less where the coordinator's kind takes less (task 13.14).
        self._budget = min(PR.INPUT_BUDGET_BYTES, max_text_bytes(K.adapter(self._coordinator.kind)))
        if isinstance(participants, (str, bytes)) or not isinstance(participants, Sequence):
            raise InvalidConfigError("participants must be a sequence of Participant")
        if not participants:
            raise InvalidConfigError("a discussion needs at least one participant")
        names: set[str] = set()
        for p in participants:
            if not isinstance(p, Participant):
                raise InvalidConfigError("participants must be Participant objects")
            if p.name in names:
                raise InvalidConfigError(f"participant name {p.name!r} is used twice")
            names.add(p.name)
            _member_spec(p.spec, f"participant {p.name!r}")
        self._participants = list(participants)
        if not isinstance(cwd, (str, os.PathLike)) or not os.fspath(cwd):
            raise InvalidConfigError("cwd must be a path")
        self._cwd = os.fspath(cwd)
        template = DEFAULT_TEMPLATE if template is None else template
        if not isinstance(template, SummaryTemplate):
            raise InvalidConfigError("template must be a SummaryTemplate")
        problems = template.validate()
        if problems:
            raise InvalidConfigError("invalid summary template: " + "; ".join(problems))
        self._template = template
        self._max_rounds = _positive(max_rounds, "max_rounds", integer=True)
        self._turn_timeout = float(_positive(turn_timeout, "turn_timeout"))
        self._total_timeout = float(_positive(total_timeout, "total_timeout"))
        self._summary_timeout = float(_positive(summary_timeout, "summary_timeout"))
        if isinstance(start_timeout, bool) or not isinstance(start_timeout, (int, float)) or not 3 < start_timeout <= 300:
            raise InvalidConfigError("start_timeout must be in (3, 300] seconds")
        self._start_timeout = start_timeout
        if not isinstance(keep_agents, bool):
            raise InvalidConfigError("keep_agents must be a bool")
        self._keep_agents = keep_agents
        self._reply_limit = _positive(reply_limit_bytes, "reply_limit_bytes", integer=True)
        self._check_summary_budget()
        self._cancel = threading.Event()
        self._ran = threading.Lock()
        self._id = uuid.uuid4().hex

    # ---- public -----------------------------------------------------------------

    @property
    def id(self) -> str:
        """The discussion ID (uuid4 hex), known before :meth:`run`; ``PolyAgent.cleanup(discussion_id=...)``
        reclaims the agents of a run that could not clean up."""
        return self._id

    def cancel(self) -> None:
        """Thread-safe: stop the running discussion; :meth:`run` returns a ``cancelled`` record."""
        self._cancel.set()

    def run(self, requirement: str, on_event: Callable[[Event], Any] | None = None) -> DiscussionRecord:
        """Run the discussion to its end and return the record. ``on_event`` is called on this thread
        only, with strictly increasing ``Event.seq``; an exception it raises ends the discussion with
        ``callback_error`` (except for ``discussion_finished``, which only records the error)."""
        if on_event is not None and not callable(on_event):
            raise InvalidConfigError("on_event must be callable")
        requirement = self._check_requirement(requirement)
        if not os.path.isdir(self._cwd):
            raise InvalidConfigError(f"working directory {self._cwd!r} does not exist")
        # D13: every member's read_only static checks before any agent starts (no record on failure).
        specs = {COORDINATOR: P.check_static(self._coordinator, self._cwd, READ_ONLY)}
        for p in self._participants:
            specs[p.name] = P.check_static(p.spec, self._cwd, READ_ONLY)
        for name, spec in specs.items():  # defence in depth: members only ever run read_only (review C13)
            if spec.policy != READ_ONLY:
                raise InvalidConfigError(f"{name}: discussion members always run read_only")
        # The first input is checked with a placeholder of the marker's length, before the run flag: a
        # call refused for its configuration never holds the flag, not even briefly (phase-8 review C4).
        if len(self._first_input(requirement, "0" * MARKER_CHARS)[1].encode("utf-8")) > self._budget:
            raise InvalidConfigError("the requirement and the participant roles do not fit in one coordinator "
                                     f"input ({self._budget} bytes)")
        if not self._ran.acquire(blocking=False):
            raise InvalidConfigError("this Discussion has already been run")
        try:
            # D13 "分隔标记": generated only once this call owns the run, and kept on the run's own text
            # object, so a rejected concurrent run() cannot replace it (review C1).
            text, first = self._first_input(requirement, secrets.token_hex(MARKER_CHARS // 2))
            run = _Run(self, text, requirement, specs, first, on_event)
        except BaseException:
            self._ran.release()  # nothing was started; the object stays usable (review C14)
            raise
        return run.execute()

    # ---- helpers ------------------------------------------------------------------

    def _names(self) -> list[str]:
        return [p.name for p in self._participants]

    def _check_summary_budget(self) -> None:
        """D13 "协调者输入的长度": the summary input's fixed text plus room for feedback fits the budget,
        and with every participant answering each reply still gets ``MIN_REPLY_SHARE_BYTES``."""
        text = PR.ProtocolText({p.name: p.role for p in self._participants}, self._template, "0" * MARKER_CHARS)
        footer = max((text.summary_request(r) for r in PR.SUMMARY_REASONS), key=lambda t: len(t.encode("utf-8")))
        footer += text.reminder([PR.SUMMARY], self._names(), None, self._summary_timeout)
        kind = self._coordinator.kind
        fixed = len(footer.encode("utf-8")) + PR.FEEDBACK_RESERVE_BYTES
        if fixed > self._budget:
            raise InvalidConfigError(
                f"the summary template does not fit a {kind} coordinator's input: the summary request with its "
                f"reminder and {PR.FEEDBACK_RESERVE_BYTES} bytes for feedback take {fixed} of {self._budget} bytes")
        share = PR.worst_reply_share(self._max_rounds, self._names(), footer, self._budget, text)
        if share < PR.MIN_REPLY_SHARE_BYTES:
            raise InvalidConfigError(
                f"{len(self._participants)} participants are too many for a {kind} coordinator with this template: "
                f"each reply would get {max(share, 0)} bytes of its {self._budget}-byte input, less than "
                f"{PR.MIN_REPLY_SHARE_BYTES}")

    def _first_input(self, requirement: str, marker: str) -> tuple[PR.ProtocolText, str]:
        text = PR.ProtocolText({p.name: p.role for p in self._participants}, self._template, marker)
        opening = text.opening(requirement, self._max_rounds, self._total_timeout, self._turn_timeout)
        return text, opening + "\n" + text.reminder([PR.BRIEF], self._names(), None, None)

    def _check_requirement(self, requirement: object) -> str:
        if not isinstance(requirement, str):
            raise InvalidConfigError("the requirement must be a string")
        text = requirement.replace("\r\n", "\n")
        if not text.strip():
            raise InvalidConfigError("the requirement is empty")
        if PR.utf8_safe(text) != text:
            raise InvalidPromptError("the requirement is not valid Unicode text (lone surrogate)")
        if len(text.encode("utf-8")) > PR.MAX_REQUIREMENT_BYTES:
            raise InvalidConfigError(f"the requirement is longer than {PR.MAX_REQUIREMENT_BYTES} bytes (UTF-8)")
        bad = _CONTROL_RE.search(text)
        if bad:
            raise InvalidPromptError(f"the requirement contains a control character (U+{ord(bad.group()):04X})")
        return text


class _Run:
    """State of one :meth:`Discussion.run`."""

    def __init__(self, d: Discussion, text: PR.ProtocolText, requirement: str, specs: dict[str, AgentSpec],
                 first_input: str, on_event: Callable[[Event], Any] | None):
        self.d = d
        self.text = text  # this run's texts and separator marker; never replaced while the run lives
        self.specs = specs
        self.first_input = first_input
        self.callback = on_event
        self.cancel = d._cancel
        self.id = d.id
        values: list[str] = [text.marker]  # the separator marker is redacted like a credential (D13)
        for spec in specs.values():
            values += spec.secret_values()
        self.redact = Redactor(values)
        roles = {COORDINATOR: "coordinator", **{p.name: p.role for p in d._participants}}
        members = [
            Member(name=name, role=self.redact.text(roles[name]), kind=spec.kind, model=spec.model,
                   provider=spec.provider.type if spec.provider is not None else None)
            for name, spec in specs.items()
        ]
        self.record = DiscussionRecord(id=self.id, requirement=self.redact.text(requirement),
                                       template=d._template.to_dict(), members=members)
        self.agents: dict[str, DiscussionAgent] = {}
        self.unavailable: set[str] = set()
        self.introduced: set[str] = set()
        self.event_seq = 0
        self.rounds = 0
        self.brief: dict[str, Any] | None = None
        self.t_brief: float | None = None
        self.t_summary = 0.0
        self.budget_unsent = False
        self.last_coordinator: tuple[str | None, str, float] = (None, utc_now(), 0.0)
        self.pool = cf.ThreadPoolExecutor(max_workers=len(specs), thread_name_prefix=f"polyagent-{self.id[:8]}")
        self.start_futures: list[cf.Future] = []
        self.start_names: dict[cf.Future, str] = {}

    # ---- driver -------------------------------------------------------------------

    def execute(self) -> DiscussionRecord:
        rec = self.record
        try:
            try:
                self.emit(EventType.DISCUSSION_STARTED, {"record": rec.to_dict()})
                self.drive()
            except _End as end:
                self.finish(end.status, end.reason, end.error)
            except _CallbackFailed:
                pass  # finish() already ran
            except Exception as e:  # unclassified: a library defect, never raised out of run() (D13 "内部错误")
                trace = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                log.error("discussion %s: internal error %s: %s\n%s", self.id, type(e).__name__,
                          self.redact.text(PR.utf8_safe(_error(e)["message"])), self.redact.text(PR.utf8_safe(trace)))
                self.finish(Status.FAILED.value, EndReason.INTERNAL_ERROR.value, _error(e))
        finally:
            self.cleanup()  # also when a BaseException from the callback propagates (D14)
        rec.finished_at = utc_now()
        callback, self.callback = self.callback, None
        if callback is not None:
            self.event_seq += 1
            try:
                event = Event(self.event_seq, EventType.DISCUSSION_FINISHED.value, self.id, utc_now(),
                              self.safe({"record": rec.to_dict()}))
            except Exception as e:  # a library defect: the record is still returned (review N2)
                log.error("discussion %s: could not build the discussion_finished event: %s: %s", self.id,
                          type(e).__name__, self.redact.text(PR.utf8_safe(_error(e)["message"])))
                return rec
            try:
                callback(event)
            except Exception as e:  # the terminal state is final; only the error is recorded (D14)
                err = _error(e)
                err["message"] = self.safe(err["message"])[:MAX_CALLBACK_MESSAGE]
                if rec.error is not None:
                    err["message"] = (f"{err['message']} (the discussion had already ended "
                                      f"with {rec.error['type']}: {rec.error['message']})")
                rec.error = self.safe_error(err)
        return rec

    def drive(self) -> None:
        self.phase(Phase.STARTING)
        self.start_members()
        self.phase(Phase.BRIEF)
        self.t_brief = _monotonic()
        self.run_brief()
        self.phase(Phase.DISPATCH)
        reason, results = self.run_dispatch()
        self.phase(Phase.SUMMARY)
        self.run_summary(reason, results)

    def finish(self, status: str, reason: str, error: dict[str, str] | None) -> None:
        rec = self.record
        if rec.status != Status.RUNNING.value:
            return
        rec.status = status
        rec.end_reason = reason
        rec.error = self.safe_error(error) if error is not None else None
        if status != Status.COMPLETED.value:
            rec.summary = None

    def forward(self, text: str) -> str:
        """One agent's text on its way into another agent's input (D13 "成员之间转发的文本"):
        normalised, control characters removed (so a split credential joins up again), redacted.
        Truncation, where it applies, comes last (``fit_results``)."""
        return self.redact.text(PR.clean(text))

    def safe_error(self, error: dict[str, str]) -> dict[str, str]:
        out = self.safe(error)
        out["message"] = out["message"][:MAX_ERROR_MESSAGE]
        return out

    def safe(self, value: Any) -> Any:
        """What may enter the record and events: redacted, lone surrogates replaced, a fresh copy."""
        return self.redact(PR.deep(PR.utf8_safe, value))

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise _End(Status.CANCELLED.value, EndReason.CANCELLED.value)

    # ---- events -------------------------------------------------------------------

    def emit(self, type_: EventType, payload: dict[str, Any]) -> None:
        if self.callback is None:
            return
        self.event_seq += 1
        event = Event(self.event_seq, type_.value, self.id, utc_now(), self.safe(payload))
        try:
            self.callback(event)
        except Exception as e:
            self.callback = None  # never called again (D14)
            self.finish(Status.FAILED.value, EndReason.CALLBACK_ERROR.value, _error(e))
            raise _CallbackFailed() from None

    def phase(self, p: Phase) -> None:
        self.check_cancel()
        self.emit(EventType.PHASE_CHANGED, {"phase": p.value})

    def add(self, *, sender: str, receiver: str | None, type_: MessageType, content: Any, status: MessageStatus,
            source: str | None, started_at: str, duration: float) -> Message:
        msg = Message(seq=len(self.record.messages) + 1, round=self.rounds, sender=sender, receiver=receiver,
                      type=type_.value, content=self.safe(content), status=status.value, reply_source=source,
                      started_at=started_at, duration=round(max(duration, 0.0), 3))
        self.record.messages.append(msg)
        self.emit(EventType.MESSAGE, {"message": msg.to_dict()})
        return msg

    def mark_unavailable(self, name: str, why: str) -> None:
        if name in self.unavailable:
            return
        self.unavailable.add(name)
        member = self.record.member(name)
        member.available = False
        self.emit(EventType.MEMBER_UNAVAILABLE, {"member": member.to_dict(), "reason": why})

    def available(self) -> list[str]:
        return [n for n in self.d._names() if n not in self.unavailable]

    # ---- starting -------------------------------------------------------------------

    def start_members(self) -> None:
        d = self.d
        futures = {}
        for name, spec in self.specs.items():
            f = self.pool.submit(d._factory.start, spec, d._cwd, name=f"{name}-{self.id[:8]}",
                                 start_timeout=d._start_timeout, _discussion_id=self.id, _default_policy=READ_ONLY)
            futures[f] = name
            self.start_futures.append(f)
            self.start_names[f] = name
        failure: tuple[str, BaseException] | None = None
        pending = set(futures)
        while pending:
            self.check_cancel()
            done, pending = cf.wait(pending, timeout=WAIT_SLICE)
            for f in done:
                name = futures[f]
                try:
                    self.agents[name] = f.result()
                except Exception as e:
                    failure = failure or (name, e)
        if failure is not None:
            name, e = failure
            raise _End(Status.FAILED.value, EndReason.START_FAILED.value,
                       {"type": type(e).__name__, "message": f"member {name!r} did not start: {_error(e)['message']}"})

    # ---- coordinator ----------------------------------------------------------------

    def total_left(self) -> float:
        assert self.t_brief is not None
        return self.d._total_timeout - (_monotonic() - self.t_brief)

    def ask_coordinator(self, text: str, limit: float, budget_bound: bool, phase: Phase) -> Any:
        """One coordinator turn. Returns the Reply, ``_BUDGET`` (cut short by the total budget and
        interrupted), or an ``AgentTurnAbortedError`` (counted as an invalid output)."""
        agent = self.agents[COORDINATOR]
        try:
            reply = agent.ask_reply(PR.clean(text), limit, cancel=self.cancel)
        except AgentTurnAbortedError as e:
            if self.cancel.is_set():
                self.check_cancel()
            return e
        except AgentTimeoutError as e:
            self.check_cancel()
            if budget_bound:
                try:
                    agent.interrupt()
                except Exception as ie:
                    raise _End(Status.FAILED.value, EndReason.COORDINATOR_FAILED.value,
                               {"type": type(ie).__name__,
                                "message": f"the coordinator did not return to input after the total time ran out "
                                           f"in the {phase.value} phase: {_error(ie)['message']}"}) from None
                return _BUDGET
            raise _End(Status.FAILED.value, EndReason.COORDINATOR_FAILED.value, _error(e)) from None
        except Exception as e:
            self.check_cancel()
            raise _End(Status.FAILED.value, EndReason.COORDINATOR_FAILED.value, _error(e)) from None
        self.check_cancel()
        if getattr(reply, "source", None) != "session_log":
            raise _End(Status.FAILED.value, EndReason.COORDINATOR_FAILED.value,
                       {"type": "CoordinatorLogUnavailable",
                        "message": "the coordinator's session log is not bound: its reply came from "
                                   f"{getattr(reply, 'source', None)!r}, and actions are only read from the session log"})
        return reply

    def coordinator_action(self, phase: Phase, first_text: str, allowed: Sequence[str]) -> Any:
        """Up to three coordinator outputs for one step; returns the valid ``Action``, ``_BUDGET`` or ``_INVALID``."""
        text = first_text
        sent = False
        self.budget_unsent = False
        for _ in range(MAX_ATTEMPTS):
            self.check_cancel()
            if phase is Phase.SUMMARY:
                left = self.d._summary_timeout - (_monotonic() - self.t_summary)
                if left <= 0:
                    raise _End(Status.FAILED.value, EndReason.COORDINATOR_FAILED.value,
                               {"type": "AgentTimeoutError", "message": "the summary time limit ran out"})
                limit, bound = min(self.d._turn_timeout, left), False
            else:
                left = self.total_left()
                if left <= 0:
                    self.budget_unsent = not sent  # first_text never reached the coordinator (review C7)
                    return _BUDGET
                limit, bound = min(self.d._turn_timeout, left), left < self.d._turn_timeout
            started, t0 = utc_now(), _monotonic()
            sent = True
            reply = self.ask_coordinator(text, limit, bound, phase)
            duration = _monotonic() - t0
            if reply is _BUDGET:
                return _BUDGET
            if isinstance(reply, AgentTurnAbortedError):
                detail = f"the turn was aborted: {reply}"
                self.add(sender=COORDINATOR, receiver=None, type_=MessageType.COORDINATOR_OUTPUT,
                         content={"output": None, "errors": [detail]}, status=MessageStatus.INVALID, source=None,
                         started_at=started, duration=duration)
                text = PR.within_budget(self.text.aborted(_error(reply)["message"]), self.reminder(allowed),
                                        self.d._budget)
                continue
            reply_text = PR.utf8_safe(reply.text)  # first step on any agent text (D13)
            value, err = PR.extract_action(reply_text)
            outcome = PR.Outcome(None, (err,)) if err else PR.check_action(
                value, allowed, self.d._template, self.available(), self.d._names(), self.participant_prompt,
                self.forward)
            if outcome.action is not None and outcome.action.kind == PR.BRIEF:
                outcome = self.check_brief(outcome.action)
            if outcome.action is not None:
                self.last_coordinator = (reply.source, started, duration)
                return outcome.action
            self.add(sender=COORDINATOR, receiver=None, type_=MessageType.COORDINATOR_OUTPUT,
                     content={"output": reply_text, "errors": list(outcome.errors)}, status=MessageStatus.INVALID,
                     source=reply.source, started_at=started, duration=duration)
            text = PR.within_budget(self.text.feedback(outcome.errors, tail=False),
                                    "\n" + PR.FEEDBACK_TAIL + self.reminder(allowed), self.d._budget)
        return _INVALID

    def check_brief(self, action: PR.Action) -> PR.Outcome:
        """The brief goes into every participant's first prompt: control characters are removed now,
        and it must fit (with a question) into the first prompt of every available participant, so a
        bad brief is fed back in the brief phase instead of failing every later dispatch (review C4)."""
        brief = PR.deep(self.forward, action.data["brief"])
        if not brief["goal"].strip():
            return PR.Outcome(None, ("$.brief.goal: must not be empty",))
        size = len(PR.brief_block(brief).encode("utf-8"))
        if size > PR.MAX_BRIEF_BYTES:
            return PR.Outcome(None, (f"$.brief: the brief is {size} bytes as sent to participants; keep it "
                                     f"under {PR.MAX_BRIEF_BYTES} bytes",))
        errors = []
        for name in self.available():
            try:
                self.participant_prompt(name, PR.BRIEF_PROBE_QUESTION, None, brief=brief, first=True)
            except Exception as e:
                errors.append(f"$.brief: the brief cannot be sent to participant {name!r}: {_error(e)['message']}")
        if errors:
            return PR.Outcome(None, tuple(errors))
        return PR.Outcome(PR.Action(PR.BRIEF, {**action.data, "brief": brief}), ())

    def reminder(self, allowed: Sequence[str]) -> str:
        if PR.SUMMARY in allowed:
            left = self.d._summary_timeout - (_monotonic() - self.t_summary)
            return self.text.reminder(allowed, self.available(), None, left)
        return self.text.reminder(allowed, self.available(), self.d._max_rounds - self.rounds,
                                     self.total_left() if self.t_brief is not None else None)

    def add_action(self, action: PR.Action, type_: MessageType, content: Any) -> None:
        source, started, duration = self.last_coordinator
        self.add(sender=COORDINATOR, receiver=None, type_=type_, content=content, status=MessageStatus.OK,
                 source=source, started_at=started, duration=duration)

    # ---- phases ---------------------------------------------------------------------

    def run_brief(self) -> None:
        action = self.coordinator_action(Phase.BRIEF, self.first_input, [PR.BRIEF])
        if action is _BUDGET:
            raise _End(Status.FAILED.value, EndReason.BUDGET_EXHAUSTED.value,
                       {"type": "BudgetExhausted", "message": "the total time ran out before the coordinator gave a brief"})
        if action is _INVALID:
            raise _End(Status.FAILED.value, EndReason.COORDINATOR_INVALID.value,
                       {"type": "CoordinatorInvalid",
                        "message": f"the coordinator gave {MAX_ATTEMPTS} invalid outputs in the brief phase"})
        self.brief = PR.deep(str, action.data["brief"])  # the engine's own copy (review C6)
        self.add_action(action, MessageType.BRIEF, self.brief)
        self.record.brief = self.safe(self.brief)

    def run_dispatch(self) -> tuple[str, list[PR.RoundResult] | None]:
        results: list[PR.RoundResult] | None = None
        intro = self.text.after_brief()
        while True:
            self.check_cancel()
            if self.rounds >= self.d._max_rounds or self.total_left() <= 0:
                return EndReason.BUDGET_EXHAUSTED.value, results
            if not self.available():
                return EndReason.PARTICIPANTS_UNAVAILABLE.value, results
            allowed = [PR.DISPATCH, PR.FINISH]
            footer = self.reminder(allowed)
            if results is None:
                text = intro + footer
            else:
                text = PR.fit_results(self.rounds, results, "", footer, self.d._reply_limit, self.d._budget,
                                      text=self.text, forward=self.forward)
            action = self.coordinator_action(Phase.DISPATCH, text, allowed)
            if action is _BUDGET:  # results already seen by the coordinator are not sent again
                return EndReason.BUDGET_EXHAUSTED.value, results if self.budget_unsent else None
            if action is _INVALID:
                return EndReason.COORDINATOR_INVALID.value, None
            if action.kind == PR.FINISH:
                self.add_action(action, MessageType.FINISH, {"reason": action.data["reason"]})
                return EndReason.FINISHED.value, None
            if self.total_left() <= 0:  # not executed: recorded as cancelled, not a round (D13 "分派")
                source, _, _ = self.last_coordinator
                for a in action.data["assignments"]:
                    self.add(sender=COORDINATOR, receiver=a["to"], type_=MessageType.DISPATCH,
                             content={"question": a["question"], **({"context": a["context"]} if "context" in a else {})},
                             status=MessageStatus.CANCELLED, source=source, started_at=utc_now(), duration=0.0)
                return EndReason.BUDGET_EXHAUSTED.value, None
            results = self.run_round(action.data["assignments"])

    def run_round(self, assignments: list[dict[str, Any]]) -> list[PR.RoundResult]:
        self.rounds += 1
        source, _, _ = self.last_coordinator
        timeout = min(self.d._turn_timeout, self.total_left())
        jobs = []
        for a in assignments:
            name = a["to"]
            prompt = self.participant_prompt(name, a["question"], a.get("context"))
            first = name not in self.introduced
            self.add(sender=COORDINATOR, receiver=name, type_=MessageType.DISPATCH,
                     content={"question": a["question"], **({"context": a["context"]} if "context" in a else {})},
                     status=MessageStatus.OK, source=source, started_at=utc_now(), duration=0.0)
            jobs.append((name, prompt, first))
        futures = {self.pool.submit(self.ask_participant, name, prompt, timeout): (name, first)
                   for name, prompt, first in jobs}
        results: list[PR.RoundResult] = []
        pending = set(futures)
        while pending:
            self.check_cancel()
            done, pending = cf.wait(pending, timeout=WAIT_SLICE)
            for f in sorted(done, key=lambda f: f.result().finished):  # completion order (D13)
                asked: _Asked = f.result()
                name, first = futures[f]
                if asked.status == MessageStatus.OK.value and first:
                    self.introduced.add(name)
                ok = asked.status == MessageStatus.OK.value
                self.add(sender=name, receiver=COORDINATOR, type_=MessageType.REPLY, content=asked.text,
                         status=MessageStatus(asked.status), source=asked.source if ok else None,
                         started_at=asked.started_at, duration=asked.duration)
                if asked.unavailable:
                    self.mark_unavailable(name, self.redact.text(PR.utf8_safe(f"{asked.status}: {asked.text}"))[:MAX_ERROR_MESSAGE])
                results.append(PR.RoundResult(name, asked.status, asked.text if ok else "", asked.unavailable))
        return results

    def run_summary(self, reason: str, results: list[PR.RoundResult] | None) -> None:
        self.t_summary = _monotonic()
        request = self.text.summary_request(reason)
        footer = request + self.reminder([PR.SUMMARY])
        text = footer if results is None else PR.fit_results(self.rounds, results, "", footer, self.d._reply_limit,
                                                             self.d._budget, text=self.text, forward=self.forward)
        try:
            action = self.coordinator_action(Phase.SUMMARY, text, [PR.SUMMARY])
        except _End as end:
            if end.reason == EndReason.COORDINATOR_FAILED.value and end.error is not None:
                end.error["message"] = (f"{end.error['message']} (the summary phase was entered because of "
                                        f"{reason})")
            raise
        if action is _INVALID:
            raise _End(Status.FAILED.value, EndReason.SUMMARY_INVALID.value,
                       {"type": "SummaryInvalid",
                        "message": f"the coordinator gave {MAX_ATTEMPTS} invalid summaries (the summary phase was "
                                   f"entered because of {reason})"})
        summary = action.data["summary"]
        self.add_action(action, MessageType.SUMMARY, summary)
        self.record.summary = self.safe(summary)
        raise _End(Status.COMPLETED.value, reason)

    # ---- participants ---------------------------------------------------------------

    def participant_prompt(self, name: str, question: str, context: str | None, *,
                           brief: dict[str, Any] | None = None, first: bool | None = None) -> str:
        """The prompt for ``name``; raises ``InvalidPromptError`` when its kind cannot take it (D10)."""
        first = name not in self.introduced if first is None else first
        text = self.text.participant(name, self.brief if brief is None else brief, question, context, first)
        sanitize(K.adapter(self.specs[name].kind), text)
        return text

    def ask_participant(self, name: str, prompt: str, timeout: float) -> _Asked:
        """Worker thread: ask and map the outcome to a message status (D16); no events here."""
        agent = self.agents[name]
        started, t0 = utc_now(), _monotonic()
        status, text, source, unavailable = MessageStatus.OK.value, "", None, False
        try:
            reply = agent.ask_reply(prompt, timeout, cancel=self.cancel)
            text, source = PR.utf8_safe(reply.text), reply.source
        except AgentTimeoutError as e:
            status, text = MessageStatus.TIMEOUT.value, _error(e)["message"]
            if not self.cancel.is_set():
                try:  # back to accepting input before the next dispatch (D13)
                    agent.interrupt()
                except Exception as ie:
                    unavailable, text = True, f"{_error(e)['message']}; interrupt failed: {_error(ie)['message']}"
        except AgentBlockedError as e:
            status, text = MessageStatus.BLOCKED.value, _error(e)["message"]
        except AgentTurnAbortedError as e:
            status, text = MessageStatus.ABORTED.value, _error(e)["message"]
        except AgentTurnError as e:
            status, text = MessageStatus.ERROR.value, _error(e)["message"]
        except AskCancelledError as e:
            status, text = MessageStatus.CANCELLED.value, _error(e)["message"]
        except AgentNotFoundError as e:
            status, text, unavailable = MessageStatus.FAILED.value, _error(e)["message"], True
        except Exception as e:
            status, text = MessageStatus.FAILED.value, f"{type(e).__name__}: {_error(e)['message']}"
        end = _monotonic()
        return _Asked(name, status, text, source, started, end - t0, unavailable, end)

    # ---- cleanup --------------------------------------------------------------------

    def cleanup(self) -> None:
        """Stop in-flight asks, close the agents (unless kept), wait for the workers (D13)."""
        self.cancel.set()
        closing = self.record.end_reason == EndReason.START_FAILED.value or not self.d._keep_agents
        closed: set[int] = set()

        def close(member: str, agent: Any) -> None:
            if id(agent) in closed:
                return
            closed.add(id(agent))
            try:
                agent.close()
                return
            except Exception as e:  # not retried: PolyAgent.cleanup(discussion_id=...) reclaims it (D13)
                error = e
            log.warning("discussion %s: could not close member %s (agent %s): %s: %s; reclaim it with "
                        "PolyAgent.cleanup(discussion_id=%r)", self.id, member, getattr(agent, "name", "?"),
                        type(error).__name__, self.redact.text(PR.utf8_safe(str(error))), self.id)

        def step(what: str, fn: Callable[[], Any]) -> None:
            try:  # each step on its own: the terminal state is final (D13 "内部错误")
                fn()
            except Exception as e:
                log.warning("discussion %s: cleanup step %r failed: %s: %s", self.id, what, type(e).__name__,
                            self.redact.text(PR.utf8_safe(_error(e)["message"])))

        def close_known() -> None:
            for member, agent in list(self.agents.items()):
                close(member, agent)

        def close_late() -> None:  # agents whose start finished after the run stopped waiting
            for f in self.start_futures:
                if f.done() and not f.cancelled() and f.exception() is None:
                    close(self.start_names[f], f.result())

        if closing:
            step("close members", close_known)
        step("wait for workers", lambda: self.pool.shutdown(wait=True))
        if closing:
            step("close late members", close_late)
