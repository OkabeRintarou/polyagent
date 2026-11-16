"""In-memory stand-in for ``HerdrClient`` used by the agent-lifecycle and ask tests (tasks 6.x, 7.x).

It never starts a process. Behaviour is scripted per test: ``on_prompt`` decides what a submitted
prompt does (append to a session log, change the status, raise a herdr error ...), ``screens`` feed
`pane read --source visible`, and every call is recorded in ``calls``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from polyagent import herdr as H
from polyagent.errors import AskCancelledError, HerdrError


def herr(code: str, message: str = "boom") -> HerdrError:
    return H.make_error(code, message, ["herdr", "--session", "polyagent", "x"])


@dataclass
class FakeClient:
    session: str = "polyagent"
    workspaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    shell: str = "bash"
    prep: str = "ok"  # ok | fail | timeout | reason:<code>
    prep_scripts: list[str] = field(default_factory=list)
    start_result: str = "ready"  # ready | not_ready | timeout | name_taken | error
    start_status: str = "idle"
    screens: list[str] = field(default_factory=list)  # visible screens, consumed one per read (last one sticks)
    recent: str = ""  # `read --source recent*` text
    on_prompt: Callable[["FakeClient", str, str, bool], Any] | None = None
    on_keys: Callable[["FakeClient", str, tuple[str, ...]], None] | None = None
    statuses: dict[str, list[str]] = field(default_factory=dict)  # name -> scripted agent_get statuses
    wait_result: Callable[["FakeClient", str, int, tuple[str, ...]], Any] | None = None
    # herdr 0.9.5 prints `agent explain` as text (tests/fixtures/herdr/README.md, task 13.13)
    explain: dict[str, Any] = field(default_factory=lambda: {"explanation": "agent: claude\nstate: working\n"
                                                                            "evidence: \"screen text\""})
    lock: threading.Lock = field(default_factory=threading.Lock)
    _n: int = 0

    # ---- recording --------------------------------------------------------------
    def _rec(self, method: str, *args: Any) -> None:
        with self.lock:
            self.calls.append((method, args))

    def names(self) -> list[str]:
        return [m for m, _ in self.calls]

    def args_of(self, method: str) -> list[tuple[Any, ...]]:
        return [a for m, a in self.calls if m == method]

    # ---- workspaces --------------------------------------------------------------
    def workspace_create(self, cwd: str, label: str) -> dict[str, Any]:
        self._rec("workspace_create", cwd, label)
        self._n += 1
        wid = f"w{self._n}"
        self.workspaces[wid] = {"workspace_id": wid, "label": label, "cwd": cwd}
        # Shape recorded in tests/fixtures/herdr/README.md (task 13.13).
        return {"type": "workspace_created", "workspace": dict(self.workspaces[wid]),
                "root_pane": {"pane_id": f"{wid}:p1", "workspace_id": wid, "cwd": cwd, "agent_status": "unknown"}}

    def workspace_get(self, wid: str) -> dict[str, Any]:
        self._rec("workspace_get", wid)
        if wid not in self.workspaces:
            raise herr(H.WORKSPACE_NOT_FOUND)
        return dict(self.workspaces[wid])

    def workspace_close(self, wid: str) -> None:
        self._rec("workspace_close", wid)
        if self.workspaces.pop(wid, None) is None:
            raise herr(H.WORKSPACE_NOT_FOUND)
        for name in [n for n, a in self.agents.items() if a["workspace_id"] == wid]:
            del self.agents[name]

    def workspace_list(self) -> list[dict[str, Any]]:
        self._rec("workspace_list")
        return [dict(w) for w in self.workspaces.values()]

    # ---- panes ---------------------------------------------------------------------
    def pane_process_info(self, pane: str) -> dict[str, Any]:
        self._rec("pane_process_info", pane)
        return {"process_info": {"pane_id": pane, "shell_pid": 101, "foreground_process_group_id": 101,
                                 "foreground_processes": [{"pid": 101, "name": self.shell, "argv": [self.shell],
                                                           "cmdline": self.shell, "cwd": "/tmp/x"}]}}

    def pane_run(self, pane: str, command: str) -> None:
        self._rec("pane_run", pane, command)
        m = re.search(r"(?:^|; )\. (\S+?);", command)
        if m:
            path = m.group(1).strip("'")
            with open(path) as f:
                self.prep_scripts.append(f.read())
            if self.prep == "ok":
                import os

                os.unlink(path)

    def pane_wait_output(self, pane: str, timeout_ms: int, match=None, regex=None, source=None, cancel=None):
        self._rec("pane_wait_output", pane, timeout_ms, regex)
        if self.prep == "timeout":
            raise herr(H.TIMEOUT, "timed out")
        return {"matched": True}

    def pane_read(self, pane: str, source: str = "recent-unwrapped", lines: int | None = None) -> str:
        self._rec("pane_read", pane, source)
        if source == "recent":  # the prep sentinel read
            if self.prep == "ok":
                return "$ . script\n__pa_env_ok_2\n$ "
            if self.prep.startswith("reason:"):
                return f"__pa_reason_{self.prep.split(':', 1)[1]}_2\n__pa_env_fail_2\n"
            return "__pa_env_fail_2\n"
        return self._visible()

    def _visible(self) -> str:
        """The current visible screen; it changes when keys are sent (a dialog stays until answered)."""
        return self.screens[0] if self.screens else "$ "

    # ---- agents ---------------------------------------------------------------------
    def agent_list(self) -> list[dict[str, Any]]:
        self._rec("agent_list")
        return [dict(a) for a in self.agents.values()]

    def add_agent(self, name: str, wid: str, status: str = "idle", cwd: str = "/tmp/x") -> None:
        self.agents[name] = {"name": name, "workspace_id": wid, "pane_id": f"{wid}:p1", "agent_status": status,
                             "state_change_seq": 1, "cwd": cwd}

    def agent_get(self, name: str) -> dict[str, Any]:
        self._rec("agent_get", name)
        if name not in self.agents:
            raise herr(H.AGENT_NOT_FOUND, f"agent target {name} not found")
        seq = self.statuses.get(name)
        if seq:
            self.agents[name]["agent_status"] = seq.pop(0) if len(seq) > 1 else seq[0]
        return dict(self.agents[name])

    def set_status(self, name: str, status: str) -> None:
        self.statuses.pop(name, None)
        self.agents[name]["agent_status"] = status
        self.agents[name]["state_change_seq"] += 1

    def agent_explain(self, name: str) -> dict[str, Any]:
        self._rec("agent_explain", name)
        return dict(self.explain)

    def agent_start(self, name, kind, pane, timeout_ms, args=None, cancel=None):
        self._rec("agent_start", name, kind, pane, timeout_ms, list(args or []))
        if self.start_result == "name_taken":
            raise herr(H.AGENT_NAME_TAKEN, f"agent name {name} is already used")
        if self.start_result == "timeout":
            raise herr(H.TIMEOUT, "agent did not become ready")
        if self.start_result == "error":
            raise herr("pane_busy", "nope")
        wid = pane.split(":")[0]
        self.add_agent(name, wid, self.start_status, self.workspaces.get(wid, {}).get("cwd", "/tmp/x"))
        if self.start_result == "not_ready":
            raise herr(H.AGENT_NOT_READY, "not ready")
        return dict(self.agents[name])

    def agent_prompt(self, name, text, timeout_ms=None, wait=True, cancel=None):
        self._rec("agent_prompt", name, text, timeout_ms, wait)
        if name not in self.agents:
            raise herr(H.AGENT_NOT_FOUND)
        if self.agents[name]["agent_status"] == "blocked":  # like herdr: nothing is sent (review T1)
            raise herr(H.AGENT_BLOCKED, f"agent {name} is blocked")
        if self.on_prompt is not None:
            out = self.on_prompt(self, name, text, wait)
            if isinstance(out, HerdrError):
                raise out
        return dict(self.agents[name])

    def agent_wait(self, name, timeout_ms, until=(), cancel=None):
        self._rec("agent_wait", name, timeout_ms, tuple(until))
        if name not in self.agents:
            raise herr(H.AGENT_NOT_FOUND)
        if self.wait_result is not None:
            out = self.wait_result(self, name, timeout_ms, tuple(until))
            if isinstance(out, HerdrError):
                raise out
            if out is not None:
                return out
        if cancel is not None and cancel.is_set():
            raise AskCancelledError("cancelled")
        return dict(self.agents[name])

    def agent_read(self, name, source="recent-unwrapped", lines=None):
        self._rec("agent_read", name, source, lines)
        if name not in self.agents:
            raise herr(H.AGENT_NOT_FOUND)
        return self._visible() if source == "visible" else self.recent

    def agent_send_keys(self, name, *keys):
        self._rec("agent_send_keys", name, *keys)
        if name not in self.agents:
            raise herr(H.AGENT_NOT_FOUND)
        if self.on_keys is not None:
            self.on_keys(self, name, keys)
        elif len(self.screens) > 1:
            self.screens.pop(0)


def append_jsonl(path, *entries: dict[str, Any]) -> None:
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def iso(ts: float | None = None) -> str:
    t = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"
