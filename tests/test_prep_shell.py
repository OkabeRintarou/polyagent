"""Review T2: the D6 preparation script executed by real POSIX shells (no herdr, no agent).

Each test runs the library's own pane command line (`_launch.prep_command`) on the generated script,
with inherited variables in the environment, then reads the resulting environment with `env -0`.
"""

import os
import shlex
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
if shutil.which("bash"):
    SHELLS.append("bash-posix")  # bash in POSIX mode prints traps differently (D6, phase-5 design notes)


def argv(shell):
    return ["bash", "--posix"] if shell == "bash-posix" else [shell]


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
    exe = shutil.which(argv(shell)[0])
    path = os.pathsep.join(([str(fake_bin(tmp_path))] if with_agents else []) + [os.path.dirname(exe), "/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(tmp_path), **INHERITED}
    code = f"{preamble}\n{L.prep_command(str(f))}\ncommand env -0\n"
    out = subprocess.run([exe, *argv(shell)[1:], "-c", code], env=env, capture_output=True, timeout=30)
    rest, marks = out.stdout, []
    while True:  # the sentinel and reason lines come first, then the environment
        line, sep, tail = rest.partition(b"\n")
        if not (sep and line.startswith(b"__pa_")):
            break
        marks.append(line.decode())
        rest = tail
    ok = "__pa_env_ok_2" in marks
    reason = next((m for m in marks if m.startswith("__pa_reason_")), None)
    head = reason or ("__OK__" if ok else "__FAIL__")
    got = dict(item.split(b"=", 1) for item in rest.split(b"\0") if b"=" in item)
    text = "\n".join(marks + ["__OK__" if ok else "__FAIL__"])
    return head, {k.decode(): v.decode() for k, v in got.items()}, text, out.stderr.decode(errors="replace")


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


HOOK = "alias copilot='copilot --yolo'; export COPILOT_ALLOW_ALL=true"


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("posix", [False, True])
@pytest.mark.parametrize("hooks", [
    f"PROMPT_COMMAND={shlex.quote(HOOK)}",
    f"PROMPT_COMMAND=(true {shlex.quote(HOOK)})",  # bash 5.1+ array form
    "set -a; PS1='${COPILOT_ALLOW_ALL:=true}'",  # allexport plus an assigning prompt
    f"trap {shlex.quote(HOOK)} DEBUG",
    f"trap {shlex.quote(HOOK)} DEBUG; trap() {{ :; }}",  # a function named `trap` cannot keep it (review C2)
    f"trap {shlex.quote(HOOK)} RETURN",  # runs when the sourced script ends, after its re-checks
    "PS0='$(touch ps0-ran)'",
])
def test_shell_hooks_are_removed(tmp_path, hooks, posix):
    """Phase-5 findings P5-3 / review C2: hooks bash runs between the script and the `agent start` command
    line (PROMPT_COMMAND before the next prompt, DEBUG before each command, RETURN when the sourced file
    ends) are removed; the library's own command line is used, and the next command line is plain."""
    if posix and "trap()" in hooks:
        pytest.skip("POSIX-mode bash refuses a function named `trap` (special builtin)")
    f = tmp_path / "check.sh"
    f.write_text(L.prep_script(RO_COPILOT, K.adapter("copilot")))
    probe = subprocess.run(
        ["bash", *(["--posix"] if posix else []), "-c", f"shopt -s expand_aliases; {hooks}\n{L.prep_command(str(f))}\n"
                       'echo "PC=[${PROMPT_COMMAND-unset}] PS0=[$PS0] ALLOW=[${COPILOT_ALLOW_ALL-unset}] FLAGS=[$-]"; '
                       "builtin trap -p DEBUG RETURN; alias copilot 2>/dev/null || echo NOALIAS"],
        env={"PATH": f"{fake_bin(tmp_path)}:/usr/bin:/bin"}, capture_output=True, text=True, timeout=30)
    assert "__pa_env_ok_2" in probe.stdout, probe.stdout + probe.stderr
    assert "PC=[unset] PS0=[] ALLOW=[unset]" in probe.stdout, probe.stdout
    assert "NOALIAS" in probe.stdout
    flags = probe.stdout.split("FLAGS=[", 1)[1].split("]", 1)[0]
    assert "a" not in flags, "allexport still on"
    assert not any(line.startswith("trap -- '") for line in probe.stdout.splitlines()), probe.stdout


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_a_trap_still_set_after_the_script_fails_it(tmp_path):
    """The check on the command line itself: a trap left in place after the script (here one the sourced
    file sets; bash keeps an ERR trap set by a sourced file) fails the preparation with `shell_hooks`."""
    f = tmp_path / "check.sh"
    f.write_text(L.prep_script(RO_COPILOT, K.adapter("copilot")).replace("return 0", "trap 'true' ERR\nreturn 0"))
    probe = subprocess.run(["bash", "-c", L.prep_command(str(f))], env={"PATH": f"{fake_bin(tmp_path)}:/usr/bin:/bin"},
                           capture_output=True, text=True, timeout=30)
    assert "__pa_reason_shell_hooks_2" in probe.stdout and "__pa_env_fail_2" in probe.stdout, probe.stdout
    assert not os.path.exists(str(f) + ".traps")


@pytest.mark.parametrize("shell", SHELLS)
def test_prompt_command_that_cannot_be_removed_fails_the_script(shell, tmp_path):
    head, _, out, err = run(shell, L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path,
                            f"PROMPT_COMMAND={shlex.quote(HOOK)}; readonly PROMPT_COMMAND")
    assert head == "__pa_reason_shell_hooks_2", (out, err)


@pytest.mark.parametrize("shell", SHELLS)
def test_allexport_is_switched_off(shell, tmp_path):
    """Phase-5 design notes (A2): with `set -a` an assignment in the prompt would be exported."""
    head, env, out, err = run(shell, L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path, "set -a")
    assert head == "__OK__", (out, err)


@pytest.mark.parametrize("shell", SHELLS)
def test_plain_shell_passes_the_trap_check(shell, tmp_path):
    """No hooks at all: the trap check (with POSIX-mode bash printing `trap -- - SIG` lines) passes."""
    head, _, out, err = run(shell, L.prep_script(RO_COPILOT, K.adapter("copilot")), tmp_path)
    assert head == "__OK__", (out, err)


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_hook_that_re_exports_backend_variables_is_removed(tmp_path):
    """agent-session scenario: a PROMPT_COMMAND that re-exports another backend after the script ran."""
    f = tmp_path / "check.sh"
    f.write_text(L.prep_script(claude_spec("full_auto"), K.adapter("claude")))
    hook = "export ANTHROPIC_BASE_URL=https://other.example.com CLAUDE_CODE_USE_BEDROCK=1"
    probe = subprocess.run(
        ["bash", "-c", f"PROMPT_COMMAND={shlex.quote(hook)}\n{L.prep_command(str(f))}\n"
                       'eval "$PROMPT_COMMAND"; echo "URL=[$ANTHROPIC_BASE_URL] BEDROCK=[${CLAUDE_CODE_USE_BEDROCK-unset}]"'],
        env={"PATH": f"{fake_bin(tmp_path)}:/usr/bin:/bin", **INHERITED}, capture_output=True, text=True, timeout=30)
    assert "__pa_env_ok_2" in probe.stdout, probe.stdout + probe.stderr
    assert "URL=[https://llm.example.com] BEDROCK=[unset]" in probe.stdout, probe.stdout


# ---- task 13.22: the prepared pane's shell writes no history (D6 "shell 历史") ------------------------


def _interactive(tmp_path, posix, lines, env_extra=None):
    """An interactive bash (no rc files) fed ``lines`` on stdin, with HOME in ``tmp_path``: the user's
    real history file is never involved."""
    home = tmp_path / ("home-posix" if posix else "home")
    home.mkdir()
    argv_ = ["bash", "--posix", "--noprofile", "-i"] if posix else ["bash", "--norc", "--noprofile", "-i"]
    env = {"PATH": f"{fake_bin(tmp_path)}:/usr/bin:/bin", "HOME": str(home), **(env_extra or {})}
    out = subprocess.run(argv_, input="\n".join(lines + ["exit"]) + "\n", env=env, capture_output=True, text=True,
                         timeout=30)
    return home, out


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("posix", [False, True], ids=["bash", "bash-posix"])
@pytest.mark.parametrize("custom", [False, True], ids=["default-histfile", "histfile-from-rc"])
def test_prepared_shell_writes_no_history(tmp_path, posix, custom):
    """Scenario: 准备过环境的终端不写历史 — for full_auto with variables and for read_only."""
    for spec in (AgentSpec(kind="copilot", env={"X_VAR": "v"}).resolve("full_auto"), RO_COPILOT):
        run_dir = tmp_path / spec.policy
        (run_dir / "c").mkdir(parents=True)
        (run_dir / "p").mkdir()
        custom_file = run_dir / "custom_history"
        extra = {"HISTFILE": str(custom_file)} if custom else {}
        control, _ = _interactive(run_dir / "c", posix, ["echo MARK_CONTROL"], extra)
        written = custom_file if custom else next(control.iterdir(), None)
        assert written is not None and "MARK_CONTROL" in written.read_text(), "the control shell must write history"
        if custom:
            custom_file.unlink()
        script = run_dir / "prep.sh"  # a fresh file: the command line removes it after sourcing
        script.write_text(L.prep_script(spec, K.adapter("copilot")))
        home, out = _interactive(run_dir / "p", posix,
                                 ["echo MARK_BEFORE", L.prep_command(str(script)), "echo MARK_AFTER"], extra)
        assert "__pa_env_ok_2" in out.stdout, out.stdout + out.stderr
        assert list(home.iterdir()) == [] and not custom_file.exists()


def test_history_file_is_unset_in_every_prepared_script():
    for spec in (AgentSpec(kind="copilot", env={"X": "1"}), RO_COPILOT, claude_spec(policy="full_auto")):
        assert "\\command unset HISTFILE 2>/dev/null" in L.prep_script(spec, K.adapter(spec.kind))
