"""Tasks 6.1–6.3: start flow, pane preparation, start-up dialogs, naming, attach/list, close, keys
(design D6, D9, D4, D11). PolyAgent is built on the subprocess-level fake, then its client is
replaced by the in-memory ``FakeClient``."""

import os
import re
import threading
from pathlib import Path

import dataclasses
import pytest

from fakeclient import INPUT_BOX, FakeClient
from polyagent import agent as A
from polyagent import kinds as K
from polyagent import locks
from polyagent.errors import (
    AgentBlockedError,
    AgentNameConflictError,
    AgentNotFoundError,
    AgentTimeoutError,
    HerdrError,
    InvalidConfigError,
    PolicyNotSupportedError,
)
from polyagent.manager import Marker, PolyAgent
from polyagent.spec import AgentSpec, ClaudeProvider

SCREENS = Path(__file__).parent / "fixtures" / "screens"
TOKEN = "fake-token-0123456789"
PROVIDER = ClaudeProvider.compatible("https://llm.example.com", auth_token=TOKEN, model="m1")
READY_SCREEN = "$ agent ready\n" + INPUT_BOX


@pytest.fixture
def pa(fake):
    p = PolyAgent()
    p._herdr = FakeClient()
    return p


@pytest.fixture
def client(pa):
    return pa._herdr


def screen(rel):
    return (SCREENS / rel).read_text()


def prep_paths(c):
    return [re.search(r"(?:^|; )\. (\S+?);", a[1]).group(1).strip("'") for a in c.args_of("pane_run")]


# ---- 6.1 start flow ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["codex", "copilot", "claude"])
def test_full_auto_without_variables_needs_no_preparation(pa, client, tmp_path, kind):
    agent = pa.start(AgentSpec(kind=kind), tmp_path)
    assert not client.args_of("pane_run") and not client.args_of("pane_process_info")
    (_, label), = client.args_of("workspace_create")
    m = Marker.parse(label)
    assert m.policy == "full_auto" and m.kind == kind and m.name == agent.name
    assert (m.session_id is not None) == (kind in ("copilot", "claude"))
    (name, herdr_kind, pane, timeout_ms, args), = client.args_of("agent_start")
    assert herdr_kind == kind and timeout_ms == 60_000 and pane == agent.pane_id
    if m.session_id:
        assert args[args.index("--session-id") + 1] == m.session_id


def test_cursor_always_prepares_and_stops_self_update(pa, client, tmp_path):
    pa.start(AgentSpec(kind="cursor"), tmp_path)
    script, = client.prep_scripts
    assert "export AGENT_CLI_UPDATE_CHECK_URL=" in script
    assert client.names().index("pane_run") < client.names().index("agent_start")


def test_read_only_always_prepares(pa, client, tmp_path):
    pa.start(AgentSpec(kind="copilot", policy="read_only", env={"GH_TOKEN": TOKEN}), tmp_path)
    script, = client.prep_scripts
    lines = script.splitlines()
    assert "\\command unalias copilot 2>/dev/null; \\command unset -f copilot 2>/dev/null" in lines
    assert any(line.startswith("\\command unset COPILOT_ALLOW_ALL") for line in lines)
    assert lines[1] == "{ \\command set +xva; } 2>/dev/null"
    exported = lines.index(f"\\command export GH_TOKEN={TOKEN}")
    assert lines[-1] == "return 0"
    # the re-checks (D6) come after the exports: the export took effect (review N3), then the read_only ones
    checks = lines[exported + 1:-1]
    assert (f'if \\command test "${{GH_TOKEN+x}}" != x || \\command test "$GH_TOKEN" != {TOKEN}; '
            "then \\command echo __pa_reason_export_failed_$((1+1)); return 1; fi") in checks
    assert "__pa_reason_shell_hooks_" in " ".join(lines[:exported]) or "__pa_reason_shell_hooks_" in " ".join(checks)
    assert all("__pa_reason_" in line for line in checks)


def test_read_only_claude_clears_provider_families_then_exports(pa, client, tmp_path):
    pa.start(AgentSpec(kind="claude", policy="read_only", provider=PROVIDER), tmp_path)
    script, = client.prep_scripts
    clear = next(i for i, line in enumerate(script.splitlines()) if "ANTHROPIC_" in line and "unset" in line)
    export = next(i for i, line in enumerate(script.splitlines()) if line.startswith("\\command export ANTHROPIC_AUTH_TOKEN="))
    assert clear < export
    for prefix in ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_"):
        assert prefix in script.splitlines()[clear]
    assert "CLOUD_ML_REGION" in script


def test_prep_script_runs_in_the_bash_family_shell_with_sentinels(pa, client, tmp_path):
    pa.start(AgentSpec(kind="cursor"), tmp_path)
    (_, command), = client.args_of("pane_run")
    assert command.startswith("{ \\command trap - DEBUG RETURN ERR; } 2>/dev/null; . ") and "__pa_env_ok_$((1+1))" in command and "__pa_env_fail_$((1+1))" in command
    assert "__pa_env_ok_2" not in command
    (_, _, regex), = client.args_of("pane_wait_output")
    assert regex == "__pa_env_(ok|fail)_2"
    assert not any(os.path.exists(p) for p in prep_paths(client))


def test_prep_file_is_private(pa, client, tmp_path, monkeypatch):
    seen = []
    orig = client.pane_run

    def pane_run(pane, command):
        path = re.search(r"(?:^|; )\. (\S+?);", command).group(1).strip("'")
        seen.append(os.stat(path).st_mode & 0o777)
        orig(pane, command)

    client.pane_run = pane_run
    pa.start(AgentSpec(kind="cursor", env={"X": "1"}), tmp_path)
    assert seen == [0o600]


@pytest.mark.parametrize("shell", ["fish", "nu", "pwsh", "zsh", "-zsh", "ksh"])
def test_non_posix_shell_is_refused(pa, client, tmp_path, shell):
    """zsh and ksh failed task 13.17 (exports, tracing): refused like non-POSIX shells."""
    client.shell = shell
    with pytest.raises(HerdrError) as ei:
        pa.start(AgentSpec(kind="cursor"), tmp_path)
    assert ei.value.code == "unsupported_shell"
    assert client.workspaces == {}


@pytest.mark.parametrize("shell", ["bash", "-bash", "sh", "dash"])
def test_posix_shells_are_accepted(pa, client, tmp_path, shell):
    client.shell = shell
    pa.start(AgentSpec(kind="cursor"), tmp_path)


@pytest.fixture
def codex_ro_verified(monkeypatch):
    a = K.adapter("codex")
    caps = dict(a.capabilities, **{K.READ_ONLY: K.Capability(K.VERIFIED)})
    monkeypatch.setitem(K.ADAPTERS, "codex", dataclasses.replace(a, capabilities=caps))


@pytest.mark.parametrize("reason,words", [("sandbox_unavailable", "user namespaces"), ("user_rules", "rules")])
def test_self_check_failure(pa, client, tmp_path, codex_ro_verified, reason, words):
    """Scenarios: 沙箱不可用的主机 / 用户级放行规则 — PolicyNotSupportedError, no workspace left."""
    client.prep = f"reason:{reason}"
    with pytest.raises(PolicyNotSupportedError) as ei:
        pa.start(AgentSpec(kind="codex", policy="read_only"), tmp_path)
    assert words in str(ei.value)
    assert client.workspaces == {} and not client.args_of("agent_start")
    assert not any(os.path.exists(p) for p in prep_paths(client))
    script, = client.prep_scripts
    assert "__pa_reason_sandbox_unavailable_$((1+1))" in script and "__pa_reason_user_rules_$((1+1))" in script
    assert "__pa_reason_user_rules_2" not in script


@pytest.mark.parametrize("failure", ["fail", "reason:provider_env", "reason:export_failed"])
def test_injection_failure_closes_the_workspace(pa, client, tmp_path, failure):
    """Scenarios: 注入失败 / 清除没有生效 — HerdrError(code="prep_failed") (D6 step 3, D16)."""
    client.prep = failure
    with pytest.raises(HerdrError) as ei:
        pa.start(AgentSpec(kind="claude", env={"X_TOKEN": TOKEN}), tmp_path)
    assert ei.value.code == "prep_failed" and not isinstance(ei.value, PolicyNotSupportedError)
    if failure == "reason:export_failed":  # D6: the message points at the export, not the provider families
        assert "exported variable" in str(ei.value) and "provider" not in str(ei.value)
    assert client.workspaces == {}
    assert not any(os.path.exists(p) for p in prep_paths(client))


@pytest.mark.parametrize("reason,words", [("executable_shadowed", "alias"), ("permission_env", "permission switch")])
def test_read_only_recheck_failure_is_policy_not_supported(pa, client, tmp_path, reason, words):
    """D6: the two read_only re-checks failing mean read_only cannot be enforced in this pane."""
    client.prep = f"reason:{reason}"
    with pytest.raises(PolicyNotSupportedError) as ei:
        pa.start(AgentSpec(kind="copilot", policy="read_only"), tmp_path)
    assert words in str(ei.value)
    assert client.workspaces == {} and not client.args_of("agent_start")
    script, = client.prep_scripts
    assert "__pa_reason_executable_shadowed_$((1+1))" in script and "__pa_reason_permission_env_$((1+1))" in script
    assert '${COPILOT_ALLOW_ALL+x}' in script


@pytest.mark.parametrize("policy,error", [("read_only", PolicyNotSupportedError), ("full_auto", HerdrError)])
def test_shell_hooks_reason_depends_on_the_policy(pa, client, tmp_path, policy, error):
    """Review C9: a shell hook that survives is a policy failure under read_only, an injection failure
    (prep_failed) otherwise."""
    client.prep = "reason:shell_hooks"
    with pytest.raises(error) as ei:
        pa.start(AgentSpec(kind="copilot", policy=policy, env={"X_TOKEN": TOKEN}), tmp_path)
    assert "PROMPT_COMMAND" in str(ei.value)
    if policy == "full_auto":
        assert ei.value.code == "prep_failed" and not isinstance(ei.value, PolicyNotSupportedError)
    assert client.workspaces == {}


def test_start_timeout_closes_the_workspace(pa, client, tmp_path):
    """Scenario: 启动失败清理."""
    client.start_result = "timeout"
    with pytest.raises(AgentTimeoutError):
        pa.start(AgentSpec(kind="codex"), tmp_path, start_timeout=5)
    assert client.workspaces == {}
    assert client.args_of("agent_start")[0][3] == 5000


def test_credentials_never_reach_a_herdr_argument(pa, client, tmp_path):
    """Scenario: 注入凭据 (library side): values only in the 0600 script."""
    hdr = "hv-" + TOKEN
    prov = ClaudeProvider.compatible("https://llm.example.com", auth_token=TOKEN, custom_headers={"X-H": hdr})
    pa.start(AgentSpec(kind="claude", provider=prov, env={"GH_TOKEN": "gh-" + TOKEN}), tmp_path)
    assert TOKEN not in repr(client.calls)
    assert TOKEN in client.prep_scripts[0]


def test_name_taken_by_herdr_is_a_conflict_and_cleans_up(pa, client, tmp_path):
    client.start_result = "name_taken"
    with pytest.raises(AgentNameConflictError):
        pa.start(AgentSpec(kind="codex"), tmp_path, name="rev")
    assert client.workspaces == {}


def test_same_name_being_started_is_an_immediate_conflict(pa, client, tmp_path):
    """Scenario: 同名 agent 正在启动."""
    held = locks.acquire_for_start("polyagent", "reviewer")
    try:
        with pytest.raises(AgentNameConflictError):
            pa.start(AgentSpec(kind="codex"), tmp_path, name="reviewer")
    finally:
        held.release()
    assert not client.args_of("workspace_create")


def test_live_name_is_a_conflict(pa, client, tmp_path):
    """Scenario: 名字冲突."""
    client.workspaces["w9"] = {"workspace_id": "w9", "label": "manual"}
    client.add_agent("reviewer", "w9")
    with pytest.raises(AgentNameConflictError):
        pa.start(AgentSpec(kind="codex"), tmp_path, name="reviewer")
    assert not client.args_of("workspace_create")


def test_same_name_orphan_is_reclaimed_under_the_lock(pa, client, tmp_path):
    client.workspaces["w9"] = {"workspace_id": "w9", "label": Marker("reviewer", "codex", "full_auto", 5).encode()}
    pa.start(AgentSpec(kind="codex"), tmp_path, name="reviewer")
    assert "w9" not in client.workspaces


def test_static_checks_happen_before_any_herdr_call(pa, client, tmp_path):
    for bad in (dict(spec=AgentSpec(kind="cursor", policy="read_only"), cwd=tmp_path),
                dict(spec=AgentSpec(kind="codex"), cwd=tmp_path / "missing"),
                dict(spec=AgentSpec(kind="codex"), cwd=tmp_path, name="Bad Name"),
                dict(spec=AgentSpec(kind="codex"), cwd=tmp_path, start_timeout=3),
                dict(spec=AgentSpec(kind="codex", extra_args=["--"]), cwd=tmp_path)):
        with pytest.raises(InvalidConfigError):
            pa.start(**bad)
    assert client.calls == []


def test_workspace_cwd_is_the_resolved_path(pa, client, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    pa.start(AgentSpec(kind="claude"), tmp_path / "link")
    (cwd, _), = client.args_of("workspace_create")
    assert cwd == str(real)


# ---- 6.2 start-up dialogs ------------------------------------------------------------------------


def test_copilot_trust_dialog_session_only(pa, client, tmp_path):
    """Scenario: 目录信任对话框 — Enter picks option 1 (this session only)."""
    client.screens = [screen("copilot/trust-dialog.txt"), READY_SCREEN]
    pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert [k for _, *k in client.args_of("agent_send_keys")] == [["enter"]]
    assert "1. Yes" in screen("copilot/trust-dialog.txt").split("❯")[-1][:20]


def test_state_lagging_behind_an_answered_dialog_is_waited_out(pa, client, tmp_path, quick_dialogs):
    """Real herdr keeps copilot `blocked` for about a second after the trust dialog left the screen, and
    `agent wait` without `--until` returns at once on that state (phase 5): start still succeeds."""
    client.screens = [screen("copilot/trust-dialog.txt"), READY_SCREEN]
    client.start_status = "blocked"

    def on_keys(fake, name, keys):
        fake.screens.pop(0)
        fake.statuses[name] = ["blocked", "blocked", "blocked", "idle"]

    client.on_keys = on_keys
    agent = pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert agent.status() == "idle" and len(client.args_of("agent_send_keys")) == 1


def test_copilot_trust_dialog_drawn_after_idle_is_still_answered(pa, client, tmp_path, quick_dialogs):
    """Task 13.20: herdr reports copilot `idle` while its screen shows neither its input box nor the
    trust dialog yet; the dialog appears only then (seen in phase 8, the first prompt went into it).
    Start waits for the input box, so the late dialog is answered by start."""
    banner = "Copilot v1.0.112\n ● Disabled tools: bash, create\n"
    client.screens = [screen("copilot/trust-dialog.txt"), READY_SCREEN]
    reads = iter([banner, banner])
    client._visible = lambda: next(reads, None) or FakeClient._visible(client)
    agent = pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert [k for _, *k in client.args_of("agent_send_keys")] == [["enter"]]
    assert agent.status() == "idle"


def test_copilot_never_showing_its_input_box_is_a_start_timeout(pa, client, tmp_path, quick_dialogs, monkeypatch):
    from polyagent import _launch as L

    monkeypatch.setattr(L, "DIALOG_WAIT_MS", 200)
    client.screens = ["Copilot v1.0.112\n"]
    with pytest.raises(AgentTimeoutError, match="input area"):
        pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert client.workspaces == {}


def test_caller_disabled_auto_trust(pa, client, tmp_path):
    """Scenario: 调用方禁用自动信任."""
    client.screens = [screen("copilot/trust-dialog.txt")]
    with pytest.raises(AgentBlockedError) as ei:
        pa.start(AgentSpec(kind="copilot", auto_trust=False), tmp_path)
    assert "Do you trust the files in this folder?" in ei.value.screen
    assert not client.args_of("agent_send_keys") and client.workspaces == {}


def test_unknown_dialog_even_with_the_word_trust(pa, client, tmp_path):
    """Scenario: 未知启动对话框 (negative example containing "trust")."""
    client.start_status = "blocked"
    client.screens = ["Do you trust this plugin to run? [y/N]"]
    with pytest.raises(AgentBlockedError) as ei:
        pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert "trust this plugin" in ei.value.screen
    assert not client.args_of("agent_send_keys")


def test_dialog_still_on_screen_although_reported_ready(pa, client, tmp_path):
    """Scenario: 已报告就绪时仍有对话框 (cursor, auto_trust off)."""
    client.screens = [screen("cursor/trust-dialog.txt")]
    with pytest.raises(AgentBlockedError):
        pa.start(AgentSpec(kind="cursor", auto_trust=False), tmp_path)
    assert not client.args_of("agent_send_keys") and client.workspaces == {}
    assert "--trust" not in client.args_of("agent_start")[0][4]


def test_cursor_auto_trust_uses_the_flag(pa, client, tmp_path):
    pa.start(AgentSpec(kind="cursor"), tmp_path)
    assert "--trust" in client.args_of("agent_start")[0][4]


def test_codex_trust_dialog_is_never_answered(pa, client, tmp_path):
    client.screens = [screen("codex/trust-dialog.txt")]
    with pytest.raises(AgentBlockedError):
        pa.start(AgentSpec(kind="codex"), tmp_path)
    assert not client.args_of("agent_send_keys")
    args = client.args_of("agent_start")[0][4]
    assert any(a.startswith("projects={") for a in args)  # trusted inline, nothing written


def test_claude_dialogs_and_their_preconditions(pa, client, tmp_path):
    keyed = PROVIDER  # token provider: no API-key dialog expected, but if it shows, it is not ours to answer
    api_key = ClaudeProvider.compatible("https://llm.example.com", api_key=TOKEN)
    client.screens = [screen("claude/trust-dialog.txt"), screen("claude/api-key-confirmation-dialog.txt"),
                      screen("claude/bypass-permissions-dialog.txt"), READY_SCREEN]
    pa.start(AgentSpec(kind="claude", provider=api_key), tmp_path, name="c1")
    assert [k for _, *k in client.args_of("agent_send_keys")] == [["down", "enter"], ["up", "enter"], ["down", "enter"]]
    client.screens = [screen("claude/api-key-confirmation-dialog.txt")]
    with pytest.raises(AgentBlockedError):
        pa.start(AgentSpec(kind="claude", provider=keyed), tmp_path, name="c2")
    client.screens = [screen("claude/bypass-permissions-dialog.txt")]
    with pytest.raises(AgentBlockedError):
        pa.start(AgentSpec(kind="claude", policy="read_only", provider=keyed), tmp_path, name="c3")


@pytest.fixture
def quick_dialogs(monkeypatch):
    from polyagent import _launch as L

    monkeypatch.setattr(L, "DIALOG_GONE_WAIT", 0.05)
    monkeypatch.setattr(L, "DIALOG_POLL", 0.005)


def test_a_dialog_is_answered_once(pa, client, tmp_path, quick_dialogs):
    """Review C10 / T4: the same dialog still on screen after its keys is not answered again."""
    client.screens = [screen("copilot/trust-dialog.txt")] * 5
    with pytest.raises(AgentBlockedError):
        pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert len(client.args_of("agent_send_keys")) == 1 and client.workspaces == {}


def test_redrawn_dialog_text_is_waited_out(pa, client, tmp_path, quick_dialogs):
    """The keys go out once even if the old dialog text lingers for a few reads (C10)."""
    dlg = screen("claude/api-key-confirmation-dialog.txt")
    reads = {"n": 0}
    orig = client.pane_read

    def pane_read(pane, source="recent-unwrapped", lines=None):
        if source == "visible" and client.args_of("agent_send_keys"):
            reads["n"] += 1
            return dlg if reads["n"] < 3 else READY_SCREEN
        return orig(pane, source, lines)

    client.pane_read = pane_read
    client.screens = [dlg]
    client.on_keys = lambda *a: None
    pa.start(AgentSpec(kind="claude", provider=ClaudeProvider.compatible("https://llm.example.com", api_key=TOKEN)),
             tmp_path, start_timeout=10)
    assert [k for _, *k in client.args_of("agent_send_keys")] == [["up", "enter"]]


def test_answered_dialog_but_never_ready_is_a_timeout(pa, client, tmp_path, quick_dialogs):
    """C10: after answering, the agent must still become idle/done."""
    client.screens = [screen("copilot/trust-dialog.txt"), READY_SCREEN]
    client.start_status = "working"
    client.wait_result = lambda fake, name, ms, until: dict(fake.agents[name])
    with pytest.raises(AgentTimeoutError):
        pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert client.workspaces == {}


def test_dialog_that_comes_back_is_not_answered_again(pa, client, tmp_path, quick_dialogs):
    """D9: the dialog went away after its keys, then showed again: AgentBlockedError, keys sent once."""
    dlg = screen("copilot/trust-dialog.txt")
    client.screens = [dlg, READY_SCREEN]
    client.start_status = "working"

    def wait_result(fake, name, ms, until):
        fake.screens[:] = [dlg]  # back on screen after the wait
        return None

    client.wait_result = wait_result
    with pytest.raises(AgentBlockedError):
        pa.start(AgentSpec(kind="copilot"), tmp_path)
    assert len(client.args_of("agent_send_keys")) == 1 and client.workspaces == {}


def test_reported_ready_but_not_idle_is_a_timeout(pa, client, tmp_path):
    """D9: herdr's ready report does not replace the idle/done check at the end of dialog handling."""
    client.start_status = "working"
    client.wait_result = lambda fake, name, ms, until: dict(fake.agents[name])
    with pytest.raises(AgentTimeoutError):
        pa.start(AgentSpec(kind="codex"), tmp_path)
    assert client.workspaces == {}
    assert [a[2] for a in client.args_of("agent_wait")] == [()]  # one wait without --until


def test_preparation_timeout_is_an_agent_timeout(pa, client, tmp_path):
    """Review C9 / D16: a `pane wait-output` timeout at start is AgentTimeoutError."""
    client.prep = "timeout"
    with pytest.raises(AgentTimeoutError):
        pa.start(AgentSpec(kind="claude", env={"X": "1"}), tmp_path)
    assert client.workspaces == {}
    assert not any(os.path.exists(p) for p in prep_paths(client))


def test_not_ready_without_dialog_is_a_timeout(pa, client, tmp_path):
    client.start_result, client.start_status = "not_ready", "unknown"
    with pytest.raises(AgentTimeoutError):
        pa.start(AgentSpec(kind="codex"), tmp_path)
    assert client.workspaces == {}


# ---- 6.3 naming, attach, list, close, keys -----------------------------------------------------------


def test_automatic_name(pa, client, tmp_path):
    """Scenario: 自动命名."""
    names = {pa.start(AgentSpec(kind="codex"), tmp_path).name for _ in range(5)}
    assert len(names) == 5 and all(re.fullmatch(r"codex-[0-9a-f]{6}", n) for n in names)


def test_attach_rebuilds_from_the_marker(pa, client, tmp_path):
    """Scenario: 服务重启后接管 (handle side)."""
    started = pa.start(AgentSpec(kind="claude", policy="read_only", provider=PROVIDER), tmp_path, name="reviewer")
    got = pa.attach("reviewer")
    assert (got.kind, got.policy, got._session_id, got.workspace_id, got.pane_id) == (
        "claude", "read_only", started._session_id, started.workspace_id, started.pane_id)
    assert not hasattr(got, "_cwd")  # the log is found by session id; handles do not use the cwd (D12)


def test_attached_handle_keeps_the_read_only_refusal(pa, client, tmp_path, monkeypatch):
    """Scenario: 接管后策略仍生效."""
    monkeypatch.setattr(A, "POLL_INTERVAL", 0.005)
    pa.start(AgentSpec(kind="cursor"), tmp_path, name="w")  # some other agent
    client.workspaces["w7"] = {"workspace_id": "w7",
                               "label": Marker("reviewer", "claude", "read_only", 5, session_id="sid").encode()}
    client.add_agent("reviewer", "w7")
    agent = pa.attach("reviewer")
    log = tmp_path / "log.jsonl"
    agent._log_path = str(log)
    import test_ask as T

    client.on_prompt, client.on_keys = T.blocked_then(log, 1)
    assert agent.ask("write it", 5) == "done after refusal"
    assert [k for _, *k in client.args_of("agent_send_keys")] == [["esc"]]


@pytest.mark.parametrize("label", ["manual workspace", Marker("other", "codex", "full_auto", 5).encode()])
def test_attach_refuses_foreign_agents(pa, client, label):
    client.workspaces["w5"] = {"workspace_id": "w5", "label": label}
    client.add_agent("reviewer", "w5")
    with pytest.raises(AgentNotFoundError):
        pa.attach("reviewer")
    with pytest.raises(AgentNotFoundError):
        pa.attach("nobody")


def test_list_only_returns_polyagent_agents(pa, client, tmp_path):
    """Scenario: 按名字重新接管与列举 (list)."""
    a = pa.start(AgentSpec(kind="codex"), tmp_path, name="mine")
    client.workspaces["w8"] = {"workspace_id": "w8", "label": "manual"}
    client.add_agent("manual", "w8")
    client.workspaces["w9"] = {"workspace_id": "w9", "label": Marker("ghost", "codex", "full_auto", 5).encode()}
    assert [x.name for x in pa.list()] == ["mine"]
    assert pa.list()[0].workspace_id == a.workspace_id


def test_close(pa, client, tmp_path):
    """Scenarios: 关闭 / agent 已退出后关闭 / repeated close."""
    agent = pa.start(AgentSpec(kind="codex"), tmp_path, name="reviewer")
    agent.close()
    assert agent.workspace_id not in client.workspaces and pa.list() == []
    agent.close()  # idempotent
    other = pa.start(AgentSpec(kind="codex"), tmp_path, name="crashed")
    del client.agents["crashed"]  # the agent process exited; its workspace is still there
    other.close()
    assert other.workspace_id not in client.workspaces


def test_close_checks_the_marker(pa, client, tmp_path):
    agent = pa.start(AgentSpec(kind="codex"), tmp_path, name="reviewer")
    client.workspaces[agent.workspace_id]["label"] = "renamed by the user"
    agent.close()
    assert agent.workspace_id in client.workspaces
    with pytest.raises(AgentNotFoundError):
        agent.ask("q", 1)


def test_close_takes_no_lock(pa, client, tmp_path):
    agent = pa.start(AgentSpec(kind="codex"), tmp_path, name="reviewer")
    held = locks.acquire_for_start("polyagent", "reviewer")
    try:
        t = threading.Thread(target=agent.close)
        t.start()
        t.join(2)
        assert not t.is_alive()
    finally:
        held.release()


@pytest.mark.parametrize("keys", [("/allow-all",), ("a",), ("enter", "y"), ("Escape",), ()])
def test_send_keys_refuses_text(pa, client, tmp_path, keys):
    """Scenario: 发送按键拒绝文本."""
    agent = pa.start(AgentSpec(kind="codex"), tmp_path)
    with pytest.raises(InvalidConfigError):
        agent.send_keys(*keys)
    assert not client.args_of("agent_send_keys")


def test_send_keys_named(pa, client, tmp_path):
    agent = pa.start(AgentSpec(kind="codex"), tmp_path)
    agent.send_keys("esc", "down", "enter", "ctrl+c")
    assert [k for _, *k in client.args_of("agent_send_keys")] == [["esc", "down", "enter", "ctrl+c"]]


def test_status_and_read(pa, client, tmp_path):
    agent = pa.start(AgentSpec(kind="codex"), tmp_path)
    client.set_status(agent.name, "working")
    assert agent.status() == "working"
    client.recent = "screen text"
    assert agent.read() == "screen text"


def test_log_binding_follows_the_agent_process_environment(pa, client, tmp_path, monkeypatch):
    """D12: the copilot log root comes from COPILOT_HOME in the agent process's environment."""
    agent = pa.start(AgentSpec(kind="copilot"), tmp_path)
    orig = client.pane_process_info

    def info(pane):
        out = orig(pane)
        out["process_info"]["foreground_processes"].append({"pid": 202, "name": "copilot"})
        return out

    client.pane_process_info = info
    from polyagent.capture import locate

    iso = tmp_path / "iso-cp"
    expected = iso / "session-state" / agent._session_id / "events.jsonl"
    environ = {"v": None}
    monkeypatch.setattr(locate, "read_environ", lambda pid: environ["v"] if pid == 202 else None)
    log = agent._bind_log()
    assert log is not None and not log.exists()
    expected.parent.mkdir(parents=True)
    expected.write_text("")
    assert not log.exists()  # environment not readable yet: the default directory, not kept (no caching)
    assert agent._config is None
    environ["v"] = f"COPILOT_HOME={iso}\0".encode()
    assert log.exists() and log.path == str(expected)
    assert agent._config == iso
    other = A.Agent(client, name=agent.name, kind="copilot", policy="full_auto", workspace_id=agent.workspace_id,
                    pane_id=agent.pane_id, label=agent._label, session_id=agent._session_id)
    environ["v"] = b"PATH=/bin\0"
    assert other._config_dir() == locks.user_home() / ".copilot"


def test_start_can_run_on_several_threads_at_once(pa, client, tmp_path):
    # The discussion engine starts its members in parallel through one PolyAgent (design D16).
    barrier = threading.Barrier(2, timeout=5)
    out, errors = {}, []

    def start(name):
        try:
            barrier.wait()
            out[name] = pa.start(AgentSpec(kind="codex"), tmp_path, name=name)
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=start, args=(n,)) for n in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [] and set(out) == {"alpha", "beta"}
    assert out["alpha"].workspace_id != out["beta"].workspace_id and out["alpha"].pane_id != out["beta"].pane_id
    started = {a[0]: a[2] for a in client.args_of("agent_start")}
    assert started == {"alpha": out["alpha"].pane_id, "beta": out["beta"].pane_id}
    labels = {Marker.parse(a[1]).name for a in client.args_of("workspace_create")}
    assert labels == {"alpha", "beta"}


@pytest.mark.parametrize("spec", [AgentSpec(kind="codex"), AgentSpec(kind="claude", provider=PROVIDER, policy="read_only")],
                         ids=["full_auto-no-prep", "read_only"])
def test_every_pane_is_created_without_shell_history(pa, client, tmp_path, spec):
    """Task 13.19 / D6: the pane's shell gets HISTFILE=/dev/null from `workspace create --env`, so neither
    the start command nor a preparation line reaches the history of the user running the server."""
    pa.start(spec, tmp_path)
    assert client.workspace_env == [{"HISTFILE": "/dev/null"}]


def test_workspace_create_passes_env_as_options():
    from polyagent.herdr import HerdrClient

    c = HerdrClient.__new__(HerdrClient)
    seen = []
    c.call = lambda *args: seen.append(args) or {}
    c.workspace_create("/w d", "L", {"HISTFILE": "/dev/null", "A": "b c"})
    assert seen == [("workspace", "create", "--cwd", "/w d", "--no-focus", "--label", "L",
                     "--env", "HISTFILE=/dev/null", "--env", "A=b c")]
    c.workspace_create("/w", "L")
    assert seen[-1] == ("workspace", "create", "--cwd", "/w", "--no-focus", "--label", "L")
