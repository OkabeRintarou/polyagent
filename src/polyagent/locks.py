"""Per-agent ``flock`` file locks under ``~/.local/state/polyagent/<session>/`` (design D5).

Layout: ``<dir>/server.lock`` guards server start (D3); ``<dir>/agents/<name>.lock`` guards one
agent; ``<dir>/agents/<name>.submitted`` is the submission record of D11 (a small file, not a lock). Agent locks live in their own subdirectory so no agent name can collide with the
server lock (review A1). Every acquisition opens the lock file anew, so threads of one process,
several handles of the same agent and different processes all exclude each other. Lock files
are never deleted.
"""

from __future__ import annotations

import fcntl
import os
import pwd
import threading
import time
from pathlib import Path
from typing import Protocol

from ._rules import SERVER_LOCK_TIMEOUT, validate_agent_name, validate_session_name
from .errors import (
    AgentBusyError,
    AgentNameConflictError,
    AskCancelledError,
    HerdrUnavailableError,
    InvalidConfigError,
)

SERVER_LOCK_FILE = "server.lock"
AGENT_LOCK_DIR = "agents"
POLL_INTERVAL = 0.05


class Cancel(Protocol):
    def is_set(self) -> bool: ...


def user_home() -> Path:
    """The user's home directory from the password database (not ``$HOME``/``XDG_*``)."""
    uid = os.getuid()
    try:
        return Path(pwd.getpwuid(uid).pw_dir)
    except KeyError:
        raise InvalidConfigError(
            f"uid {uid} has no passwd entry; polyagent needs a home directory for its lock files"
        ) from None


def _private_dir(d: Path) -> Path:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def lock_dir(session: str) -> Path:
    """``<passwd home>/.local/state/polyagent/<session>/`` (0700); ``InvalidConfigError`` if it cannot be used."""
    d = user_home() / ".local" / "state" / "polyagent" / validate_session_name(session)
    try:
        return _private_dir(d)
    except OSError as e:
        raise InvalidConfigError(
            f"cannot create the polyagent lock directory {d} ({e.strerror or e}); "
            "the home directory from the password database must be writable"
        ) from None


class FileLock:
    """An exclusive ``flock`` on ``path``; each instance uses its own open file description.

    Use ``with`` only on an already acquired lock: entering an unheld lock raises ``RuntimeError``.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None
        self._mutex = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        with self._mutex:
            if self._fd is not None:
                raise RuntimeError("lock already held by this FileLock")
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
            return True

    def acquire(self, deadline: float, cancel: Cancel | None = None, poll: float = POLL_INTERVAL) -> bool:
        """Poll until acquired or ``deadline`` (``time.monotonic()``) passes. Not first-come-first-served."""
        while True:
            if cancel is not None and cancel.is_set():
                raise AskCancelledError("cancelled while waiting for the agent lock")
            if self.try_acquire():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))

    def release(self) -> None:
        with self._mutex:
            fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> "FileLock":
        if not self.held:
            raise RuntimeError("FileLock used as a context manager without being acquired")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def agent_lock(session: str, name: str) -> FileLock:
    """An *unacquired* lock object for agent ``name``."""
    sub = lock_dir(session) / AGENT_LOCK_DIR
    try:
        _private_dir(sub)
    except OSError as e:
        raise InvalidConfigError(f"cannot create the agent lock directory {sub} ({e.strerror or e})") from None
    return FileLock(sub / f"{validate_agent_name(name)}.lock")


SUBMISSION_SUFFIX = ".submitted"


def submission_path(session: str, name: str) -> Path:
    """``agents/<name>.submitted``: the D11 submission record of agent ``name`` (not a lock)."""
    sub = lock_dir(session) / AGENT_LOCK_DIR
    try:
        _private_dir(sub)
    except OSError as e:
        raise InvalidConfigError(f"cannot create the agent lock directory {sub} ({e.strerror or e})") from None
    return sub / f"{validate_agent_name(name)}{SUBMISSION_SUFFIX}"


def server_lock(session: str) -> FileLock:
    """An *unacquired* lock object guarding server start."""
    return FileLock(lock_dir(session) / SERVER_LOCK_FILE)


def acquire_for_ask(session: str, name: str, deadline: float, cancel: Cancel | None = None) -> FileLock:
    """Ask lock: poll until ``deadline``; raise ``AgentBusyError`` when it does not come."""
    lock = agent_lock(session, name)
    if not lock.acquire(deadline, cancel):
        raise AgentBusyError(f"agent {name} is busy with another prompt")
    return lock


def acquire_for_start(session: str, name: str) -> FileLock:
    """Start lock: a single non-blocking attempt; raise ``AgentNameConflictError`` when it is held."""
    lock = agent_lock(session, name)
    if not lock.try_acquire():
        raise AgentNameConflictError(f"agent {name} is being started or used by another caller")
    return lock


def try_lock(session: str, name: str) -> FileLock | None:
    lock = agent_lock(session, name)
    return lock if lock.try_acquire() else None


def acquire_server_lock(session: str, timeout: float) -> FileLock:
    lock = server_lock(session)
    if not lock.acquire(time.monotonic() + timeout):
        raise HerdrUnavailableError(
            SERVER_LOCK_TIMEOUT, f"could not lock server start for herdr session {session!r} within {timeout:g}s"
        )
    return lock
