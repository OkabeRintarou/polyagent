"""Task 8.1: self-tests of the real-agent safety lab (design D17). No agent is started here."""

import os
import secrets
import shutil
import stat
import subprocess
import time

import pytest

from .agentlab import (HOME, NONCE_VAR, AgentLab, UserFileGuard, _norm, claude_secrets, digest_diff, owned,
                       preflight, session_names, tree_digest, user_config_digest)

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
        for kind in ("codex", "claude", "copilot", "cursor"):
            try:
                lab.prepare(kind)
            except pytest.skip.Exception:
                pass  # no login state of that kind on this host
        assert lab._prepared, "no kind could be prepared"
        for kind in lab._prepared:
            var = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR", "copilot": "COPILOT_HOME",
                   "cursor": "CURSOR_CONFIG_DIR"}[kind]
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


def test_guard_restores_and_refuses_a_leftover_backup(tmp_path):
    home, store = tmp_path / "home", tmp_path / "backup"
    (home / ".cursor" / "sandbox-policies").mkdir(parents=True)
    (home / ".cursor" / "sandbox-policies" / "a.walk").write_text("original")
    before = user_config_digest(home)
    with pytest.raises(RuntimeError):
        with UserFileGuard([".cursor/sandbox-policies", ".cursor/absent.json"], home=home, root=store):
            (home / ".cursor" / "sandbox-policies" / "a.walk").write_text("changed by the agent")
            (home / ".cursor" / "sandbox-policies" / "new.walk").write_text("new")
            (home / ".cursor" / "absent.json").write_text("{}")
            raise RuntimeError("agent failed")
    assert digest_diff(before, user_config_digest(home)) == []
    assert not (home / ".cursor" / "absent.json").exists() and not store.exists()
    (store / "current").mkdir(parents=True)  # a backup an earlier run did not restore
    with pytest.raises(pytest.fail.Exception, match="still at"):
        with UserFileGuard([".cursor/sandbox-policies"], home=home, root=store):
            pass
    assert (store / "current").exists()  # left for the human, never overwritten


def test_digest_sees_config_install_and_link_changes(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("a")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "share" / "cursor-agent" / "versions" / "1").mkdir(parents=True)
    (home / ".local" / "bin" / "cursor-agent").symlink_to(home / ".local" / "share" / "cursor-agent" / "versions" / "1")
    a = user_config_digest(home)
    (home / ".codex" / "config.toml").write_text("b")
    (home / ".local" / "share" / "cursor-agent" / "versions" / "2").mkdir()
    (home / ".local" / "bin" / "cursor-agent").unlink()
    (home / ".local" / "bin" / "cursor-agent").symlink_to(home / ".local" / "share" / "cursor-agent" / "versions" / "2")
    changed = digest_diff(a, user_config_digest(home))
    assert {".codex/config.toml", ".local/share/cursor-agent/versions", ".local/bin/cursor-agent"} <= set(changed)


def test_guard_refuses_symlinks_inside_guarded_paths(tmp_path):
    home = tmp_path / "home"
    (home / ".cursor" / "sandbox-policies").mkdir(parents=True)
    (tmp_path / "elsewhere").write_text("real target")
    (home / ".cursor" / "sandbox-policies" / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(pytest.fail.Exception, match="symbolic links"):
        with UserFileGuard([".cursor/sandbox-policies"], home=home, root=tmp_path / "backup"):
            pass
    assert not (tmp_path / "backup").exists()


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
        allowed = home / ".cursor" / "ai-tracking" / f"{lab.root.name}.db"  # cursor may name it there
        allowed.parent.mkdir(parents=True)
        allowed.write_text(str(lab.root))
    problems = " ".join(lab.problems)
    assert "carry its name" in problems
    assert str(cache / f"-tmp-{_norm(lab.root.name)}-w-claude") in problems and str(content) in problems
    assert str(top) in problems and str(allowed) not in problems


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


def test_guard_copy_failure_leaves_no_backup(tmp_path):
    """Review C5: a backup that fails half-way leaves nothing that a later run would take for a backup."""
    home, store = tmp_path / "home", tmp_path / "backup"
    guarded = home / ".cursor" / "ai-tracking"
    guarded.mkdir(parents=True)
    (guarded / "ok.db").write_text("x")
    (guarded / "locked.db").write_text("y")
    (guarded / "locked.db").chmod(0)
    try:
        with pytest.raises(Exception):
            with UserFileGuard([".cursor/ai-tracking"], home=home, root=store):
                pass
        assert not (store / "current").exists() and list(store.iterdir()) == []
    finally:
        (guarded / "locked.db").chmod(0o600)


def test_tree_digest_sees_names_content_and_links(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_text("1")
    before = tree_digest(tmp_path)
    (tmp_path / "a" / "f").write_text("2")
    (tmp_path / "new").write_text("")
    (tmp_path / "ln").symlink_to(tmp_path / "a")
    assert set(digest_diff(before, tree_digest(tmp_path))) == {"a/f", "new", "ln"}


def test_foreign_cursor_check_ignores_commands_that_mention_the_name():
    from .agentlab import foreign_cursor_processes

    p = subprocess.Popen(["bash", "-c", "sleep 30 # cursor-agent mentioned only"])
    try:
        assert p.pid not in foreign_cursor_processes()
    finally:
        p.kill()
        p.wait()


def test_cursor_sockets_left_by_the_lab_are_removed_others_kept(tmp_path):
    """cursor ignores TMPDIR for its askpass sockets: dead ones created during the lab are removed, ones
    that existed before or are still bound are left (the latter reported)."""
    import socket

    from .agentlab import clean_cursor_sockets

    old = tmp_path / "cursor-askpass-old.sock"
    old.write_text("")
    before = set(tmp_path.glob("cursor-askpass-*"))
    since = time.time() - 1
    dead, live = tmp_path / "cursor-askpass-dead.sock", tmp_path / "cursor-askpass-live.sock"
    s1 = socket.socket(socket.AF_UNIX)
    s1.bind(str(dead))
    s1.close()
    s2 = socket.socket(socket.AF_UNIX)
    s2.bind(str(live))
    s2.listen()
    try:
        removed, problems = clean_cursor_sockets(before, since, tmp=tmp_path)
        assert removed == ["cursor-askpass-dead.sock"] and not dead.exists()
        assert old.exists() and live.exists() and any("still bound" in p for p in problems)
    finally:
        s2.close()


def test_guard_restores_entry_by_entry(tmp_path):
    """D17 / review A9: only entries that differ are written back; untouched ones keep their time."""
    home = tmp_path / "home"
    d = home / ".cursor" / "sandbox-policies" / "walk-cache"
    d.mkdir(parents=True)
    (d / "same.walk").write_text("same")
    (d / "changed.walk").write_text("before")
    os.utime(d / "same.walk", (1_000_000_000, 1_000_000_000))
    with UserFileGuard([".cursor/sandbox-policies"], home=home, root=tmp_path / "backup"):
        (d / "changed.walk").write_text("after")
        (d / "new.walk").write_text("new")
        (d / "newdir").mkdir()
    assert (d / "changed.walk").read_text() == "before" and not (d / "new.walk").exists()
    assert not (d / "newdir").exists() and os.stat(d / "same.walk").st_mtime == 1_000_000_000


def test_cursor_state_differences_only_inside_the_closed_list(tmp_path):
    from .agentlab import cursor_state, cursor_state_diff

    home = tmp_path / "home"
    for rel in (".cursor/ai-tracking", ".cursor/skills-cursor", ".config/cursor", ".local/share/cursor-agent/v1"):
        (home / rel).mkdir(parents=True)
    (home / ".cursor/ai-tracking/db").write_text("a")
    (home / ".cursor/hooks.json").write_text("{}")
    before = cursor_state(home)
    (home / ".cursor/ai-tracking/db").write_text("b")  # allowed to change
    (home / ".cursor/skills-cursor/new.md").write_text("x")  # allowed to change
    assert cursor_state_diff(before, cursor_state(home)) == []
    (home / ".cursor/hooks.json").write_text('{"x": 1}')
    (home / ".local/share/cursor-agent/v1/.running").mkdir()
    (home / ".local/share/cursor-agent/v1/.running/123").write_text("")
    assert set(cursor_state_diff(before, cursor_state(home))) == {
        ".cursor/hooks.json", ".local/share/cursor-agent/v1/.running", ".local/share/cursor-agent/v1/.running/123"}


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


def test_cursor_socket_is_kept_when_bindings_cannot_be_read(tmp_path, monkeypatch):
    import socket

    from . import agentlab

    before = set(tmp_path.glob("cursor-askpass-*"))
    dead = tmp_path / "cursor-askpass-dead.sock"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(dead))
    s.close()
    (tmp_path / "cursor-askpass-plain").write_text("")  # not a socket: never touched
    monkeypatch.setattr(agentlab, "bound_unix_paths", lambda: None)
    removed, problems = agentlab.clean_cursor_sockets(before, time.time() - 5, tmp=tmp_path)
    assert removed == [] and dead.exists() and (tmp_path / "cursor-askpass-plain").exists()
    assert any("could not be read" in p for p in problems)


def test_preflight_ignores_other_users_and_separates_half_finished_copies(tmp_path, monkeypatch):
    from . import agentlab

    tmp, store = tmp_path / "tmp", tmp_path / "backup"
    (tmp / "pa-it-theirs-1").mkdir(parents=True)
    (tmp / "pa-it-mine-1").mkdir()
    (store / "staging-abc").mkdir(parents=True)
    real_uid = agentlab._uid_of
    monkeypatch.setattr(agentlab, "_uid_of",
                        lambda p: os.getuid() + 1 if "theirs" in str(p) else real_uid(p))
    monkeypatch.setattr(agentlab, "session_names", lambda real: set())
    problems = agentlab.preflight("herdr", tmp=tmp, backup_root=store)
    text = " ".join(problems)
    assert "pa-it-mine-1" in text and "pa-it-theirs-1" not in text
    assert "half-finished" in text and "complete backup" not in text
    (store / "current").mkdir()
    assert any("complete backup" in p for p in agentlab.preflight("herdr", tmp=tmp, backup_root=store))


def test_foreign_cursor_check_ignores_other_programs_named_agent(tmp_path):
    from .agentlab import foreign_cursor_processes

    exe = tmp_path / "agent"
    exe.symlink_to(shutil.which("sleep"))
    p = subprocess.Popen([str(exe), "30"])
    try:
        assert p.pid not in foreign_cursor_processes()
    finally:
        p.kill()
        p.wait()
