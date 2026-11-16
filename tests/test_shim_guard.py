"""Review T1: the integration shim refuses every call not scoped to the temp session.

Runs without a real herdr: the shim's "real" binary is a script that only records it was reached.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from conftest import script
from integration.shim import build_shim, is_scoped, read_records

NAME = "polyagent-test-guard01"


@pytest.fixture
def guard(tmp_path):
    reached = tmp_path / "reached.log"
    real = script(tmp_path, f"printf '%s\\n' \"$*\" >> {reached}\necho '{{\"result\":{{}}}}'\n", name="real-herdr")
    shim = build_shim(tmp_path / "shim", real, NAME, tmp_path / "calls.log", tmp_path / "refused.log")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env["PATH"] = f"{shim.parent}{os.pathsep}{env.get('PATH', '')}"

    def run(*args, extra_env=None, via_path=False):
        e = dict(env, **(extra_env or {}))
        argv = ["herdr", *args] if via_path else [str(shim), *args]
        return subprocess.run(argv, capture_output=True, text=True, env=e, timeout=10)

    def reached_lines():
        return reached.read_text().splitlines() if reached.exists() else []

    return run, reached_lines, tmp_path


ALLOWED = [
    ["--version"],
    ["--session", NAME, "workspace", "list"],
    ["--session", NAME, "session", "list", "--json"],
    ["--session", NAME, "session", "stop", NAME, "--json"],
    ["--session", NAME, "session", "delete", NAME],
    ["--session", NAME, "agent", "start", "a", "--kind", "codex", "--pane", "w1:p1", "--", "--session", "x"],
    ["--session", NAME, "agent", "prompt", "a", "line one\nline two --session racer", "--wait"],
]

REFUSED = [
    ["--session", NAME, "session", "stop", "racer", "--json"],
    ["--session", NAME, "session", "delete", "default", "--json"],
    ["--session", NAME, "session", "stop", NAME, "--json", "racer"],
    ["--session", NAME, "session", "attach", NAME],
    ["--session", "racer", "workspace", "list"],
    ["--session", "default", "workspace", "list"],
    [f"--session={NAME}", "workspace", "list"],
    ["workspace", "list"],
    ["session", "stop", "racer"],
    ["--session", NAME, "workspace", "list", "--session", "racer"],
    ["--session", NAME, "workspace", "list", "--session=racer"],
    ["--version", "--session", "racer"],
]


@pytest.mark.parametrize("argv", ALLOWED, ids=lambda a: " ".join(a)[:60])
def test_allowed_calls_reach_real_herdr(guard, argv):
    run, reached, tmp = guard
    assert run(*argv).returncode == 0
    assert len(reached()) >= 1
    assert is_scoped(argv, NAME)
    assert read_records(tmp / "calls.log") == [argv]
    assert read_records(tmp / "refused.log") == []


@pytest.mark.parametrize("argv", REFUSED, ids=lambda a: " ".join(a)[:60])
def test_unscoped_calls_are_refused_before_real_herdr(guard, argv):
    run, reached, tmp = guard
    r = run(*argv)
    assert r.returncode != 0 and "refused_by_test_shim" in r.stderr
    assert reached() == []
    assert not is_scoped(argv, NAME)
    assert len(read_records(tmp / "refused.log")) == 1


def test_inherited_herdr_context_is_refused(guard):
    run, reached, _ = guard
    r = run("--session", NAME, "workspace", "list", extra_env={"HERDR_SOCKET_PATH": "/x/herdr.sock"})
    assert r.returncode != 0 and "inherited HERDR_SOCKET_PATH" in r.stderr
    assert reached() == []
    assert run("--session", NAME, "workspace", "list", extra_env={"HERDR_CONFIG_PATH": "/x"}).returncode == 0


def test_bare_herdr_on_path_hits_the_shim(guard):
    run, reached, _ = guard
    r = run("workspace", "list", via_path=True)
    assert r.returncode != 0 and "refused_by_test_shim" in r.stderr
    assert reached() == []
    assert run("--session", NAME, "workspace", "list", via_path=True).returncode == 0
