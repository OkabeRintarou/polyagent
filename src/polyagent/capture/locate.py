"""Where the session logs are (D12): config directory from the agent process's environment, the
per-kind paths, and the codex rollout file held open by the agent process."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

ROLLOUT_RE = re.compile(r"rollout-.*\.jsonl$")


def read_environ(pid: int) -> bytes | None:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f.read()
    except OSError:
        return None


def env_value(environ: bytes | None, name: str) -> str | None:
    """Value of ``name`` in a ``/proc/<pid>/environ`` blob (NUL-separated ``K=V``)."""
    if not environ:
        return None
    key = name.encode() + b"="
    for item in environ.split(b"\0"):
        if item.startswith(key):
            return item[len(key):].decode("utf-8", "surrogateescape") or None
    return None


def config_dir(environ: bytes | None, var: str, home: Path, default_rel: str) -> Path:
    """The agent's configuration directory: ``var`` from its own environment, else ``home/default_rel``."""
    value = env_value(environ, var)
    return Path(value) if value else home / default_rel


def copilot_log_path(config: Path, session_id: str) -> Path:
    return config / "session-state" / session_id / "events.jsonl"


def find_claude_log(config: Path, session_id: str) -> str | None:
    """The transcript ``<config>/projects/*/<session_id>.jsonl``, whatever the project directory's name.
    The directory encodes the working directory, but how spaces, ``@`` or non-ASCII characters are
    encoded is not known, so the library never computes it (D12); the session id is a library-made
    UUID and does not repeat."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        return None
    try:
        projects = os.scandir(config / "projects")
    except OSError:
        return None
    with projects:
        for d in projects:
            candidate = os.path.join(d.path, f"{session_id}.jsonl")
            if d.is_dir(follow_symlinks=False) and os.path.isfile(candidate):
                return candidate
    return None


def open_rollouts(pids: Iterable[int], root: str = "/proc") -> list[str]:
    """codex rollout files held open by any of ``pids`` (D12: needs `--no-daemon`)."""
    found: list[str] = []
    for pid in pids:
        fd_dir = os.path.join(root, str(pid), "fd")
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in names:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if ROLLOUT_RE.search(os.path.basename(target)) and target not in found:
                found.append(target)
    return found


def process_start(pid: int, root: str = "/proc") -> int | None:
    """Start time of ``pid`` (clock ticks since boot, ``stat`` field 22): with the pid it identifies the
    process, so a reused pid is told apart (D12 "提交后再绑定"). ``None`` when it cannot be read."""
    try:
        with open(os.path.join(root, str(pid), "stat"), "rb") as f:
            data = f.read()
        fields = data[data.rindex(b")") + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None
