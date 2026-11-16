"""Integration fixtures (design D17): a random temporary herdr session per test.

Every herdr invocation from the library goes through a guarding shim (``shim.py``) that is first on
``PATH``. Teardown runs ``session stop`` + ``session delete`` and audits the recorded calls.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from polyagent import locks

from .shim import build_shim, is_scoped, read_records

HERE = Path(__file__).parent


def _enabled() -> bool:
    return os.environ.get("POLYAGENT_INTEGRATION") == "1"


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="set POLYAGENT_INTEGRATION=1 to run integration tests")
    for item in items:
        if HERE not in Path(str(item.path)).parents:
            continue
        item.add_marker(pytest.mark.integration)
        if not _enabled():
            item.add_marker(skip)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if (not k.startswith("HERDR_") or k == "HERDR_CONFIG_PATH") and k != "XDG_CONFIG_HOME"}


def _real_herdr() -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "herdr")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return os.path.abspath(p)
    return None


@dataclass
class TempSession:
    name: str
    real_herdr: str
    shim: str
    log: Path
    refused: Path
    expect_refusals: bool = False
    extra: dict = field(default_factory=dict)

    def calls(self) -> list[list[str]]:
        return read_records(self.log)

    def refusals(self) -> list[list[str]]:
        return read_records(self.refused)

    def herdr(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        """Fixture-side herdr call: real binary, clean env, always ``--session <temp>``."""
        argv = [self.real_herdr, "--session", self.name, *args]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_clean_env(),
                              stdin=subprocess.DEVNULL)

    def server_pids(self) -> list[int]:
        """PIDs whose cmdline is exactly ``<…/herdr> --session <temp> server``."""
        want = ["--session", self.name, "server"]
        pids = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                argv = (d / "cmdline").read_bytes().split(b"\0")[:-1]
            except OSError:
                continue
            if argv and argv[0].endswith(b"herdr") and [a.decode(errors="replace") for a in argv[1:]] == want:
                pids.append(int(d.name))
        return pids


def _session_names(real: str) -> set[str]:
    out = subprocess.run([real, "session", "list", "--json"], capture_output=True, text=True, timeout=30,
                         env=_clean_env(), stdin=subprocess.DEVNULL)
    return {s["name"] for s in json.loads(out.stdout)["sessions"]}


@pytest.fixture
def temp_session(tmp_path, monkeypatch):
    if not _enabled():
        pytest.skip("set POLYAGENT_INTEGRATION=1 to run integration tests")
    real = _real_herdr()
    if not real:
        pytest.skip("herdr is not installed")
    name = f"polyagent-test-{secrets.token_hex(4)}"
    before = _session_names(real)
    assert name not in before
    log, refused = tmp_path / "herdr-argv.log", tmp_path / "herdr-refused.log"
    shim = build_shim(tmp_path / "shim", real, name, log, refused)
    # Anything that resolves `herdr` via PATH (library or child processes) hits the shim.
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    ts = TempSession(name, real, str(shim), log, refused)
    try:
        yield ts
    finally:
        ts.herdr("session", "stop", name, "--json")
        deadline = time.monotonic() + 5
        while ts.server_pids() and time.monotonic() < deadline:  # let it exit on its own first
            time.sleep(0.1)
        for pid in ts.server_pids():  # fallback; cmdline verified by server_pids()
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while ts.server_pids() and time.monotonic() < deadline:
            time.sleep(0.1)
        listed = name in _session_names(real)
        deleted = ts.herdr("session", "delete", name, "--json") if listed else None
        lockdir = locks.user_home() / ".local" / "state" / "polyagent" / name
        shutil.rmtree(lockdir, ignore_errors=True)
        try:
            lockdir.parent.rmdir()
        except OSError:
            pass
        after = _session_names(real)
        assert ts.server_pids() == []
        if deleted is not None:
            assert deleted.returncode == 0, deleted.stderr
        assert after == before, f"session list changed: {sorted(after ^ before)}"
        bad = [c for c in ts.calls() if not is_scoped(c, name)]
        assert bad == [], f"herdr calls not scoped to {name}: {bad}"
        if not ts.expect_refusals:
            assert ts.refusals() == [], f"shim refused calls: {ts.refusals()}"
