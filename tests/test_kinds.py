"""Task 4.3: kind adapter table (design D7, D8, D9)."""

import dataclasses
from pathlib import Path

import pytest

from polyagent import kinds as K

FIXTURES = Path(__file__).parent / "fixtures"
SCREENS = FIXTURES / "screens"

# Evidence for each policy's native arguments (review T3): re-run the verification that produced
# these samples after upgrading the CLI, before trusting the assertions below.
EVIDENCE = {
    ("codex", "read_only"): ["logs/codex/readonly-no-rule-localshell.rollout.jsonl",
                             "logs/codex/readonly-sandbox-fail-localshell-rule-escape.rollout.jsonl", "herdr/README.md"],
    ("codex", "full_auto"): ["logs/codex/fullauto-sandbox-only.rollout.jsonl", "herdr/README.md"],
    ("copilot", "read_only"): ["logs/copilot/readonly-pong-read-denied.events.jsonl",
                               "logs/copilot/readonly-available-tools-nowait.events.jsonl"],
    ("copilot", "full_auto"): ["logs/copilot/fullauto-write-exit-noform-localshell.events.jsonl",
                               "logs/copilot/fullauto-background-task-async-shell-late-turns.events.jsonl",
                               "logs/copilot/fullauto-excluded-tools-async-shell-late-turn.events.jsonl"],
    ("claude", "read_only"): ["logs/claude/stub-readonly-denied-tools-prefixes.jsonl",
                              "logs/claude/real-readonly-restricted-env.jsonl", "logs/claude/real-mcp-config-allowlist.jsonl"],
    ("claude", "full_auto"): ["logs/claude/stub-fullauto.jsonl", "logs/claude/real-fullauto-write-askuser-bash.jsonl"],
}
SID = "11111111-2222-3333-4444-555555555555"


def args(kind, policy, **kw):
    return K.adapter(kind).native_args(policy, **kw)


def test_every_supported_policy_has_existing_evidence():
    for kind, a in K.ADAPTERS.items():
        policies = ["full_auto"] + (["read_only"] if a.read_only_args else [])
        for policy in policies:
            files = EVIDENCE[(kind, policy)]
            assert files and all((FIXTURES / f).is_file() for f in files), (kind, policy)


def test_every_kind_has_a_complete_capability_table():
    for a in K.ADAPTERS.values():
        assert {K.READ_ONLY, K.MCP_ALLOW, K.LOG_ADAPTER, K.INTERRUPT_KEY} <= set(a.capabilities)
        for cap in a.capabilities.values():
            assert cap.state in (K.VERIFIED, K.UNSUPPORTED, K.UNVERIFIED)
            assert cap.state != K.UNVERIFIED or cap.task


def test_read_only_states():
    assert K.adapter("claude").capability(K.READ_ONLY).state == K.VERIFIED
    assert K.adapter("copilot").capability(K.READ_ONLY).state == K.VERIFIED  # task 13.1 done
    assert K.adapter("codex").capability(K.READ_ONLY) == K.Capability(K.UNVERIFIED, "13.4", K.adapter("codex").capability(K.READ_ONLY).note)


def test_codex_args():
    for policy in ("read_only", "full_auto"):
        assert "--no-daemon" in args("codex", policy)
    assert "--dangerously-bypass-approvals-and-sandbox" in args("codex", "full_auto")
    ro = args("codex", "read_only")
    assert ro[ro.index("--sandbox") + 1] == "read-only" and ro[ro.index("--ask-for-approval") + 1] == "never"
    assert "--dangerously-bypass-approvals-and-sandbox" not in ro


def test_copilot_read_only_args():
    ro = args("copilot", "read_only", session_id=SID)
    denies = [ro[i + 1] for i, a in enumerate(ro) if a == "--deny-tool"]
    assert denies == ["shell", "write", "memory", "url"]  # four separate rules
    assert "--disable-builtin-mcps" in ro and "--available-tools=view,grep,glob" in ro
    assert not any(a.startswith(("--allow-tool", "--allow-url", "--allow-all", "--yolo")) for a in ro)


def test_copilot_full_auto_args():
    fa = args("copilot", "full_auto")
    assert "--yolo" in fa
    excluded = next(a for a in fa if a.startswith("--excluded-tools=")).split("=", 1)[1].split(",")
    assert {"task", "read_agent", "write_agent", "list_agents", "run_dynamic_workflow", "dynamic_workflows_manage"} <= set(excluded)


def _list(argv, option):
    i = argv.index(option)
    out = []
    for a in argv[i + 1:]:
        if a.startswith("-"):
            break
        out.append(a)
    return out


def test_claude_args():
    ro = args("claude", "read_only", session_id=SID)
    for flag in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in ro
    assert ro[ro.index("--permission-mode") + 1] == "dontAsk"
    assert _list(ro, "--allowedTools") == ["Read", "Grep", "Glob"]
    fa = args("claude", "full_auto", session_id=SID)
    assert "--dangerously-skip-permissions" in fa and "--restricted" not in fa
    for argv in (ro, fa):
        deny = _list(argv, "--disallowedTools")
        assert "AskUserQuestion" in deny and set(K.CLAUDE_ASYNC_TOOLS) <= set(deny)
        assert "MultiEdit" not in deny
        assert argv[-len(deny) - 1] == "--disallowedTools"  # variadic lists go last
    assert {"Edit", "Write", "NotebookEdit", "Bash", "WebFetch", "WebSearch"} <= set(_list(ro, "--disallowedTools"))


def test_claude_mcp_allow_entries_extend_the_allow_list():
    ro = args("claude", "read_only", allow_extra=("mcp__docs", "mcp__web__get"))
    assert _list(ro, "--allowedTools") == ["Read", "Grep", "Glob", "mcp__docs", "mcp__web__get"]
    with pytest.raises(ValueError):
        args("copilot", "read_only", allow_extra=("mcp__docs",))


@pytest.mark.parametrize("kind", ["copilot", "claude"])
def test_session_id_in_both_policies(kind):
    for policy in ("read_only", "full_auto"):
        a = args(kind, policy, session_id=SID)
        assert a[a.index("--session-id") + 1] == SID


@pytest.mark.parametrize("policy", ["read_only", "full_auto"])
def test_ask_form_disabled_in_both_policies(policy):
    """Scenarios: 默认策略下禁用提问表单 / 只读策略下禁用提问表单."""
    assert "--no-ask-user" in args("copilot", policy)
    assert "AskUserQuestion" in _list(args("claude", policy), "--disallowedTools")


def test_model_flag():
    assert args("codex", "full_auto", model="gpt-5")[-2:] == ["--model", "gpt-5"]
    with pytest.raises(ValueError):  # a kind without read_only arguments
        dataclasses.replace(K.adapter("codex"), read_only_args=()).native_args("read_only")


def test_kinds_and_adapter_table_are_exactly_the_three_v1_kinds():
    """Task 13.41: cursor is not supported in v1; neither its name nor its alias is a kind."""
    assert set(K.KINDS) == set(K.ADAPTERS) == {"codex", "copilot", "claude"}
    assert len(K.KINDS) == 3


def test_status_trust_and_keys():
    assert K.adapter("copilot").status_trusted is False
    assert all(K.adapter(k).status_trusted for k in ("codex", "claude"))
    assert {K.adapter(k).interrupt_key for k in K.KINDS} == {"esc"}


@pytest.mark.parametrize(
    "kind,dialog,fixture",
    [
        ("copilot", "trust", "copilot/trust-dialog.txt"),
        ("codex", "trust", "codex/trust-dialog.txt"),
        ("claude", "trust", "claude/trust-dialog.txt"),
        ("claude", "api_key", "claude/api-key-confirmation-dialog.txt"),
        ("claude", "bypass_permissions", "claude/bypass-permissions-dialog.txt"),
    ],
)
def test_dialog_signatures_match_only_their_screen(kind, dialog, fixture):
    d = next(x for x in K.adapter(kind).dialogs if x.name == dialog)
    for f in SCREENS.rglob("*.txt"):
        hit = bool(d.pattern.search(f.read_text()))
        assert hit == (f == SCREENS / fixture), f


def test_dialog_answers_and_persistence():
    copilot_trust = K.adapter("copilot").dialogs[0]
    assert copilot_trust.keys == ("enter",) and not copilot_trust.persisted
    assert K.adapter("codex").dialogs[0].keys is None  # never answered: it would write config.toml
    claude = {d.name: d for d in K.adapter("claude").dialogs}
    assert claude["trust"].keys == ("down", "enter") and claude["trust"].persisted
    assert claude["api_key"].keys == ("up", "enter")
    assert claude["bypass_permissions"].category == K.FULL_AUTO_CONFIRM


def test_config_env_families():
    assert K.is_config_env("COPILOT_ALLOW_ALL") and K.is_config_env("CODEX_HOME")
    assert K.is_config_env("CLAUDE_CONFIG_DIR") and K.is_config_env("CLAUDE_CODE_USE_BEDROCK")
    assert not K.is_config_env("GH_TOKEN") and not K.is_config_env("ANTHROPIC_MODEL")


# ---- reserved options and per-policy turn attribution (task 4.5) ------------------------------


def _options(argv):
    return {a.split("=", 1)[0] for a in argv if a.startswith("-")}


@pytest.mark.parametrize("kind", K.KINDS)
def test_reserved_options_cover_every_generated_full_auto_option(kind):
    """Derived from the table's own arguments, not from a second list."""
    a = K.adapter(kind)
    generated = _options(a.native_args("full_auto", model="m", session_id=SID))
    assert generated <= set(a.reserved_options), generated - set(a.reserved_options)


def test_reserved_session_identity_and_run_mode_options():
    assert {"--resume", "-r", "--continue", "-c", "--fork-session", "--print", "-p"} <= set(K.adapter("claude").reserved_options)
    assert {"--resume", "-r", "--continue", "--prompt", "-p", "--interactive", "-i", "--available-tools"} <= set(
        K.adapter("copilot").reserved_options)
    assert "-c" not in K.adapter("codex").reserved_options  # codex `-c` stays a normal option (D8)


def test_copilot_turn_attribution_is_stated_per_policy():
    a = K.adapter("copilot")
    assert a.capability(K.turn_attribution("read_only")).state == K.VERIFIED
    fa = a.capability(K.turn_attribution("full_auto"))
    assert fa.state == K.VERIFIED and fa.task == "13.10" and "async" in fa.note
    assert all(K.turn_attribution(p) in x.capabilities for x in K.ADAPTERS.values() for p in ("read_only", "full_auto"))


def test_claude_and_copilot_variable_families():
    assert K.adapter("claude").config_env_prefixes == ("CLAUDE_",)
    assert {"USE_BUILTIN_RIPGREP", "USE_TGREP"} <= set(K.adapter("copilot").config_env_names)
    assert K.is_config_env("CLAUDE_FOO") and K.is_config_env("USE_TGREP")


def test_claude_lists_after_task_13_2():
    """Task 13.2 (local stub): Skill can run in the background; the plan-mode pair waits for the user;
    DesignSync writes remote state. Denied under the policies that need it."""
    def denied(policy):
        return _list(args("claude", policy), "--disallowedTools")
    for policy in ("full_auto", "read_only"):
        d = denied(policy)
        assert {"Skill", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion"} <= set(d)
    assert "DesignSync" in denied("read_only") and "DesignSync" not in denied("full_auto")
    assert "ReportFindings" in denied("read_only") and "ReportFindings" not in denied("full_auto")  # 13.33


def test_claude_read_only_names_its_built_in_tools():
    """Task 13.33: read_only offers only Read, Grep and Glob of the built-in set (a new built-in tool is
    not available by default); full_auto keeps the default set and its deny list."""
    ro = args("claude", "read_only", session_id=SID, allow_extra=("mcp__stub__ping",))
    assert _list(ro, "--tools") == ["Read,Grep,Glob"]
    assert _list(ro, "--allowedTools") == ["Read", "Grep", "Glob", "mcp__stub__ping"]  # MCP via the allow list
    assert "--tools" not in args("claude", "full_auto", session_id=SID)
    for p in ("read_only", "full_auto"):
        assert K.adapter("claude").capability(K.turn_attribution(p)).state == K.VERIFIED
