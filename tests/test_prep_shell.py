"""Review T2: the D6 preparation script executed by real POSIX shells (no herdr, no agent).

Each test sources the generated script the way the pane does (`. file`), with inherited variables
in the environment, then reads the resulting environment with `env -0`.
"""

import os
import shutil
import subprocess

import pytest

from polyagent import _launch as L
from polyagent import kinds as K
from polyagent.spec import AgentSpec, ClaudeProvider

TOKEN = "fake-token-0123456789"
TRICKY = "it's $(id) `x` \"q\" $HOME\nline2"
INHERITED = {"ANTHROPIC_CUSTOM_HEADERS": "X-Old: old", "AWS_PROFILE": "old", "CLAUDE_CODE_USE_BEDROCK": "1",
             "GOOGLE_APPLICATION_CREDENTIALS": "/old", "CLOUD_ML_REGION": "old", "ANTHROPIC_MODEL": "old-model",
             "KEEP_ME": "kept", "COPILOT_ALLOW_ALL": "true"}
SHELLS = [s for s in sorted(L.POSIX_SHELLS) if shutil.which(s)]  # the accepted shells present on this host


def fake_bin(tmp_path):
    """Stand-ins for the agents' executables, so that `command -v` resolves them to a path (D6 re-check)."""
    d = tmp_path / "bin"
    if not d.exists():
        d.mkdir()
        for name in {a.executable for a in K.ADAPTERS.values()}:
            (d / name).write_text("#!/bin/sh\nexit 0\n")
            (d / name).chmod(0o755)
    return d


def run(shell, script, tmp_path, preamble="", with_agents=True):
    f = tmp_path / "prep.sh"
    f.write_text(script)
    path = os.pathsep.join(([str(fake_bin(tmp_path))] if with_agents else []) + [os.path.dirname(shutil.which(shell)),
                                                                               "/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(tmp_path), **INHERITED}
    code = f"{preamble}\n. {f} && echo __OK__ || echo __FAIL__\ncommand env -0\n"
    out = subprocess.run([shutil.which(shell), "-c", code], env=env, capture_output=True, timeout=30)
    head, _, rest = out.stdout.partition(b"\n")
    got = dict(item.split(b"=", 1) for item in rest.split(b"\0") if b"=" in item)
    return head.decode(), {k.decode(): v.decode() for k, v in got.items()}, out.stdout.decode(errors="replace"), \
        out.stderr.decode(errors="replace")


def claude_spec(policy="read_only", **kw):
    prov = ClaudeProvider.compatible("https://llm.example.com", auth_token=TOKEN, custom_headers={"X-New": "new value"})
    return AgentSpec(kind="claude", policy=policy, provider=prov, env={"MY_VAR": TRICKY}, **kw).resolve("full_auto")


@pytest.mark.parametrize("shell", SHELLS)
def test_provider_families_are_cleared_and_values_exported_verbatim(shell, tmp_path):
    head, env, _, err = run(shell, L.prep_script(claude_spec(), K.adapter("claude")), tmp_path)
    assert head == "__OK__", err
    assert env["MY_VAR"] == TRICKY and env["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-New: new value" and env["KEEP_ME"] == "kept"
    for gone in ("AWS_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION",
                 "ANTHROPIC_MODEL"):
        assert gone not in env, gone
    assert "__pa_v" not in env


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("preamble", [
    "shopt -s expand_aliases; alias grep='grep --color=always'",
    "shopt -s expand_aliases; alias env='env -u NOTHING'; alias sed='sed --debug'",
    "grep() { echo CLAUDE_CODE_USE_BEDROCK_X; }; env() { :; }; unset() { :; }; export() { :; }",
])
def test_aliases_and_functions_do_not_break_the_script(tmp_path, preamble):
    """Review C4: the pane shell is interactive; aliases and functions must not change the commands."""
    head, env, _, err = run("bash", L.prep_script(claude_spec("full_auto"), K.adapter("claude")), tmp_path, preamble)
    assert head == "__OK__", err
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and env["ANTHROPIC_CUSTOM_HEADERS"] == "X-New: new value"
    assert env["ANTHROPIC_AUTH_TOKEN"] == TOKEN


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("flags", ["set -x", "set -v", "set -xv"])
def test_tracing_never_shows_the_values(shell, tmp_path, flags):
    """Review C4: with xtrace/verbose on in the user's shell, nothing of the values reaches the terminal."""
    head, env, out, err = run(shell, L.prep_script(claude_spec(), K.adapter("claude")), tmp_path, flags)
    assert head.endswith("__OK__") or "__OK__" in out, err
    assert TOKEN not in err and "new value" not in err and "line2" not in err


@pytest.mark.parametrize("shell", SHELLS)
def test_a_variable_that_cannot_be_cleared_fails_the_script(shell, tmp_path):
    """Review C4: the re-check after the exports catches what `unset` could not remove."""
    head, _, out, err = run(shell, L.prep_script(claude_spec("full_auto"), K.adapter("claude")), tmp_path,
                            "readonly CLAUDE_CODE_USE_BEDROCK")
    assert head == "__pa_reason_provider_env_2" and "__FAIL__" in out, (out, err)


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_read_only_removes_aliases_functions_and_permission_switches(tmp_path):
    spec = AgentSpec(kind="copilot", policy="read_only").resolve("full_auto")
    pre = "copilot() { :; }\nshopt -s expand_aliases; alias copilot='copilot --yolo'"
    script = L.prep_script(spec, K.adapter("copilot"))
    head, env, out, err = run("bash", script, tmp_path, pre)
    assert head == "__OK__", err
    assert "COPILOT_ALLOW_ALL" not in env
    f = tmp_path / "check.sh"
    f.write_text(script)
    probe = subprocess.run(["bash", "-c", f"{pre}\n. {f}\nalias copilot || echo NOALIAS; "
                                          "type -t copilot || echo NOFUNC"],
                           env={"PATH": f"{fake_bin(tmp_path)}:/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
    assert "NOALIAS" in probe.stdout and "function" not in probe.stdout


RO_COPILOT = AgentSpec(kind="copilot", policy="read_only").resolve("full_auto")


@pytest.mark.parametrize("shell", SHELLS)
def test_permission_switch_that_cannot_be_unset_fails_the_script(shell, tmp_path):
    """D6 re-check `permission_env`: a read-only COPILOT_ALLOW_ALL survives `unset`."""
    head, _, out, err = run(shell, L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path,
                            "readonly COPILOT_ALLOW_ALL")
    assert head == "__pa_reason_permission_env_2" and "__FAIL__" in out, (out, err)


@pytest.mark.parametrize("shell", SHELLS)
def test_permission_switch_set_but_empty_is_still_set(shell, tmp_path):
    head, _, out, err = run(shell, L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path,
                            "COPILOT_ASSISTED_APPROVAL=; readonly COPILOT_ASSISTED_APPROVAL")
    assert head == "__pa_reason_permission_env_2", (out, err)


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_function_that_cannot_be_removed_fails_the_script(tmp_path):
    """D6 re-check `executable_shadowed`: a read-only function survives `unset -f`."""
    head, _, out, err = run("bash", L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path,
                            "copilot() { command copilot --yolo \"$@\"; }; readonly -f copilot")
    assert head == "__pa_reason_executable_shadowed_2" and "__FAIL__" in out, (out, err)


@pytest.mark.parametrize("shell", SHELLS)
def test_executable_that_resolves_to_nothing_fails_the_script(shell, tmp_path):
    head, _, out, err = run(shell, L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path, with_agents=False)
    assert head == "__pa_reason_executable_shadowed_2", (out, err)


@pytest.mark.parametrize("shell", SHELLS)
def test_read_only_rechecks_pass_in_a_clean_shell(shell, tmp_path):
    for kind in ("copilot", "claude"):
        spec = claude_spec() if kind == "claude" else RO_COPILOT
        head, env, out, err = run(shell, L.prep_script(spec, K.adapter(kind)), tmp_path)
        assert head == "__OK__", (kind, out, err)
        assert ("COPILOT_ALLOW_ALL" in env) == (kind == "claude")  # only the kind's own switches are cleared


@pytest.mark.parametrize("shell", SHELLS)
def test_read_only_provider_variable_keeps_its_old_value_fails_the_script(shell, tmp_path):
    """Review N3 / D6 `export_failed`: an allowed name that could not be set (read-only, old endpoint kept)."""
    head, env, out, err = run(shell, L.prep_script(claude_spec("full_auto"), K.adapter("claude")), tmp_path,
                              "ANTHROPIC_BASE_URL=https://old.example.com; export ANTHROPIC_BASE_URL; "
                              "readonly ANTHROPIC_BASE_URL")
    assert head == "__pa_reason_export_failed_2" and "__FAIL__" in out, (out, err)


@pytest.mark.parametrize("shell", SHELLS)
def test_caller_variable_that_cannot_be_exported_fails_the_script(shell, tmp_path):
    """Review N3 / D6 `export_failed`: the same for a caller's variable."""
    head, _, out, err = run(shell, L.prep_script(claude_spec("full_auto"), K.adapter("claude")), tmp_path,
                            "MY_VAR=old; readonly MY_VAR")
    assert head == "__pa_reason_export_failed_2" and "__FAIL__" in out, (out, err)
