"""Task 8.1: self-tests of the real-agent safety lab (design D17). No agent is started here."""

import os
import secrets
import stat
import subprocess
import time

import pytest

from .agentlab import (HOME, NONCE_VAR, AgentLab, _norm, claude_secrets, digest_diff, owned, preflight,
                       session_names, user_config_digest)

pytestmark = pytest.mark.integration


def test_lab_tears_everything_down_after_an_exception(real_herdr):
    """The code under test fails right after the lab prepared homes and started the server: the user's
    configuration digest is unchanged, the temporary root is gone, the session list is as before and
    nothing still refers to the root."""
    before = user_config_digest()
    sessions = session_names(real_herdr)
    lab = AgentLab(real_herdr, tag="selftest")
    with pytest.raises(RuntimeError, match="boom after setup"):
        with lab:
            assert lab.name in session_names(real_herdr)
            assert stat.S_IMODE(os.stat(lab.root / "cfg").st_mode) == 0o700
            for var, path in lab.homes.items():
                assert lab.env[var] == str(path) and lab.root in path.parents
            pane = lab.pa.herdr.workspace_create(str(lab.work()), "selftest")["root_pane"]["pane_id"]
            assert pane
            raise RuntimeError("boom after setup")
    assert lab.problems == []
    assert not lab.root.exists()
    assert owned(lab.nonce) == []
    assert session_names(real_herdr) == sessions
    assert digest_diff(before, user_config_digest()) == []


def test_isolated_homes_carry_login_fields_only(real_herdr):
    with AgentLab(real_herdr, tag="homes") as lab:
        for kind in ("codex", "claude", "copilot"):
            try:
                lab.prepare(kind)
            except pytest.skip.Exception:
                pass  # no login state of that kind on this host
        assert lab._prepared, "no kind could be prepared"
        for kind in lab._prepared:
            var = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "copilot": "COPILOT_HOME"}[kind]
            assert stat.S_IMODE(os.stat(lab.homes[var]).st_mode) == 0o700, var
        secrets = claude_secrets()
        for path in lab.homes.values():
            for dirpath, _, files in os.walk(path):  # (directories of kinds not prepared do not exist)
                for n in files:
                    p = os.path.join(dirpath, n)
                    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p
                    text = open(p, errors="replace").read()
                    assert not any(s in text for s in secrets), f"claude file value copied into {n}"
        if "copilot" in lab._prepared:
            import json

            keys = set(json.loads((lab.homes["COPILOT_HOME"] / "config.json").read_text()))
            assert keys <= {"firstLaunchAt", "loggedInUsers", "lastLoggedInUser", "authTokens"}
        assert "XDG_CONFIG_HOME" not in lab.env  # it would move herdr's own session directory (D3)
    assert lab.problems == []


def test_shim_still_refuses_calls_outside_the_temp_session(real_herdr):
    with AgentLab(real_herdr, tag="shim") as lab:
        out = subprocess.run([str(lab.shim), "--session", "default", "workspace", "list"], capture_output=True,
                             text=True, env=lab.env, timeout=30)
        assert out.returncode != 0 and "refused_by_test_shim" in out.stderr
        assert any("--session default" in c for c in lab.audit())
        # A server launched without the isolated homes (e.g. a restart from the caller's environment) is refused.
        bare = {k: v for k, v in lab.env.items() if k not in lab.homes}
        out = subprocess.run([str(lab.shim), "--session", lab.name, "server"], capture_output=True, text=True,
                             env=bare, timeout=30, stdin=subprocess.DEVNULL)
        assert out.returncode != 0 and "without the isolated" in out.stderr
    assert lab.problems == []


def test_digest_sees_config_install_and_link_changes(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("a")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "share" / "claude" / "versions" / "1").mkdir(parents=True)
    (home / ".local" / "bin" / "claude").symlink_to(home / ".local" / "share" / "claude" / "versions" / "1")
    a = user_config_digest(home)
    (home / ".codex" / "config.toml").write_text("b")
    (home / ".local" / "share" / "claude" / "versions" / "2").mkdir()
    (home / ".local" / "bin" / "claude").unlink()
    (home / ".local" / "bin" / "claude").symlink_to(home / ".local" / "share" / "claude" / "versions" / "2")
    changed = digest_diff(a, user_config_digest(home))
    assert {".codex/config.toml", ".local/share/claude/versions", ".local/bin/claude"} <= set(changed)


def test_reaper_only_touches_processes_that_inherited_the_lab_nonce(real_herdr, tmp_path):
    """A process that merely mentions the lab root (an editor, `tail`) is not the lab's and is left alone;
    a helper detached from the pane (it inherited the nonce) is killed."""
    lab = AgentLab(real_herdr, tag="reap")
    with lab:
        bystander = subprocess.Popen(["bash", "-c", "sleep 60", "bystander", str(lab.root)], start_new_session=True)
        pane = lab.pa.herdr.workspace_create(str(lab.work()), "reap")["root_pane"]["pane_id"]
        lab.pa.herdr.pane_run(pane, "setsid sleep 300 >/dev/null 2>&1 < /dev/null &")
        deadline = time.monotonic() + 10
        while not any("sleep 300" in c for _, c in lab.owned()) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert any("sleep 300" in c for _, c in lab.owned())
    try:
        assert bystander.poll() is None
        assert owned(lab.nonce) == [] and lab.problems == []
        assert any("sleep 300" in n for n in lab.notes)
    finally:
        bystander.kill()
        bystander.wait()


def test_lab_names_are_generated_and_new(real_herdr):
    with pytest.raises(TypeError):
        AgentLab(real_herdr, name="default")  # type: ignore[call-arg]


def test_escapes_outside_the_watched_files_are_found(real_herdr, tmp_path):
    """Review C1/T9: entries outside the root that carry its name, in any form, or files changed during the
    lab whose content names it, are found without any list of known paths. A temporary home and
    temporary directory stand in for the user's (task 8.10)."""
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    (home / ".cache").mkdir(parents=True)
    tmp.mkdir()
    lab = AgentLab(real_herdr, tag="escape", home=home, tmpdir=tmp)
    with lab:
        cache = home / ".cache" / "probe"
        cache.mkdir()
        (cache / f"-tmp-{_norm(lab.root.name)}-w-claude").mkdir()  # claude's encoding of a lab directory
        content = tmp / "probe.log"
        content.write_text(f"session started in {lab.root}/w/x\n")  # a log that only mentions it
        top = home / f"notes-{lab.root.name}.txt"  # a regular file at the top of the home directory
        top.write_text("")
    problems = " ".join(lab.problems)
    assert "carry its name" in problems
    assert str(cache / f"-tmp-{_norm(lab.root.name)}-w-claude") in problems and str(content) in problems
    assert str(top) in problems


def test_preflight_reports_leftovers_of_a_killed_run(real_herdr, tmp_path):
    """Review C3: a killed run's root and processes are reported (and left alone)."""
    (tmp_path / "pa-it-killed-abc123").mkdir()
    proc = subprocess.Popen(["sleep", "30"], env={**os.environ, NONCE_VAR: "left-over"})
    try:
        problems = " ".join(preflight(real_herdr, tmp=tmp_path))
        assert "pa-it-killed-abc123" in problems and str(proc.pid) in problems
        assert (tmp_path / "pa-it-killed-abc123").exists() and proc.poll() is None
    finally:
        proc.kill()
        proc.wait()
        (tmp_path / "pa-it-killed-abc123").rmdir()


def test_preflight_reports_a_left_over_test_session(real_herdr):
    """D17 (phase-5 review A8): a `polyagent-test-*` session left by a killed run stops the next run."""
    import json
    import secrets as _secrets

    from .agentlab import clean_env

    name = f"polyagent-test-left{_secrets.token_hex(3)}"
    server = subprocess.Popen([real_herdr, "--session", name, "server"], env=clean_env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while name not in session_names(real_herdr) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert any(name in p for p in preflight(real_herdr))
    finally:
        subprocess.run([real_herdr, "session", "stop", name, "--json"], env=clean_env(), capture_output=True, timeout=60)
        try:
            server.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, 9)
            server.wait(5)
        if name in session_names(real_herdr):
            out = subprocess.run([real_herdr, "session", "delete", name, "--json"], env=clean_env(),
                                 capture_output=True, text=True, timeout=60)
            assert json.loads(out.stdout).get("deleted"), out.stderr
    assert name not in session_names(real_herdr)


# ---- task 8.10 --------------------------------------------------------------------------------------


def test_lab_pane_history_goes_to_the_lab_root(real_herdr, tmp_path):
    """A command typed into a lab pane ends up in the lab root's history file when the shell exits, not in
    the user's history (D17: HISTFILE of the lab's server)."""
    import json as _json

    from .agentlab import HISTORY_FILES

    marker = f"pa_hist_{secrets.token_hex(6)}"
    lab = AgentLab(real_herdr, tag="hist")
    with lab:
        w = lab.work("h")
        out = lab.herdr("workspace", "create", "--cwd", str(w), "--label", "pa-hist-probe", "--no-focus")
        created = _json.loads(out.stdout)["result"]
        pane, wid = created["root_pane"]["pane_id"], created["workspace"]["workspace_id"]
        time.sleep(1.0)  # let the shell print its prompt
        assert lab.herdr("pane", "run", pane, f"echo {marker}").returncode == 0
        # Every lab pane also keeps claude from updating the user's install (phase 8).
        assert lab.herdr("pane", "run", pane, 'echo "upd=[$DISABLE_AUTOUPDATER]"').returncode == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "upd=[1]" not in lab.herdr("pane", "read", pane).stdout:
            time.sleep(0.2)
        assert "upd=[1]" in lab.herdr("pane", "read", pane).stdout
        assert lab.herdr("pane", "run", pane, "exit").returncode == 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (lab.histfile.exists() and marker in lab.histfile.read_text()):
            time.sleep(0.2)
        assert lab.histfile.exists() and marker in lab.histfile.read_text()
        lab.herdr("workspace", "close", wid)
    assert lab.problems == []
    for rel in HISTORY_FILES:
        p = HOME / rel
        if p.exists():
            assert marker.encode() not in p.read_bytes(), f"the marker reached the user's {rel}"


def test_history_check_reports_growth_of_the_preparation_sentinel(tmp_path):
    from .agentlab import PREP_SENTINEL, history_growth, history_sentinels

    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_history").write_bytes(b"ls\n" + PREP_SENTINEL + b"\n")
    before = history_sentinels(home)
    assert history_growth(before, history_sentinels(home)) == []
    with open(home / ".bash_history", "ab") as f:
        f.write(b". /run/x.sh && echo " + PREP_SENTINEL + b"\n")
    assert history_growth(before, history_sentinels(home)) == [f"{home / '.bash_history'} (1 -> 2)"]


def test_escape_check_counts_only_the_current_users_entries(tmp_path):
    from .agentlab import find_escapes

    root = tmp_path / "tmp" / "pa-it-x-abc"
    root.mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp" / f"other-{root.name}").write_text("")
    args = dict(home=tmp_path / "home", tmp=tmp_path / "tmp")
    assert find_escapes(root, 0, **args) == [str(tmp_path / "tmp" / f"other-{root.name}")]
    assert find_escapes(root, 0, uid=os.getuid() + 1, **args) == []


def test_preflight_ignores_other_users(tmp_path, monkeypatch):
    from . import agentlab

    tmp = tmp_path / "tmp"
    (tmp / "pa-it-theirs-1").mkdir(parents=True)
    (tmp / "pa-it-mine-1").mkdir()
    real_uid = agentlab._uid_of
    monkeypatch.setattr(agentlab, "_uid_of",
                        lambda p: os.getuid() + 1 if "theirs" in str(p) else real_uid(p))
    monkeypatch.setattr(agentlab, "session_names", lambda real: set())
    problems = agentlab.preflight("herdr", tmp=tmp)
    text = " ".join(problems)
    assert "pa-it-mine-1" in text and "pa-it-theirs-1" not in text


