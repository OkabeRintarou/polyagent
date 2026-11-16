"""``Agent`` handle and the ask state machine (design D11, D12).

A handle keeps no turn state: everything an unfinished turn needs is in the JSON-native ``pending``
of ``AgentTimeoutError``, so any handle of the agent (also in another process) can finish it.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import herdr as H
from . import kinds as K
from . import locks
from ._prompt import sanitize
from .capture import ADAPTERS, LogAdapter, Outcome, ScreenCapture, locate, prompt_digest
from .errors import (
    AgentBlockedError,
    AgentBusyError,
    AgentNotFoundError,
    AgentTimeoutError,
    AgentTurnAbortedError,
    AgentTurnError,
    AskCancelledError,
    HerdrError,
    InvalidConfigError,
    PromptNotDeliveredError,
    ReplyNotFoundError,
)

log = logging.getLogger("polyagent")

POLL_INTERVAL = 0.25
CONFIRM_LIMIT = 30.0  # delivery confirmation window (D11)
SETTLE_GRACE = 15.0  # herdr settled but the log has no end marker yet (D12)
UNKNOWN_WAIT = 10.0  # `unknown` status without a log: wait this long for idle/done (D11)
INTERRUPT_WAIT = 15.0
DISMISS_WAIT = 15.0
DENY_LIMIT = 3  # automatic refusals per turn under read_only (spec: the 4th dialog raises)
SCREEN_LINES = 200
PENDING_VERSION = 2  # 2: no log path, the session id instead (D11)
READY = ("idle", "done")

# Named logical keys -> herdr key names. Anything else is refused (D11): herdr also accepts single
# printable characters, which would let text bypass the prompt sanitiser.
KEYS = {
    "enter": "enter", "esc": "esc", "tab": "tab", "up": "up", "down": "down", "left": "left", "right": "right",
    "backspace": "backspace", "ctrl+c": "ctrl+c", "ctrl+u": "ctrl+u",
}


@dataclass(frozen=True)
class Reply:
    text: str
    source: str  # "session_log" | "screen"
    duration: float


class _Stop:
    """Cancel object for herdr waits: the caller's cancel event, or the handle being closed.
    ``submitted`` tells whether this ask may already have a turn of its own (only then does a
    cancellation interrupt the agent, review C5)."""

    def __init__(self, user: threading.Event | None, closed: threading.Event, submitted: bool = False):
        self.user, self.closed, self.submitted = user, closed, submitted

    def is_set(self) -> bool:
        return self.closed.is_set() or (self.user is not None and self.user.is_set())


@dataclass
class _Source:
    """Where this turn's reply comes from: a bound log and its mark; the screen baseline is recorded
    when there is no log or its file does not exist yet (D11 step 4)."""

    log: LogAdapter | None = None
    mark: int = 0
    baseline: list[str] | None = None
    count: int = 0  # occurrences of the baseline block on the pre-submit screen (review N1)

    def pending(self, echo: list[str]) -> dict[str, Any]:
        """D11: a log-bound turn carries its mark and no prompt text and no path (the handle binds the
        log itself); a screen turn carries the baseline and the echo key (first line, 60 characters)."""
        if self.log is not None:
            return {"source": "session_log", "mark": self.mark}
        return {"source": "screen", "baseline": list(self.baseline or []), "count": self.count, "echo": echo}


# `agent explain` lines kept in errors: its `evidence:` line quotes the screen (which may echo the prompt,
# D1) and `manifest:` names a path in the user's home; only the detection result is kept (review C10).
DIAGNOSTIC_FIELDS = ("agent", "state", "rule")


def _diagnostics(text: str) -> str | None:
    kept = [line for line in text.splitlines() if line.split(":", 1)[0].strip() in DIAGNOSTIC_FIELDS]
    return "\n".join(kept)[:2000] or None


def _check_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise InvalidConfigError("timeout must be a finite positive number of seconds")
    return float(timeout)


class Agent:
    """Handle of one agent started by polyagent (``PolyAgent.start`` / ``attach``)."""

    def __init__(
        self,
        client: H.HerdrClient,
        *,
        name: str,
        kind: str,
        policy: str,
        workspace_id: str,
        pane_id: str,
        label: str,
        session_id: str | None = None,
    ):
        self._h = client
        self.name = name
        self.kind = kind
        self.policy = policy
        self.workspace_id = workspace_id
        self.pane_id = pane_id
        self._label = label
        self._session_id = session_id
        self._adapter = K.adapter(kind)
        self._closed = threading.Event()
        self._log_path: str | None = None  # where the log is (a binding, not turn state)
        self._log: LogAdapter | None = None  # the bound adapter, reused so reads stay incremental (C12)
        self._log_resolve: Callable[[], str | None] | None = None
        self._log_bound_to: str | None = None
        self._config: Path | None = None  # from the agent process's environment once it has been read

    def __repr__(self) -> str:
        return (f"Agent(name={self.name!r}, kind={self.kind!r}, policy={self.policy!r}, "
                f"workspace_id={self.workspace_id!r})")

    # ---- small operations ------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed.is_set():
            raise AgentNotFoundError(f"agent {self.name} has been closed")

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """herdr call with ``agent_not_found`` mapped to ``AgentNotFoundError``."""
        try:
            return fn(*args, **kwargs)
        except HerdrError as e:
            if e.code == H.AGENT_NOT_FOUND:
                raise AgentNotFoundError(f"agent {self.name} no longer exists") from None
            raise

    def _info(self) -> dict[str, Any]:
        self._check_open()
        return self._call(self._h.agent_get, self.name)

    def status(self) -> str:
        """``idle``, ``working``, ``blocked``, ``done`` or ``unknown``. For kinds whose herdr status
        is not trusted, a turn still open in the session log reads as ``working``."""
        state = str(self._info().get("agent_status") or "unknown")
        if not self._adapter.status_trusted and state != "blocked":
            src = self._bind_log()
            if src is not None and src.turn_open():
                return "working"
        return state

    def read(self, source: str = "recent-unwrapped", lines: int | None = None) -> str:
        self._check_open()
        return self._call(self._h.agent_read, self.name, source, lines)

    def send_keys(self, *keys: str) -> None:
        """Send named logical keys only (``enter``, ``esc``, ``tab``, arrows, ``ctrl+c`` ...)."""
        if not keys:
            raise InvalidConfigError("no keys given")
        if any(not isinstance(k, str) or k not in KEYS for k in keys):
            raise InvalidConfigError(f"send_keys only takes the named keys {sorted(KEYS)}; text goes through ask()")
        self._check_open()
        self._call(self._h.agent_send_keys, self.name, *(KEYS[k] for k in keys))

    def close(self) -> None:
        """Close the agent's workspace if it still carries this agent's marker. Takes no lock (D11):
        an ask in progress on this handle then fails with ``AgentNotFoundError``. Idempotent."""
        self._closed.set()
        try:
            ws = self._h.workspace_get(self.workspace_id)
        except HerdrError as e:
            if e.code == H.WORKSPACE_NOT_FOUND:
                return
            raise
        if ws.get("label") != self._label:
            log.warning("workspace %s no longer carries agent %s's marker; not closing it", self.workspace_id, self.name)
            return
        try:
            self._h.workspace_close(self.workspace_id)
        except HerdrError as e:
            if e.code != H.WORKSPACE_NOT_FOUND:
                raise
        log.info("closed agent %s (workspace %s)", self.name, self.workspace_id)

    # ---- log binding (D12) --------------------------------------------------------

    def _agent_pids(self) -> list[int]:
        try:
            info = self._h.pane_process_info(self.pane_id).get("process_info") or {}
        except HerdrError:
            return []
        shell = info.get("shell_pid")
        return [p["pid"] for p in info.get("foreground_processes") or []
                if isinstance(p, dict) and isinstance(p.get("pid"), int) and p.get("pid") != shell]

    def _config_dir(self) -> Path:
        """The agent's configuration directory (D12). Only an answer read from the agent process's own
        environment is kept; the default is not, so a later call can still find the real one."""
        if self._config is not None:
            return self._config
        environ = next((e for e in (locate.read_environ(p) for p in self._agent_pids()) if e), None)
        a = self._adapter
        config = locate.config_dir(environ, a.config_dir_var, locks.user_home(), a.default_config_dir)
        if environ is not None:
            self._config = config
        return config

    def _find_log_path(self) -> str | None:
        binding = self._adapter.log_binding
        if binding == "session_id":
            if not self._session_id:
                return None
            sid = self._session_id
            if self.kind == "copilot":
                def resolve() -> str | None:
                    path = locate.copilot_log_path(self._config_dir(), sid)
                    return str(path) if path.is_file() else None
            elif self.kind == "claude":
                # Found by session id under projects/*/, never by encoding the working directory (D12).
                def resolve() -> str | None:
                    return locate.find_claude_log(self._config_dir(), sid)
            else:
                return None
            # Both files appear only with the first user message. Until then the binding has no path
            # and every read looks again: a miss is not cached (D12).
            self._log_resolve = resolve
            return ""
        if binding == "rollout_fd":
            try:
                session = self._h.agent_get(self.name).get("agent_session")
            except HerdrError:
                session = None
            if isinstance(session, str) and os.path.isfile(session):
                return session
            found = locate.open_rollouts(self._agent_pids())
            return found[0] if found else None
        return None

    def _bind_log(self) -> LogAdapter | None:
        cls = ADAPTERS.get(self.kind)
        if cls is None:
            return None
        if self._log is not None and self._log_bound_to == self._log_path:
            return self._log
        if self._log_path is None:
            self._log_path = self._find_log_path()
        if self._log_path is None:
            return None
        self._log = cls(self._log_path, self._log_resolve)
        self._log_bound_to = self._log_path
        return self._log

    # ---- ask --------------------------------------------------------------------

    def ask(self, text: str, timeout: float, cancel: threading.Event | None = None) -> str:
        return self.ask_reply(text, timeout, cancel).text

    def ask_reply(self, text: str, timeout: float, cancel: threading.Event | None = None) -> Reply:
        started = time.monotonic()
        deadline = started + _check_timeout(timeout)
        sent = sanitize(self._adapter, text)
        self._check_open()
        stop = _Stop(cancel, self._closed)
        lock = self._lock(deadline, stop)
        try:
            self._raise_if_stopped(stop)
            denials = [0]  # one refusal budget for the whole ask (D11)
            src, info = self._precheck(deadline, stop, denials)
            self._submit(sent, src, info, deadline, stop, denials)
            return self._wait_turn(src, prompt_digest(sent), sent.splitlines(), deadline, stop, started, denials)
        finally:
            lock.release()

    def wait_reply(self, pending: dict[str, Any], timeout: float, cancel: threading.Event | None = None) -> Reply:
        """Keep waiting for the turn described by ``AgentTimeoutError.pending`` and return its reply."""
        started = time.monotonic()
        deadline = started + _check_timeout(timeout)
        self._check_open()
        src, digest, echo = self._from_pending(pending)
        stop = _Stop(cancel, self._closed, submitted=True)
        lock = self._lock(deadline, stop)
        try:
            self._raise_if_stopped(stop)
            return self._wait_turn(src, digest, echo, deadline, stop, started, [0])
        finally:
            lock.release()

    def _lock(self, deadline: float, stop: _Stop) -> locks.FileLock:
        try:
            return locks.acquire_for_ask(self._h.session, self.name, deadline, stop)
        except (AgentBusyError, AskCancelledError):
            if self._closed.is_set():  # closed while waiting for the turn (D11): not a cancellation
                raise AgentNotFoundError(f"agent {self.name} was closed") from None
            if stop.user is not None and stop.user.is_set():
                raise AskCancelledError(f"ask on agent {self.name} was cancelled before it was submitted") from None
            raise

    def _from_pending(self, pending: object) -> tuple[_Source, str, list[str]]:
        """Check a ``pending`` against this handle (D11): same agent, kind and session; the log is the
        one this handle binds, never a path from ``pending`` (it may have been rewritten)."""
        if not isinstance(pending, dict) or pending.get("v") != PENDING_VERSION or pending.get("agent") != self.name:
            raise InvalidConfigError("pending does not describe an unfinished turn of this agent")
        if pending.get("kind") != self.kind or pending.get("sid") != self._session_id:
            raise InvalidConfigError(f"pending belongs to another session than agent {self.name}'s current one")
        digest = pending.get("digest")
        if not isinstance(digest, str):
            raise InvalidConfigError("malformed pending")
        if pending.get("source") == "session_log":
            mark = pending.get("mark")
            if not isinstance(mark, int) or isinstance(mark, bool) or mark < 0:
                raise InvalidConfigError("malformed pending")
            # A handle that cannot bind its log treats the log as unavailable (D12).
            return _Source(log=self._bind_log(), mark=mark), digest, []
        if pending.get("source") != "screen":
            raise InvalidConfigError("malformed pending")
        echo, baseline, count = pending.get("echo"), pending.get("baseline"), pending.get("count")
        if (not isinstance(echo, list) or not isinstance(baseline, list) or not isinstance(count, int)
                or isinstance(count, bool) or count < (1 if baseline else 0)):
            raise InvalidConfigError("malformed pending")
        return _Source(baseline=[str(x) for x in baseline], count=count), digest, [str(x) for x in echo]

    def _raise_if_stopped(self, stop: _Stop) -> None:
        if self._closed.is_set():
            raise AgentNotFoundError(f"agent {self.name} was closed")
        if stop.is_set():
            if stop.submitted:  # only a turn of this ask may be interrupted (C5)
                try:
                    self.interrupt()
                except Exception as e:  # noqa: BLE001 - the cancellation itself is what the caller asked for
                    log.warning("interrupting agent %s after cancellation failed: %s", self.name, e)
            raise AskCancelledError(f"ask on agent {self.name} was cancelled")

    def _guard(self, fn: Callable[..., Any], stop: _Stop, *args: Any, **kwargs: Any) -> Any:
        """herdr call inside an ask: cancellation or close become the matching error."""
        try:
            return self._call(fn, *args, **kwargs)
        except AskCancelledError:
            self._raise_if_stopped(stop)
            raise

    def _screen(self) -> ScreenCapture:
        return ScreenCapture(self._adapter.footer)

    def _screen_verified(self) -> bool:
        """Screen capture is only used where screen samples back its rules (D12: cursor, codex)."""
        return self._adapter.capability(K.SCREEN_CAPTURE).state == K.VERIFIED

    def _log_unavailable(self) -> ReplyNotFoundError:
        task = self._adapter.capability(K.SCREEN_CAPTURE).task
        return ReplyNotFoundError(f"the session log of agent {self.name} is unavailable and screen capture is not "
                                  f"verified for {self.kind} (task {task}); no reply is taken from the screen",
                                  self._visible())

    def _visible(self) -> str:
        try:
            return self._h.agent_read(self.name, "visible")
        except HerdrError:
            return ""

    def _read_recent(self, lines: int) -> str:
        try:
            return self._h.agent_read(self.name, "recent-unwrapped", lines)
        except HerdrError as e:
            if e.code == H.AGENT_NOT_FOUND:
                raise AgentNotFoundError(f"agent {self.name} no longer exists") from None
            return ""

    # -- step 3: pre-check
    def _precheck(self, deadline: float, stop: _Stop, denials: list[int]) -> tuple[_Source, dict[str, Any]]:
        logsrc = self._bind_log()
        if not self._adapter.status_trusted and logsrc is None:
            raise AgentBusyError(f"herdr's status for {self.kind} is not reliable and the session log of agent "
                                 f"{self.name} cannot be located; cannot confirm that it is idle")
        while True:
            info = self._guard(self._h.agent_get, stop, self.name)
            state = info.get("agent_status")
            if state == "blocked":  # a dialog left by an earlier turn: handle it first (D11 step 3)
                self._on_blocked(denials, stop)
                continue
            waited = False
            if not self._adapter.status_trusted:
                self._wait_log_idle(logsrc, deadline, stop)  # type: ignore[arg-type]
            elif state == "working":
                self._wait_settled(deadline, stop, ())
                waited = True
            elif state not in READY:  # unknown: never taken as idle
                if logsrc is not None:
                    self._wait_log_idle(logsrc, deadline, stop)
                else:
                    self._wait_settled(min(deadline, time.monotonic() + UNKNOWN_WAIT), stop, READY)
                waited = True
            if waited:
                # The state compared for delivery evidence is the one at the end of the pre-check, not
                # the one before waiting (review N2).
                info = self._guard(self._h.agent_get, stop, self.name)
                if info.get("agent_status") == "blocked":
                    continue
            break
        # D11 step 4: a screen baseline when there is no log or its file does not exist yet.
        # The occurrences are counted on the widest window ever read for this turn: a later read of fewer
        # lines can only lose early occurrences, which errs towards "not found" (review N1).
        # A failed read is not an empty screen: an empty baseline would make the whole screen count as
        # new and let an old echo pass for this prompt's. Nothing is submitted yet, so the error is
        # raised as it is (review S1).
        baseline, count = None, 0
        if logsrc is None or not logsrc.exists():
            capture = self._screen()
            screen = self._guard(self._h.agent_read, stop, self.name, "recent-unwrapped", SCREEN_LINES * 4)
            baseline = capture.baseline(screen)
            count = capture.count(screen, baseline)
        return _Source(log=logsrc, mark=logsrc.mark() if logsrc else 0, baseline=baseline, count=count), info

    def _wait_log_idle(self, logsrc: LogAdapter, deadline: float, stop: _Stop) -> None:
        while logsrc.turn_open():
            self._raise_if_stopped(stop)
            if time.monotonic() >= deadline:
                raise AgentBusyError(f"agent {self.name} is still in an earlier turn; nothing was submitted")
            time.sleep(POLL_INTERVAL)

    def _wait_settled(self, deadline: float, stop: _Stop, until: tuple[str, ...]) -> dict[str, Any]:
        remaining = int((deadline - time.monotonic()) * 1000)
        busy = f"agent {self.name} did not become idle in time; nothing was submitted"
        if remaining <= 0:
            raise AgentBusyError(busy)
        try:
            info = self._guard(self._h.agent_wait, stop, self.name, remaining, until, cancel=stop)
        except HerdrError as e:
            if e.code == H.TIMEOUT:
                raise AgentBusyError(busy) from None
            raise
        if info.get("agent_status") not in READY + ("blocked",):
            raise AgentBusyError(busy)
        return info

    # -- step 5: submit and confirm delivery
    def _submit(self, sent: str, src: _Source, info: dict[str, Any], deadline: float, stop: _Stop,
                denials: list[int]) -> None:
        digest = prompt_digest(sent)
        trusted = self._adapter.status_trusted
        while True:
            before = (info.get("agent_status"), info.get("state_change_seq"))
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:  # nothing submitted yet (D11): busy, not a timeout with a pending turn
                raise AgentBusyError(f"the timeout elapsed before agent {self.name} could take the prompt; "
                                     "nothing was submitted")
            stop.submitted = True
            try:
                if trusted:
                    self._guard(self._h.agent_prompt, stop, self.name, sent, remaining, cancel=stop)
                else:  # no --wait, no --timeout; delivery by the log only (D11)
                    self._guard(self._h.agent_prompt, stop, self.name, sent, wait=False, cancel=stop)
                    self._confirm(src, digest, sent, before, deadline, stop, log_only=True)
                return
            except HerdrError as e:
                if e.code == H.AGENT_BLOCKED:  # herdr sent nothing: handle the dialog, then submit (C3, C8)
                    stop.submitted = False
                    self._on_blocked(denials, stop)
                    info = self._guard(self._h.agent_get, stop, self.name)
                    continue
                if e.code == H.AGENT_PROMPT_STALLED and trusted:
                    self._confirm(src, digest, sent, before, deadline, stop, log_only=False)
                    return
                if e.code == H.TIMEOUT and trusted:
                    raise self._timeout(src, digest, sent, "the turn did not end within the timeout", "working") from None
                raise

    def _confirm(self, src: _Source, digest: str, sent: str, before: tuple[Any, Any], deadline: float,
                 stop: _Stop, *, log_only: bool) -> None:
        limit = min(deadline, time.monotonic() + CONFIRM_LIMIT)
        echo = sent.splitlines()
        while True:
            self._raise_if_stopped(stop)
            if src.log is not None and src.log.judge(src.mark, digest).outcome is not Outcome.NOT_SEEN:
                return
            if not log_only:
                if (src.log is None and self._screen_verified()
                        and self._screen().echo_seen(self._read_recent(SCREEN_LINES), src.baseline or [], echo,
                                                     count=src.count)):
                    return
                info = self._guard(self._h.agent_get, stop, self.name)
                if (info.get("agent_status"), info.get("state_change_seq")) != before:
                    return
            now = time.monotonic()
            if now >= limit:
                if now >= deadline:
                    raise self._timeout(src, digest, sent, "the prompt was not confirmed before the timeout", None)
                raise PromptNotDeliveredError(f"could not confirm that agent {self.name} received the prompt; "
                                              "it was not resent")
            time.sleep(POLL_INTERVAL)

    # -- steps 6, 7: wait for the end of the turn, handle dialogs, take the reply
    def _screen_reply(self, src: _Source, echo: list[str], started: float) -> Reply | None:
        capture = self._screen()
        text = capture.extract(self._read_recent(SCREEN_LINES), src.baseline or [], echo, count=src.count)
        if text is None:
            text = capture.extract(self._read_recent(SCREEN_LINES * 4), src.baseline or [], echo, count=src.count)
        return Reply(text, "screen", time.monotonic() - started) if text is not None else None

    def _wait_turn(self, src: _Source, digest: str, echo: list[str], deadline: float, stop: _Stop,
                   started: float, denials: list[int]) -> Reply:
        trusted = self._adapter.status_trusted
        settled_at: float | None = None
        while True:
            self._raise_if_stopped(stop)
            state = self._guard(self._h.agent_get, stop, self.name).get("agent_status")
            if state == "blocked":
                self._on_blocked(denials, stop)
                settled_at = None
                continue
            settled = trusted and state in READY
            settled_at = (settled_at or time.monotonic()) if settled else None
            grace_over = settled_at is not None and time.monotonic() - settled_at >= SETTLE_GRACE
            if src.log is not None:
                j = src.log.judge(src.mark, digest)
                if j.outcome is Outcome.REPLY:
                    return Reply(j.text or "", "session_log", time.monotonic() - started)
                if j.outcome is Outcome.ERROR:
                    raise AgentTurnError(j.text or "the turn ended with an agent error")
                if j.outcome is Outcome.ABORTED:
                    raise AgentTurnAbortedError(f"the turn of agent {self.name} was refused or interrupted and "
                                                "produced no final reply")
                if j.outcome is Outcome.AMBIGUOUS:
                    raise ReplyNotFoundError("the session log cannot tell which end marker belongs to this prompt "
                                             "(it arrived during a turn the agent started by itself)", self._visible())
                if grace_over:
                    if not src.log.exists():  # bound, but the file never appeared: the log is unavailable (D12)
                        if not self._screen_verified():
                            raise self._log_unavailable()
                        reply = self._screen_reply(src, echo, started)
                        if reply is not None:
                            return reply
                        raise ReplyNotFoundError("the session log was not found and the reply could not be "
                                                 "separated from the screen", self._visible())
                    if j.outcome is Outcome.NOT_SEEN:
                        raise PromptNotDeliveredError(f"the turn ended but agent {self.name}'s session log never "
                                                      "showed this prompt")
                    raise ReplyNotFoundError("herdr reports the turn as ended but the session log has no end marker",
                                             self._visible())
            elif settled_at is not None:
                if not self._screen_verified():
                    raise self._log_unavailable()
                reply = self._screen_reply(src, echo, started)
                if reply is not None:
                    return reply
                if grace_over or time.monotonic() >= deadline:
                    raise ReplyNotFoundError("could not separate this prompt's reply from the screen", self._visible())
            if time.monotonic() >= deadline:
                raise self._timeout(src, digest, "\n".join(echo), "the turn did not end within the timeout", state)
            time.sleep(POLL_INTERVAL)

    def _on_blocked(self, denials: list[int], stop: _Stop) -> None:
        screen = self._visible()
        if self.policy != "read_only":
            raise AgentBlockedError(f"agent {self.name} is waiting at a dialog", screen, self.name)
        if denials[0] >= DENY_LIMIT:
            raise AgentBlockedError(f"agent {self.name} stopped at a dialog again after {DENY_LIMIT} refusals",
                                    screen, self.name)
        denials[0] += 1
        self._guard(self._h.agent_send_keys, stop, self.name, self._adapter.deny_key or "esc")
        try:
            self._guard(self._h.agent_wait, stop, self.name, int(DISMISS_WAIT * 1000), (), cancel=stop)
        except HerdrError as e:
            if e.code != H.TIMEOUT:
                raise

    def _timeout(self, src: _Source, digest: str, sent: str, why: str, state: Any) -> AgentTimeoutError:
        try:
            diag = self._h.agent_explain(self.name)
            diagnostics = _diagnostics(str(diag.get("explanation") or ""))
        except HerdrError:
            diagnostics = None
        first = next((line.strip() for line in sent.splitlines() if line.strip()), "")
        # JSON-native; a digest instead of the prompt (D1). Only a screen turn carries the echo key (D11).
        pending = {"v": PENDING_VERSION, "agent": self.name, "kind": self.kind, "sid": self._session_id,
                   "digest": digest, **src.pending([first[:60]])}
        return AgentTimeoutError(f"agent {self.name}: {why}", state if isinstance(state, str) else None,
                                 diagnostics, pending)

    # ---- interrupt / dismiss ------------------------------------------------------

    def interrupt(self) -> None:
        """Send the kind's interrupt key and wait (bounded) for the turn to end; the key is never resent."""
        self._check_open()
        self._call(self._h.agent_send_keys, self.name, self._adapter.interrupt_key)
        deadline = time.monotonic() + INTERRUPT_WAIT
        if self._adapter.status_trusted:
            try:
                if self._call(self._h.agent_wait, self.name, int(INTERRUPT_WAIT * 1000), READY).get("agent_status") in READY:
                    return
            except HerdrError as e:
                if e.code != H.TIMEOUT:
                    raise
        else:
            logsrc = self._bind_log()
            while logsrc is not None and time.monotonic() < deadline:
                if not logsrc.turn_open():
                    return
                time.sleep(POLL_INTERVAL)
        raise AgentTimeoutError(f"agent {self.name} did not end its turn within {INTERRUPT_WAIT:g}s of the "
                                "interrupt key (not resent)")

    def dismiss(self) -> None:
        """At a dialog: answer it with the kind's refusal key and wait for idle/done. Otherwise nothing."""
        if self._info().get("agent_status") != "blocked":
            return
        self._call(self._h.agent_send_keys, self.name, self._adapter.deny_key or "esc")
        try:
            info = self._call(self._h.agent_wait, self.name, int(DISMISS_WAIT * 1000), READY)
        except HerdrError as e:
            if e.code != H.TIMEOUT:
                raise
            info = {}
        if info.get("agent_status") not in READY:
            raise AgentTimeoutError(f"agent {self.name} did not return to idle after the dialog was dismissed",
                                    status="blocked")
