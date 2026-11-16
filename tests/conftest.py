"""Shared fixtures: a fake ``herdr`` behind a mocked ``subprocess.Popen``, plus a global safety net."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from polyagent import locks

REAL_WHICH = shutil.which
ORIG_USER_HOME = locks.user_home


@pytest.fixture(autouse=True)
def _never_real_herdr_or_home(request, tmp_path_factory, monkeypatch):
    """Unit tests never find the real herdr on PATH and never touch ~/.local/state (review T2)."""
    if request.node.get_closest_marker("integration"):
        return
    home = tmp_path_factory.mktemp("safe-home")
    monkeypatch.setattr(locks, "user_home", lambda: home)

    def which(cmd, *a, **k):
        return None if os.path.basename(str(cmd)) == "herdr" else REAL_WHICH(cmd, *a, **k)

    monkeypatch.setattr(shutil, "which", which)


@dataclass
class Resp:
    rc: int = 0
    stdout: str = ""
    stderr: str = ""
    hang: bool = False
    raises: BaseException | None = None


def ok(result: dict[str, Any] | None = None, raw: dict[str, Any] | None = None) -> Resp:
    payload = raw if raw is not None else {"id": "cli:x", "result": result or {}}
    return Resp(0, json.dumps(payload))


def err(code: str, message: str = "boom") -> Resp:
    return Resp(1, "", json.dumps({"id": "cli:x", "error": {"code": code, "message": message}}))


class FakeProc:
    """Never a real process: ``pid`` is only an identifier and no signal is ever sent."""

    _next_pid = 40000

    def __init__(self, resp: Resp):
        self.resp = resp
        self.killed = False
        self.returncode: int | None = None
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.timeouts: list[float | None] = []
        self.stdout = None
        self.stderr = None
        self.server = False  # a fake `herdr server`: alive (returncode None) until signalled or exited
        self.ignores_term = False
        self.signals: list[str] = []

    def communicate(self, timeout: float | None = None):
        self.timeouts.append(timeout)
        if self.resp.raises is not None and not self.killed:
            raise self.resp.raises
        if self.resp.hang and not self.killed:
            time.sleep(min(timeout or 0.01, 0.01))
            raise subprocess.TimeoutExpired("herdr", timeout)
        self.returncode = -9 if self.killed else self.resp.rc
        return ("", "") if self.killed else (self.resp.stdout, self.resp.stderr)

    def kill(self):
        self.killed = True
        self.signals.append("KILL")
        if self.server:
            self.returncode = -9

    def terminate(self):
        self.signals.append("TERM")
        if self.server and not self.ignores_term:
            self.returncode = -15

    def poll(self):
        return self.returncode

    def wait(self, timeout: float | None = None):
        if self.server and self.returncode is None and timeout is not None:
            raise subprocess.TimeoutExpired("herdr server", timeout)
        return self.returncode


@dataclass
class FakeHerdr:
    """Records every Popen call. ``handler(argv)`` may return a ``Resp`` to override the default."""

    running: bool = True
    version: str = "herdr 0.9.5"
    server_starts: bool = True
    sessions_default: Any = False
    session_listed: bool = True
    workspaces: list[dict[str, Any]] = field(default_factory=list)
    agents: list[dict[str, Any]] = field(default_factory=list)
    handler: Callable[[list[str]], Resp | None] | None = None
    on_server: Callable[[], None] | None = None
    server_exit: int | None = None  # exit status of a fake server that terminates at once
    server_ignores_term: bool = False
    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    procs: list[FakeProc] = field(default_factory=list)

    @property
    def argvs(self) -> list[list[str]]:
        return [a for a, _ in self.calls]

    def popen(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        resp = self.handler(argv) if self.handler else None
        if resp is None:
            resp = self.default(argv)
        proc = FakeProc(resp)
        if argv[3:] == ["server"]:
            proc.server = True
            proc.ignores_term = self.server_ignores_term
            if self.server_exit is not None:
                proc.returncode = self.server_exit
        self.procs.append(proc)
        return proc

    def default(self, argv: list[str]) -> Resp:
        if argv[1:] == ["--version"]:
            return Resp(0, self.version + "\n")
        assert argv[1] == "--session", argv
        sess, rest = argv[2], argv[3:]
        if rest == ["server"]:
            if self.on_server:
                self.on_server()
            if self.server_starts:
                self.running = True
            return Resp(0, "")
        if rest[:2] == ["session", "list"]:
            entry = {"name": sess, "running": self.running, "session_dir": f"/fake/sessions/{sess}"}
            if self.sessions_default is not None:
                entry["default"] = self.sessions_default
            return ok(raw={"sessions": [entry] if self.session_listed else []})
        if rest[:2] == ["session", "stop"]:
            self.running = False
            return ok(raw={"session": {"name": sess, "default": False}, "stopped": True})
        if not self.running:
            return err("server_not_running", "no herdr server is running")
        if rest[:2] == ["workspace", "list"]:
            return ok({"type": "workspace_list", "workspaces": self.workspaces})
        if rest[:2] == ["workspace", "get"]:
            for w in self.workspaces:
                if w["workspace_id"] == rest[2]:
                    return ok({"type": "workspace_info", "workspace": w})
            return err("workspace_not_found", f"workspace {rest[2]} not found")
        if rest[:2] == ["agent", "list"]:
            return ok({"type": "agent_list", "agents": self.agents})
        if rest[:2] == ["workspace", "close"]:
            wid = rest[2]
            before = len(self.workspaces)
            self.workspaces[:] = [w for w in self.workspaces if w["workspace_id"] != wid]
            if len(self.workspaces) == before:
                return err("workspace_not_found", f"workspace {wid} not found")
            return Resp(0, "")
        return ok({})


@pytest.fixture
def herdr_bin(tmp_path):
    p = tmp_path / "bin" / "herdr"
    p.parent.mkdir(exist_ok=True)
    p.write_text("#!/bin/sh\nexit 99\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def lock_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(locks, "user_home", lambda: home)
    return home


@pytest.fixture
def fake(monkeypatch, herdr_bin, lock_home):
    f = FakeHerdr()
    monkeypatch.setattr(subprocess, "Popen", f.popen)
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: herdr_bin if cmd == "herdr" else REAL_WHICH(cmd, *a, **k))
    return f


def script(tmp_path, body: str, name: str = "herdr") -> str:
    """A throwaway executable shell script standing in for herdr (never the real one)."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)
