"""``PolyAgent`` entry object: session checks, server lifecycle, ownership markers and cleanup.

Design D2 (session protection), D3 (server start/stop), D4 (ownership marker, cleanup).
Agent start/attach/list arrive with task group 6.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, TypeVar

from . import _launch
from . import herdr as H
from . import kinds as K
from . import locks
from . import policy as P
from .agent import Agent, drop_stale_submissions, drop_submission
from ._rules import AGENT_NAME_RE, DISCUSSION_ID_RE, validate_agent_name, validate_discussion_id
from .errors import (
    AgentNameConflictError,
    AgentNotFoundError,
    AgentTimeoutError,
    HerdrError,
    HerdrUnavailableError,
    InvalidConfigError,
    PolyAgentError,
)
from .kinds import KINDS, is_config_env
from .spec import AgentSpec

log = logging.getLogger("polyagent")
PANE_ENV = {"HISTFILE": "/dev/null"}  # set for the shell of every pane the library creates
T = TypeVar("T")

# ---- server environment (D3) -------------------------------------------------

SERVER_ENV_NAMES = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TERM", "TMPDIR",
        "HERDR_CONFIG_PATH",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "no_proxy", "all_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    }
)
SERVER_ENV_PREFIXES = ("LC_", "XDG_")
# Task 13.11: besides start-up files and node options, what every pane's interactive shell would run or
# expand (prompt hooks and prompts, shell options) and node module resolution (the CLIs' own code).
PASSTHROUGH_FORBIDDEN_NAMES = frozenset({"BASH_ENV", "ENV", "NODE_OPTIONS", "NODE_PATH", "PROMPT_COMMAND", "PS0",
                                         "PS1", "PS4", "SHELLOPTS", "BASHOPTS"}) | H.SESSION_DIR_VARS
PASSTHROUGH_FORBIDDEN_PREFIXES = ("LD_", "HERDR_")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SERVER_PROBE_INTERVAL = 0.2
SERVER_EXIT_GRACE = 3.0  # keep probing this long after our server child exited (lost race)
HINT_LIMIT = 2.0  # upper bound for the `session list` used to build error hints
SERVER_TERM_GRACE = 2.0  # SIGTERM -> SIGKILL for a server child that never came up
SERVER_LOCK_SLACK = 5.0  # extra wait for server.lock beyond the holder's own start timeout


def check_passthrough(names: Iterable[str]) -> tuple[str, ...]:
    """Validate ``server_env_passthrough``; raise ``InvalidConfigError`` for unsafe names."""
    if isinstance(names, str):
        raise InvalidConfigError("server_env_passthrough must be a sequence of variable names, not a string")
    out: list[str] = []
    for name in names:
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise InvalidConfigError(f"invalid environment variable name {name!r}")
        if (
            name in PASSTHROUGH_FORBIDDEN_NAMES
            or name.startswith(PASSTHROUGH_FORBIDDEN_PREFIXES)
            or is_config_env(name)
        ):
            raise InvalidConfigError(f"{name} must not be passed through to the herdr server")
        out.append(name)
    return tuple(out)


def server_env(passthrough: Iterable[str], base: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if base is None else base
    keep = set(passthrough)
    return {
        k: v
        for k, v in src.items()
        if (k in SERVER_ENV_NAMES or k.startswith(SERVER_ENV_PREFIXES) or k in keep) and k not in H.SESSION_DIR_VARS
    }


# ---- ownership marker (D4) -----------------------------------------------------

MARKER_PREFIX = "polyagent"
MARKER_VERSION = "1"
POLICY_CODES = {"read_only": "ro", "full_auto": "fa"}
_POLICY_BY_CODE = {v: k for k, v in POLICY_CODES.items()}
_SID_RE = re.compile(r"(?!-$)[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Marker:
    """``polyagent:1:<name>:<kind>:<ro|fa>:<sid|->:<disc|->:<created_unix>`` stored in the workspace label."""

    name: str
    kind: str
    policy: str
    created: int
    session_id: str | None = None
    discussion_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not AGENT_NAME_RE.fullmatch(self.name):
            raise InvalidConfigError(f"invalid agent name {self.name!r}")
        if self.kind not in KINDS:
            raise InvalidConfigError(f"unknown kind {self.kind!r}")
        if self.policy not in POLICY_CODES:
            raise InvalidConfigError(f"unknown policy {self.policy!r}")
        if self.session_id is not None and not (isinstance(self.session_id, str) and _SID_RE.fullmatch(self.session_id)):
            raise InvalidConfigError(f"invalid session id {self.session_id!r}")
        if self.discussion_id is not None and not (
            isinstance(self.discussion_id, str) and DISCUSSION_ID_RE.fullmatch(self.discussion_id)
        ):
            raise InvalidConfigError(f"invalid discussion id {self.discussion_id!r}")
        if not isinstance(self.created, int) or isinstance(self.created, bool) or self.created < 0:
            raise InvalidConfigError("created must be a non-negative unix timestamp")

    def encode(self) -> str:
        return ":".join(
            [
                MARKER_PREFIX,
                MARKER_VERSION,
                self.name,
                self.kind,
                POLICY_CODES[self.policy],
                self.session_id or "-",
                self.discussion_id or "-",
                str(self.created),
            ]
        )

    @classmethod
    def parse(cls, label: object) -> "Marker | None":
        """Return the marker encoded in ``label``, or ``None`` if it is not exactly a polyagent marker."""
        if not isinstance(label, str):
            return None
        parts = label.split(":")
        if len(parts) != 8 or parts[0] != MARKER_PREFIX or parts[1] != MARKER_VERSION:
            return None
        _, _, name, kind, pol, sid, disc, created = parts
        if pol not in _POLICY_BY_CODE or not created.isdigit() or not created.isascii():
            return None
        try:
            return cls(
                name=name,
                kind=kind,
                policy=_POLICY_BY_CODE[pol],
                created=int(created),
                session_id=None if sid == "-" else sid,
                discussion_id=None if disc == "-" else disc,
            )
        except InvalidConfigError:
            return None


@dataclass(frozen=True)
class OwnedWorkspace:
    workspace_id: str
    marker: Marker
    live: bool


def _live_workspaces(agents: list[dict[str, Any]]) -> dict[str, set[str | None]]:
    """Map live agent name -> workspace ids hosting it (``None`` when herdr omits the id)."""
    live: dict[str, set[str | None]] = {}
    for a in agents:
        name = a.get("name")
        if isinstance(name, str) and name:
            wid = a.get("workspace_id")
            live.setdefault(name, set()).add(wid if isinstance(wid, str) else None)
    return live


def _is_live(live: dict[str, set[str | None]], name: str, workspace_id: str) -> bool:
    where = live.get(name, set())
    return workspace_id in where or None in where


# ---- entry object --------------------------------------------------------------


class PolyAgent:
    """Library entry object bound to one named, non-default herdr session.

    Construction validates the session name and ``server_env_passthrough`` before any herdr
    call, checks the herdr version, then starts a headless server for the session if none runs.
    herdr's ``default`` session is refused. The server is started with a minimal environment
    (``PATH``, ``HOME``, locale, terminal, proxies, ``XDG_*`` and ``HERDR_CONFIG_PATH``) plus the
    names in ``server_env_passthrough``; names that would change an agent's permissions or every pane
    shell (``BASH_ENV``, ``PROMPT_COMMAND``, the CLIs' configuration families, ...) are refused.

    Example::

        from polyagent import PolyAgent, AgentSpec

        pa = PolyAgent(session="my-service")
        agent = pa.start(AgentSpec(kind="claude", policy="read_only", provider=provider), "/srv/repo")
        try:
            print(agent.ask("Summarise README.md in three bullets.", timeout=300))
        finally:
            agent.close()

    ``start`` is safe to call from several threads at once. ``stop_server()`` kills every agent of
    the session, also those of other processes; after a restart the old workspaces come back as
    orphans and ``cleanup()`` closes them.
    """

    def __init__(
        self,
        session: str = "polyagent",
        herdr_path: str | None = None,
        server_start_timeout: float = 15,
        server_env_passthrough: Iterable[str] = (),
    ):
        self._session = H.validate_session_name(session)
        self._passthrough = check_passthrough(server_env_passthrough)
        if (
            isinstance(server_start_timeout, bool)
            or not isinstance(server_start_timeout, (int, float))
            or server_start_timeout <= 0
        ):
            raise InvalidConfigError("server_start_timeout must be a positive number of seconds")
        self._server_start_timeout = float(server_start_timeout)
        locks.lock_dir(self._session)  # D5 prerequisites (passwd entry, writable home) fail early
        binary = H.find_herdr(herdr_path)
        H.check_version(binary)
        self._herdr = H.HerdrClient(self._session, binary)
        self._server_mutex = threading.Lock()
        self._probe_unresponsive = False
        self._refuse_if_marked_default()
        self.ensure_server()

    @property
    def session(self) -> str:
        return self._session

    @property
    def herdr(self) -> H.HerdrClient:
        return self._herdr

    # ---- server (D3) ----------------------------------------------------

    def ensure_server(self) -> None:
        """Start this session's headless server unless it is already running (one server per session)."""
        with self._server_mutex:
            if self._herdr.is_running():
                return
            try:
                lock = locks.acquire_server_lock(self._session, self._server_start_timeout + SERVER_LOCK_SLACK)
            except HerdrUnavailableError:
                # Whoever holds server.lock may just have finished starting it.
                if self._herdr.is_running():
                    return
                raise
            with lock:
                if self._herdr.is_running():
                    return
                self._spawn_server()

    def _spawn_server(self) -> None:
        argv = self._herdr.argv(["server"])
        log.info("starting herdr server for session %s", self._session)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=server_env(self._passthrough),
                cwd=str(locks.user_home()),
                start_new_session=True,
            )
        except OSError as e:
            raise H.launch_error(e, argv) from e
        threading.Thread(target=proc.wait, name=f"herdr-server-reaper-{proc.pid}", daemon=True).start()
        deadline = time.monotonic() + self._server_start_timeout
        exited_at: float | None = None
        self._probe_unresponsive = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(SERVER_PROBE_INTERVAL, remaining))
            if self._probe(max(0.1, min(H.PROBE_LIMIT, deadline - time.monotonic()))):
                return
            if proc.poll() is None:
                continue
            # Our child exited. It may have lost a race against a server started outside polyagent
            # ("already running") that does not answer yet, so keep probing for a short grace period.
            now = time.monotonic()
            if exited_at is None:
                exited_at = now
            if now - exited_at >= SERVER_EXIT_GRACE:
                raise self._start_failed(proc, argv, f"no server answered within {SERVER_EXIT_GRACE:g}s")
        if proc.poll() is not None:  # exited, and the start timeout came before the grace period ended
            raise self._start_failed(proc, argv, f"no server answered within {self._server_start_timeout:g}s")
        # Still running but never served: it would only pile up with every retry (D3).
        _terminate_and_reap(proc)
        raise HerdrUnavailableError(
            H.SERVER_START_TIMEOUT,
            f"herdr server for session {self._session!r} did not come up within "
            f"{self._server_start_timeout:g}s and was terminated; {self._server_log_hint()}",
            argv,
        )

    def _start_failed(self, proc: subprocess.Popen, argv: list[str], why: str) -> HerdrUnavailableError:
        return HerdrUnavailableError(
            H.SERVER_START_FAILED,
            f"herdr server for session {self._session!r} exited with status {proc.returncode} and {why}; "
            f"{self._server_log_hint()}",
            argv,
        )

    def _probe(self, limit: float) -> bool:
        try:
            return self._herdr.is_running(limit=limit)
        except HerdrUnavailableError as e:
            if e.code == H.UNRESPONSIVE:
                self._probe_unresponsive = True
                return False
            raise

    def _server_log_hint(self) -> str:
        generic = "see herdr-server.log in the session directory reported by `herdr session list --json`"
        if self._probe_unresponsive:  # the CLI itself hangs; do not wait on it again
            return generic
        try:
            for s in self._herdr.session_list(limit=HINT_LIMIT):
                if s.get("name") == self._session and isinstance(s.get("session_dir"), str):
                    return f"see {os.path.join(s['session_dir'], 'herdr-server.log')}"
        except PolyAgentError:
            pass
        return generic

    def _with_server(self, fn: Callable[[], T]) -> T:
        """Run an entry operation; on ``server_not_running`` start the server once and retry."""
        try:
            return fn()
        except HerdrUnavailableError as e:
            if e.code != H.SERVER_NOT_RUNNING:
                raise
        self.ensure_server()
        return fn()

    def _session_entries(self) -> list[dict[str, Any]]:
        """This session's entries in ``session list``; refuse unless each is explicitly non-default (D2)."""
        entries = [s for s in self._herdr.session_list() if s.get("name") == self._session]
        if any(s.get("default") is not False for s in entries):
            raise HerdrError(
                H.DEFAULT_SESSION_REFUSED,
                f"herdr reports session {self._session!r} as the default session; refusing to use it",
            )
        return entries

    def _refuse_if_marked_default(self) -> None:
        self._session_entries()

    def stop_server(self) -> None:
        """Stop this session's server; every agent in the session (of any process) is killed."""
        if not self._session_entries():
            return
        self._herdr.session_stop()

    # ---- ownership (D4) -------------------------------------------------

    def _owned(self) -> list[OwnedWorkspace]:
        workspaces = self._herdr.workspace_list()
        live = _live_workspaces(self._herdr.agent_list())
        out = []
        for ws in workspaces:
            marker = Marker.parse(ws.get("label"))
            if marker is None:
                continue
            wid = ws.get("workspace_id")
            if not isinstance(wid, str) or not wid:
                raise HerdrError(H.BAD_RESPONSE, f"workspace without workspace_id: {ws!r}"[:500])
            out.append(OwnedWorkspace(wid, marker, _is_live(live, marker.name, wid)))
        return out

    # ---- agents (D9, D4) -------------------------------------------------------

    def start(
        self,
        spec: AgentSpec,
        cwd: str | os.PathLike[str],
        name: str | None = None,
        start_timeout: float = 60,
        *,
        _discussion_id: str | None = None,
        _default_policy: str = "full_auto",
    ) -> Agent:
        """Start an agent in its own workspace and return its handle once it accepts input (D9)."""
        if not isinstance(spec, AgentSpec):
            raise InvalidConfigError("spec must be an AgentSpec")
        if isinstance(start_timeout, bool) or not isinstance(start_timeout, (int, float)) or not 3 < start_timeout <= 300:
            raise InvalidConfigError("start_timeout must be in (3, 300] seconds")
        if name is not None:
            validate_agent_name(name)
        if _discussion_id is not None:
            validate_discussion_id(_discussion_id)
        # The working directory is resolved once; the checks, the workspace and the log path all use
        # this real path (review N1). The repo-config check also walks the literal path.
        if not os.path.isdir(cwd):
            raise InvalidConfigError(f"working directory {os.fspath(cwd)!r} does not exist")
        real_cwd = os.path.realpath(cwd)
        spec = P.check_static(spec, cwd, _default_policy)
        return self._with_server(lambda: self._start(spec, real_cwd, name, start_timeout, _discussion_id))

    def _start(self, spec: AgentSpec, cwd: str, name: str | None, start_timeout: float,
               discussion_id: str | None) -> Agent:
        live = {a.get("name") for a in self._herdr.agent_list()}
        lock = None
        if name is None:
            for _ in range(8):
                candidate = f"{spec.kind}-{secrets.token_hex(3)}"
                if candidate not in live:
                    lock = locks.try_lock(self._session, candidate)
                    if lock is not None:
                        name = candidate
                        break
            if lock is None:
                raise AgentNameConflictError("could not find a free agent name")
        else:
            if name in live:
                raise AgentNameConflictError(f"agent name {name!r} is used by a live agent in session {self._session!r}")
            lock = locks.acquire_for_start(self._session, name)
        try:
            drop_submission(self._session, name)  # a new agent has submitted nothing (D11)
            for ow in self._owned():  # a same-name orphan (e.g. restored by a server restart), under our lock
                if ow.marker.name == name and not ow.live:
                    self._close_if_marked(ow, require_orphan=True)
            return self._launch(spec, cwd, name, start_timeout, discussion_id)
        finally:
            lock.release()

    def _launch(self, spec: AgentSpec, cwd: str, name: str, start_timeout: float,
                discussion_id: str | None) -> Agent:
        adapter = K.adapter(spec.kind)
        sid = str(uuid.uuid4()) if adapter.session_id_flag else None
        marker = Marker(name=name, kind=spec.kind, policy=spec.policy, created=int(time.time()),  # type: ignore[arg-type]
                        session_id=sid, discussion_id=discussion_id)
        label = marker.encode()
        # The pane's shell writes no history: without this the command lines herdr types (the start
        # command, and the preparation line where there is one) go to the history file of the user who
        # runs the server (D6 "shell 历史", task 13.19). Not a credential, so argv is fine.
        created = self._herdr.workspace_create(cwd, label, {**PANE_ENV, **adapter.pane_env})
        wid = (created.get("workspace") or {}).get("workspace_id")
        pane = (created.get("root_pane") or {}).get("pane_id")
        if not isinstance(wid, str) or not isinstance(pane, str):
            raise HerdrError(H.BAD_RESPONSE, "workspace create returned no workspace or pane id")
        ok = False
        try:
            if _launch.needs_prep(spec, adapter):
                _launch.run_prep(self._herdr, pane, spec, adapter)
            args = P.build_args(spec, cwd, session_id=sid)
            try:
                _launch.agent_start(self._herdr, name, adapter.herdr_kind, pane, int(start_timeout * 1000), args)
            except HerdrError as e:
                if e.code == H.AGENT_NAME_TAKEN:
                    raise AgentNameConflictError(f"agent name {name!r} is already used in session {self._session!r}") from None
                if e.code == H.TIMEOUT:
                    raise AgentTimeoutError(f"agent {name} did not become ready within {start_timeout:g}s") from None
                raise
            _launch.handle_dialogs(self._herdr, name, pane, spec, adapter)
            ok = True
        finally:
            if not ok:
                _launch.start_failed_cleanup(self._herdr, wid)
        log.info("started agent %s (%s, %s) in workspace %s", name, spec.kind, spec.policy, wid)
        return Agent(self._herdr, name=name, kind=spec.kind, policy=spec.policy, workspace_id=wid,  # type: ignore[arg-type]
                     pane_id=pane, label=label, session_id=sid)

    def attach(self, name: str) -> Agent:
        """A handle for a live polyagent agent of this session, rebuilt from its ownership marker."""
        validate_agent_name(name)
        return self._with_server(lambda: self._attach(name))

    def _attach(self, name: str) -> Agent:
        try:
            info = self._herdr.agent_get(name)
        except HerdrError as e:
            if e.code == H.AGENT_NOT_FOUND:
                raise AgentNotFoundError(f"no live agent named {name!r} in session {self._session!r}") from None
            raise
        return self._handle(info, name)

    def _handle(self, info: dict[str, Any], name: str) -> Agent:
        wid, pane = info.get("workspace_id"), info.get("pane_id")
        if not isinstance(wid, str) or not isinstance(pane, str):
            raise AgentNotFoundError(f"agent {name!r} has no workspace")
        try:
            label = self._herdr.workspace_get(wid).get("label")
        except HerdrError as e:
            if e.code == H.WORKSPACE_NOT_FOUND:
                raise AgentNotFoundError(f"agent {name!r} has no workspace") from None
            raise
        marker = Marker.parse(label)
        if marker is None or marker.name != name:
            raise AgentNotFoundError(f"agent {name!r} was not started by polyagent")
        return Agent(self._herdr, name=name, kind=marker.kind, policy=marker.policy, workspace_id=wid, pane_id=pane,
                     label=str(label), session_id=marker.session_id)

    def list(self) -> list[Agent]:
        """Handles of the live agents of this session that polyagent started (marker name == agent name)."""
        return self._with_server(self._list)

    def _list(self) -> list[Agent]:
        labels = {w.get("workspace_id"): w.get("label") for w in self._herdr.workspace_list()}
        out = []
        for a in self._herdr.agent_list():
            name, wid = a.get("name"), a.get("workspace_id")
            marker = Marker.parse(labels.get(wid))
            if not isinstance(name, str) or marker is None or marker.name != name or not isinstance(a.get("pane_id"), str):
                continue
            out.append(Agent(self._herdr, name=name, kind=marker.kind, policy=marker.policy, workspace_id=wid,
                             pane_id=a["pane_id"], label=str(labels[wid]), session_id=marker.session_id))
        return out

    def owned_workspaces(self) -> list[OwnedWorkspace]:
        """Workspaces carrying an intact polyagent marker, with whether their agent is alive."""
        return self._with_server(self._owned)

    def cleanup(self, *, older_than: datetime | float | None = None, discussion_id: str | None = None) -> list[str]:
        """Close polyagent workspaces and return the agent names whose workspace was closed.

        Without arguments: close orphans (the marker's agent is not alive in that workspace),
        skipping any whose agent lock is held (being started or asked); the lock is taken first
        and the orphan state re-checked under it. With ``older_than`` and/or ``discussion_id``:
        close every matching workspace, live or not, without looking at locks. Either way the
        label is re-read right before closing and must still be the same marker.

        If closing some workspace fails, the others are still processed and the first error is
        raised afterwards with the list of closed names attached as ``error.closed``.
        """
        if discussion_id is not None:
            validate_discussion_id(discussion_id)
        cutoff = _to_unix(older_than)
        filtered = cutoff is not None or discussion_id is not None
        drop_stale_submissions(self._session)  # records left by failed or reclaimed asks (D11)
        try:
            return self._cleanup(filtered, cutoff, discussion_id)
        except HerdrUnavailableError as e:
            if e.code != H.SERVER_NOT_RUNNING:
                raise
            first: list[str] = list(getattr(e, "closed", []))
        # The server went away mid-way: restart it and run a second pass; both passes count (D4).
        try:
            self.ensure_server()
            second = self._cleanup(filtered, cutoff, discussion_id)
        except PolyAgentError as e:
            e.closed = _dedupe(first + list(getattr(e, "closed", [])))  # type: ignore[attr-defined]
            raise
        return _dedupe(first + second)

    def _cleanup(self, filtered: bool, cutoff: float | None, discussion_id: str | None) -> list[str]:
        closed: list[str] = []
        errors: list[PolyAgentError] = []
        for ow in self._owned():
            m = ow.marker
            if cutoff is not None and not m.created < cutoff:
                continue
            if discussion_id is not None and m.discussion_id != discussion_id:
                continue
            if not filtered and ow.live:
                continue
            try:
                if filtered:
                    done = self._close_if_marked(ow)
                else:
                    lock = locks.try_lock(self._session, m.name)
                    if lock is None:
                        continue
                    with lock:
                        done = self._close_if_marked(ow, require_orphan=True)
            except HerdrUnavailableError as e:
                if e.code == H.SERVER_NOT_RUNNING:  # nothing else can succeed now; stop and report progress
                    e.closed = _dedupe(closed)  # type: ignore[attr-defined]
                    raise
                log.warning("cleanup: closing workspace %s (agent %s) failed: %s", ow.workspace_id, m.name, e)
                errors.append(e)
                continue
            except PolyAgentError as e:
                log.warning("cleanup: closing workspace %s (agent %s) failed: %s", ow.workspace_id, m.name, e)
                errors.append(e)
                continue
            if done:
                closed.append(m.name)
        if errors:
            first = errors[0]
            first.closed = _dedupe(closed)  # type: ignore[attr-defined]
            raise first
        return _dedupe(closed)

    def _close_if_marked(self, ow: OwnedWorkspace, require_orphan: bool = False) -> bool:
        """Re-read the label (and liveness) right before closing; close only if unchanged."""
        try:
            current = Marker.parse(self._herdr.workspace_get(ow.workspace_id).get("label"))
        except HerdrError as e:
            if e.code == H.WORKSPACE_NOT_FOUND:
                return False
            raise
        if current != ow.marker:
            return False
        if require_orphan and _is_live(_live_workspaces(self._herdr.agent_list()), ow.marker.name, ow.workspace_id):
            return False
        try:
            self._herdr.workspace_close(ow.workspace_id)
        except HerdrError as e:
            if e.code == H.WORKSPACE_NOT_FOUND:
                return False
            raise
        log.info("closed workspace %s of agent %s (session %s)", ow.workspace_id, ow.marker.name, self._session)
        return True


def _dedupe(names: list[str]) -> list[str]:
    """Order-preserving: a name can own several workspaces (restored ones next to a new one)."""
    return list(dict.fromkeys(names))


def _terminate_and_reap(proc: subprocess.Popen) -> None:
    """SIGTERM our own server child, SIGKILL after ``SERVER_TERM_GRACE``; wait until it is reaped."""
    for signal_it in (proc.terminate, proc.kill):
        if proc.poll() is not None:
            return
        try:
            signal_it()
        except OSError:
            pass
        try:
            proc.wait(SERVER_TERM_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue
    log.warning("herdr server child %s did not exit after SIGKILL", proc.pid)


def _to_unix(value: datetime | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise InvalidConfigError("older_than must be a datetime or a unix timestamp")
