"""Tasks 5.1–5.3: tool-policy mapping, capability refusal, extra-arg/env checks, repo-level config (design D8).

All checks run before any herdr resource exists; these tests never touch herdr (the autouse safety
net would fail any Popen of herdr).
"""

import dataclasses

import pytest

from polyagent import kinds as K
from polyagent import policy as P
from polyagent.errors import InvalidConfigError, PolicyNotSupportedError
from polyagent.spec import AgentSpec, ClaudeProvider

TOKEN = "fake-token-0123456789"
PROVIDER = ClaudeProvider.compatible("https://llm.example.com", auth_token=TOKEN, model="m1")
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "pkg" / "mod"
    sub.mkdir(parents=True)
    return tmp_path


def ro(kind, **kw):
    if kind == "claude":
        kw.setdefault("provider", PROVIDER)
    return AgentSpec(kind=kind, policy="read_only", **kw)


# ---- 5.1 mapping and capability refusal ---------------------------------------------


@pytest.mark.parametrize("kind", K.KINDS)
def test_default_policy_generates_full_auto_args(kind, repo):
    """Scenario: 默认策略 — no policy given to a lone start -> full_auto arguments."""
    spec = P.check_static(AgentSpec(kind=kind), repo)
    assert spec.policy == "full_auto"
    argv = P.build_args(spec, repo, session_id=SID)
    full = {"codex": "--dangerously-bypass-approvals-and-sandbox", "copilot": "--yolo",
            "claude": "--dangerously-skip-permissions"}[kind]
    assert full in argv


def test_explicit_read_only_args(repo):
    """Scenario: 显式只读 (arguments); 只开放只读工具 for claude."""
    argv = P.build_args(P.check_static(ro("claude"), repo), repo, session_id=SID)
    assert "--restricted" in argv and "--dangerously-skip-permissions" not in argv
    i = argv.index("--allowedTools")
    assert argv[i + 1:i + 4] == ["Read", "Grep", "Glob"]
    for tool in ("Edit", "Write", "Bash", "WebFetch", "WebSearch"):
        assert tool in argv[argv.index("--disallowedTools"):]
    argv = P.build_args(P.check_static(ro("copilot"), repo), repo, session_id=SID)
    assert "--available-tools=view,grep,glob" in argv and "--yolo" not in argv


def test_discussion_default_is_read_only(repo):
    spec = P.check_static(AgentSpec(kind="claude", provider=PROVIDER), repo, default_policy="read_only")
    assert spec.policy == "read_only"


@pytest.mark.parametrize("kind", ["copilot", "claude"])
def test_library_adds_no_shell_allow_rule(kind, repo):
    """Scenario: 库不放行 shell 命令."""
    argv = P.build_args(P.check_static(ro(kind), repo), repo, session_id=SID)
    assert not any(a.startswith(("--allow-tool", "--allow-all", "--allow-url", "--yolo")) for a in argv)
    if kind == "claude":
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        assert not any(a.startswith("Bash") for a in allowed)


def test_unsupported_read_only_is_refused_with_its_reason(repo, monkeypatch):
    """Scenarios: 不支持只读的 kind / 只能靠对话框拒绝的 kind. No v1 kind has read_only marked unsupported,
    so the state is put on an adapter here."""
    a = K.adapter("codex")
    note = "web search can only be refused by a running dialog"
    caps = dict(a.capabilities, **{K.READ_ONLY: K.Capability(K.UNSUPPORTED, note=note)})
    monkeypatch.setitem(K.ADAPTERS, "codex", dataclasses.replace(a, capabilities=caps))
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("codex"), repo)
    assert "not supported" in str(ei.value) and "not yet verified" not in str(ei.value)
    assert note in str(ei.value)


def test_codex_read_only_is_unverified_with_task(repo):
    """Scenario: 尚未验证的能力 (codex read_only until task 13.4)."""
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("codex"), repo)
    assert "not yet verified" in str(ei.value) and "13.4" in str(ei.value)


def test_copilot_read_only_is_verified_after_13_1(repo):
    assert P.check_static(ro("copilot"), repo).policy == "read_only"


def test_copilot_read_only_refused_while_unverified(repo, monkeypatch):
    """The behaviour before 13.1 (state driven by the table, not hard-coded)."""
    a = K.adapter("copilot")
    caps = dict(a.capabilities, **{K.READ_ONLY: K.Capability(K.UNVERIFIED, "13.1")})
    monkeypatch.setitem(K.ADAPTERS, "copilot", dataclasses.replace(a, capabilities=caps))
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("copilot"), repo)
    assert "13.1" in str(ei.value)


def test_unverified_mcp_allow_list(repo):
    """Scenario: 尚未验证的能力 — copilot MCP allow-list (task 13.3)."""
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("copilot", mcp_allow=["docs"]), repo)
    assert "not yet verified" in str(ei.value) and "13.3" in str(ei.value)


@pytest.mark.parametrize("kind", ["codex"])
def test_unsupported_mcp_allow_list(kind, repo, monkeypatch):
    a = K.adapter(kind)
    caps = dict(a.capabilities, **{K.READ_ONLY: K.Capability(K.VERIFIED)})  # isolate the MCP check
    monkeypatch.setitem(K.ADAPTERS, kind, dataclasses.replace(a, capabilities=caps, read_only_args=("--x",)))
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro(kind, mcp_allow=["docs"]), repo)
    assert "MCP" in str(ei.value) and "not supported" in str(ei.value)


def test_tool_level_allow_list_on_server_level_kind(repo, monkeypatch):
    """Scenario: 粒度不支持 — a kind with only server-level MCP control gets a tool-level entry."""
    a = K.adapter("copilot")
    caps = dict(a.capabilities, **{K.MCP_ALLOW: K.Capability(K.VERIFIED)})
    monkeypatch.setitem(K.ADAPTERS, "copilot", dataclasses.replace(a, capabilities=caps))
    P.check_static(ro("copilot", mcp_allow=["docs"]), repo)  # server level is fine
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("copilot", mcp_allow=["docs/search"]), repo)


@pytest.mark.parametrize("provider", [None, ClaudeProvider.official()])
def test_claude_read_only_needs_explicit_non_official_provider(provider, repo):
    """Scenario: 只靠用户设置登录."""
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(AgentSpec(kind="claude", policy="read_only", provider=provider), repo)
    assert "provider" in str(ei.value)


@pytest.mark.parametrize("provider", [PROVIDER, ClaudeProvider.bedrock("us-east-1", profile="dev"),
                                      ClaudeProvider.vertex("us-east5", "proj-1")])
def test_claude_read_only_with_explicit_provider(provider, repo):
    """Scenario: 显式提供凭据 (static part)."""
    assert P.check_static(AgentSpec(kind="claude", policy="read_only", provider=provider), repo).policy == "read_only"


def test_claude_allow_list_needs_mcp_config(repo, tmp_path):
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("claude", mcp_allow=["docs"]), repo)
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    spec = P.check_static(ro("claude", mcp_allow=["docs", "web/get"], mcp_config=str(cfg)), repo)
    argv = P.build_args(spec, repo, session_id=SID)
    assert argv[argv.index("--mcp-config") + 1] == str(cfg)
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert allowed == ["Read", "Grep", "Glob", "mcp__docs", "mcp__web__get"]
    assert "{}" not in argv  # only the path, never the file content


def test_full_auto_allow_list_is_a_config_error(repo):
    """Scenario: 完全自动下提供白名单 (explicit and default)."""
    with pytest.raises(InvalidConfigError) as ei:
        P.check_static(AgentSpec(kind="claude", mcp_allow=["docs"]), repo)
    assert not isinstance(ei.value, PolicyNotSupportedError)


def test_build_args_needs_resolved_spec(repo):
    with pytest.raises(ValueError):
        P.build_args(AgentSpec(kind="codex"), repo)


def test_trust_arguments(repo):
    codex = P.build_args(P.check_static(AgentSpec(kind="codex"), repo), repo)
    i = codex.index("-c")
    assert codex[i + 1] == 'projects={"%s"={trust_level="trusted"}}' % repo


def test_read_only_hides_injected_variable_names(repo):
    argv = P.build_args(P.check_static(ro("copilot", env={"GH_TOKEN": TOKEN, "A_KEY": "v"}), repo), repo)
    assert "--secret-env-vars=A_KEY,GH_TOKEN" in argv and TOKEN not in " ".join(argv)
    fa = P.build_args(P.check_static(AgentSpec(kind="copilot", env={"GH_TOKEN": TOKEN}), repo), repo)
    assert not any(a.startswith("--secret-env-vars") for a in fa)


def test_credentials_never_in_args(repo):
    for spec in (ro("claude", env={"X_TOKEN": TOKEN}), AgentSpec(kind="claude", provider=PROVIDER)):
        argv = P.build_args(P.check_static(spec, repo), repo, session_id=SID)
        assert TOKEN not in " ".join(argv) and "llm.example.com" not in " ".join(argv)


# ---- 5.2 extra args and env names ----------------------------------------------------


@pytest.mark.parametrize("extra", [["--yolo"], ["--allow-all-tools"], ["--allow-tool", "shell"], ["--reasoning-effort"],
                                   ["--reasoning-effort", "insane"], ["positional"]])
def test_read_only_extra_args_that_widen_permissions(extra, repo):
    """Scenario: 附加参数放宽权限."""
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("copilot", extra_args=extra), repo)


@pytest.mark.parametrize("kind,extra", [("copilot", ["--reasoning-effort", "high"]), ("copilot", ["--reasoning-effort=low"]),
                                        ("claude", ["--effort", "max"])])
def test_read_only_whitelisted_extra_args(kind, extra, repo):
    argv = P.build_args(P.check_static(ro(kind, extra_args=extra), repo), repo)
    assert argv[:len(extra)] == extra


@pytest.mark.parametrize("extra", [["--dangerously-skip-permissions"], ["--permission-mode", "bypassPermissions"],
                                   ["--settings", "/x.json"], ["--effort", "high", "--add-dir", "/"]])
def test_read_only_claude_extra_args(extra, repo):
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("claude", extra_args=extra), repo)


@pytest.mark.parametrize("kind", K.KINDS)
def test_extra_args_unrestricted_under_full_auto(kind, repo):
    """Scenario: 附加参数不受限."""
    argv = P.build_args(P.check_static(AgentSpec(kind=kind, extra_args=["--some-unlisted-flag", "v"]), repo), repo)
    assert argv[:2] == ["--some-unlisted-flag", "v"]


@pytest.mark.parametrize(
    "kind,name",
    [("copilot", "COPILOT_ALLOW_ALL"), ("copilot", "COPILOT_HOME"), ("codex", "CODEX_HOME"),
     ("claude", "CLAUDE_CONFIG_DIR"), ("claude", "CLAUDE_CODE_SIMPLE"), ("copilot", "XDG_CONFIG_HOME"),
     ("copilot", "HOME"), ("copilot", "PATH"), ("copilot", "BASH_ENV"), ("copilot", "NODE_OPTIONS"),
     ("copilot", "LD_PRELOAD"), ("copilot", "HERDR_SOCKET_PATH"), ("claude", "SHELL"), ("claude", "ENV")],
)
def test_read_only_env_names_that_widen_or_redirect(kind, name, repo, monkeypatch):
    """Scenarios: 环境变量放宽权限 / 配置目录重定向."""
    if kind == "codex":  # isolate the env check from codex's unverified read_only state
        a = K.adapter("codex")
        caps = dict(a.capabilities, **{K.READ_ONLY: K.Capability(K.VERIFIED)})
        monkeypatch.setitem(K.ADAPTERS, "codex", dataclasses.replace(a, capabilities=caps))
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro(kind, env={name: "x"}), repo)


def test_config_dir_redirect_is_refused_before_capability_for_codex(repo):
    """Scenario: 配置目录重定向 — codex + CODEX_HOME under read_only is refused (policy not supported)."""
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("codex", env={"CODEX_HOME": "/x"}), repo)


@pytest.mark.parametrize("kind,name", [("copilot", "COPILOT_ALLOW_ALL"), ("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR"),
                                       ("copilot", "XDG_CONFIG_HOME")])
def test_env_names_unrestricted_under_full_auto(kind, name, repo):
    assert P.check_static(AgentSpec(kind=kind, env={name: "x"}), repo).policy == "full_auto"


def test_provider_variables_are_not_checked_as_caller_env(repo):
    """Variables generated by the provider are not subject to the read_only name check."""
    spec = P.check_static(ro("claude", provider=ClaudeProvider.bedrock("us-east-1", profile="dev")), repo)
    assert spec.provider.env()["CLAUDE_CODE_USE_BEDROCK"] == "1"


# ---- 5.3 repo-level executable configuration -------------------------------------------


@pytest.mark.parametrize("rel", [".github/hooks/pre.json", ".github/copilot/settings.json", ".mcp.json", ".vscode/mcp.json",
                                 ".claude/settings.json", ".claude/settings.local.json"])
@pytest.mark.parametrize("where", ["root", "cwd"])
def test_copilot_repo_with_config_is_refused(rel, where, repo):
    """Scenario: 含 hooks 的仓库 (and the other five copilot paths, at the repo root or in cwd)."""
    cwd = repo / "pkg" / "mod"
    base = repo if where == "root" else cwd
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("copilot"), cwd)
    assert any(str(p).startswith(str(base / c)) and str(base / c) in str(ei.value)
               for c in K.adapter("copilot").repo_config_paths)


def test_config_above_the_repo_root_is_ignored(tmp_path):
    (tmp_path / ".mcp.json").write_text("{}")
    inner = tmp_path / "inner"
    (inner / ".git").mkdir(parents=True)
    P.check_static(ro("copilot"), inner)


def test_outside_a_repo_only_cwd_is_checked(tmp_path):
    (tmp_path / ".mcp.json").write_text("{}")
    sub = tmp_path / "sub"
    sub.mkdir()
    P.check_static(ro("copilot"), sub)
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("copilot"), tmp_path)


def test_codex_repo_config_is_checked(repo, monkeypatch):
    a = K.adapter("codex")
    caps = dict(a.capabilities, **{K.READ_ONLY: K.Capability(K.VERIFIED)})
    monkeypatch.setitem(K.ADAPTERS, "codex", dataclasses.replace(a, capabilities=caps))
    P.check_static(ro("codex"), repo)
    (repo / ".codex").mkdir()
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("codex"), repo / "pkg")


def test_claude_excludes_project_config_by_arguments(repo):
    """Scenario: 可排除项目级配置 — no path check; the exclusion arguments are present instead."""
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"hooks": {}}')
    (repo / ".mcp.json").write_text("{}")
    argv = P.build_args(P.check_static(ro("claude"), repo), repo)
    assert "--restricted" in argv and "--strict-mcp-config" in argv


@pytest.mark.parametrize("kind", ["copilot", "codex"])
def test_full_auto_does_not_check_repo_config(kind, repo):
    """Scenario: 带仓库级配置的目录."""
    (repo / ".github" / "hooks").mkdir(parents=True)
    (repo / ".codex").mkdir()
    assert P.check_static(AgentSpec(kind=kind), repo).policy == "full_auto"


# ---- phase-3 review regressions --------------------------------------------------------


@pytest.mark.parametrize("target", ["pkg/mod", "pkg", "."])
def test_symlinked_cwd_still_sees_the_repo_root_config(repo, tmp_path_factory, target):
    """Review C1: a cwd that is a symlink into the repository is checked against the real repository."""
    (repo / ".mcp.json").write_text("{}")
    link = tmp_path_factory.mktemp("links") / "link"
    link.symlink_to(repo / target)
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("copilot"), link)
    assert ".mcp.json" in str(ei.value)


def test_symlinked_parent_directory(repo, tmp_path_factory):
    (repo / ".github" / "hooks").mkdir(parents=True)
    link = tmp_path_factory.mktemp("links") / "proj"
    link.symlink_to(repo)
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("copilot"), link / "pkg" / "mod")


def test_config_along_the_literal_path_is_still_checked(repo, tmp_path_factory):
    """The literal chain stays checked: a link inside a configured repo to a directory outside any repo."""
    clean = tmp_path_factory.mktemp("clean")
    (repo / ".mcp.json").write_text("{}")
    (repo / "pkg" / "ext").symlink_to(clean)
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("copilot"), repo / "pkg" / "ext")
    P.check_static(ro("copilot"), clean)


def test_provider_cannot_smuggle_forbidden_variables(repo):
    """Review C2: the only path that carried read_only-forbidden names past check_static."""
    with pytest.raises(InvalidConfigError):
        ClaudeProvider("compatible", {"ANTHROPIC_BASE_URL": "https://llm.example.com", "ANTHROPIC_AUTH_TOKEN": TOKEN,
                                      "CLAUDE_CONFIG_DIR": "/x"})
    with pytest.raises(InvalidConfigError):
        ClaudeProvider("bedrock", {})


@pytest.mark.parametrize("kind", ["codex"])
def test_build_args_refuses_unverified_read_only(kind, repo):
    """Review C6: read_only arguments are never built for a kind whose read_only is not verified."""
    with pytest.raises(PolicyNotSupportedError):
        P.build_args(AgentSpec(kind=kind, policy="read_only"), repo)


def test_build_args_refuses_unverified_mcp_allow_list(repo):
    with pytest.raises(PolicyNotSupportedError):
        P.build_args(AgentSpec(kind="copilot", policy="read_only", mcp_allow=["docs"]), repo)


def test_prefix_refusals_name_the_right_task():
    """Review C8: unverified prefixes name the task that could lift the refusal."""
    assert dict(K.adapter("codex").prompt_guard.rejected) == dict.fromkeys("#$?", "13.8")


# ---- reserved options (task 5.4) and new variable families (task 4.5) ---------------------------


def test_overriding_a_library_option():
    """Scenario: 覆盖库设置的选项 — default policy, another session id for claude."""
    for extra in (["--session-id", SID], [f"--session-id={SID}"]):
        with pytest.raises(InvalidConfigError) as ei:
            P.check_static(AgentSpec(kind="claude", extra_args=extra), ".")
        assert not isinstance(ei.value, PolicyNotSupportedError) and "--session-id" in str(ei.value)


@pytest.mark.parametrize(
    "kind,arg",
    [("claude", "--model=x"), ("claude", "--disallowed-tools"), ("claude", "-r"), ("claude", "-rabc"),
     ("claude", "--fork-session"), ("claude", "-p"), ("claude", "--sess"), ("claude", "--dangerously"),
     ("copilot", "--excluded-tools=bash"), ("copilot", "--available-tools"), ("copilot", "-ihello"), ("copilot", "--no-ask"),
     ("copilot", "--resume"), ("codex", "-mgpt-5"), ("codex", "--no-daemon"), ("codex", "--dangerously-bypass")],
)
def test_reserved_option_matching_rules(kind, arg, repo):
    """D8: exact name, `=` form, attached short value, and abbreviations of reserved long options."""
    with pytest.raises(InvalidConfigError):
        P.check_static(AgentSpec(kind=kind, extra_args=[arg, "v"]), repo)


@pytest.mark.parametrize("kind", K.KINDS)
@pytest.mark.parametrize("policy", [None, "read_only"])
def test_argument_terminator(kind, policy, repo):
    """Scenario: 参数终止符 (checked before the capability state for read_only too)."""
    spec = AgentSpec(kind=kind, policy=policy, extra_args=["--", "hello"],
                     provider=PROVIDER if kind == "claude" else None)
    with pytest.raises(InvalidConfigError):
        P.check_static(spec, repo)
    with pytest.raises(InvalidConfigError):
        P.build_args(dataclasses.replace(spec, policy=policy or "full_auto"), repo)


@pytest.mark.parametrize(
    "kind,extra",
    [("claude", ["--verbose"]), ("copilot", ["--no-color"]), ("codex", ["-c", "model_reasoning_effort=high"]), ("claude", ["--effort", "high"])],
)
def test_ordinary_extra_arguments_pass_unchanged(kind, extra, repo):
    """Scenario: 普通附加参数."""
    argv = P.build_args(P.check_static(AgentSpec(kind=kind, extra_args=extra), repo), repo, session_id=SID)
    assert argv[:len(extra)] == extra


def test_reserved_check_comes_before_the_read_only_whitelist(repo):
    """Under read_only a reserved option is refused as reserved, as PolicyNotSupportedError (spec 附加参数放宽权限)."""
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("copilot", extra_args=["--yolo"]), repo)
    assert "reserves" in str(ei.value)
    with pytest.raises(PolicyNotSupportedError) as ei:
        P.check_static(ro("copilot", extra_args=["--allow-all-tools"]), repo)
    assert "whitelist" in str(ei.value)


def test_build_args_rechecks_reserved_options(repo):
    with pytest.raises(InvalidConfigError):
        P.build_args(AgentSpec(kind="codex", policy="full_auto", extra_args=["--no-daemon"]), repo)


@pytest.mark.parametrize("kind,name", [("claude", "CLAUDE_FOO"), ("claude", "CLAUDE_AGENT_SDK_X"),
                                       ("copilot", "USE_BUILTIN_RIPGREP"), ("copilot", "USE_TGREP"),
                                       # task 13.11: documented variables without the kind's prefix
                                       ("copilot", "GH_HOST"),
                                       ("copilot", "EDITOR"), ("copilot", "VISUAL"), ("claude", "EDITOR"),
                                       ("claude", "NODE_PATH"), ("claude", "USE_BUILTIN_RIPGREP"),
                                       ("claude", "ENABLE_TOOL_SEARCH"), ("claude", "ENABLE_CLAUDEAI_MCP_SERVERS"),
                                       ("claude", "BETA_TRACING_ENDPOINT"), ("claude", "ENABLE_BETA_TRACING_DETAILED"),
                                       ("claude", "FORCE_AUTOUPDATE_PLUGINS")])
def test_new_variable_families_refused_under_read_only(kind, name, repo):
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro(kind, env={name: "1"}), repo)
    assert P.check_static(AgentSpec(kind=kind, env={name: "1"}), repo).policy == "full_auto"


# ---- phase-3 re-review regressions (N1–N3) -------------------------------------------------------


def test_symlink_followed_by_dotdot_checks_the_real_directory(repo, tmp_path_factory):
    """Review N1: `out/link/..` is the repository root when `link` points into the repository;
    abspath would turn it into `out` before resolving the link."""
    (repo / ".mcp.json").write_text("{}")
    out = tmp_path_factory.mktemp("out")
    (out / "link").symlink_to(repo / "pkg")
    with pytest.raises(PolicyNotSupportedError):
        P.check_static(ro("copilot"), str(out / "link") + "/..")


@pytest.mark.parametrize(
    "kind,arg",
    [("copilot", "-sr"), ("copilot", "-sp"), ("copilot", "-si"), ("copilot", "-sir"), ("claude", "-vp"),
     ("codex", "-xm=gpt"), ("codex", "-xC")],
)
def test_clustered_short_options_hiding_a_reserved_one(kind, arg, repo):
    """Review N2: `-sr` is `-s -r` for clap-style parsers."""
    with pytest.raises(InvalidConfigError):
        P.check_static(AgentSpec(kind=kind, extra_args=[arg]), repo)


@pytest.mark.parametrize(
    "kind,arg",
    [("claude", "--from-pr"), ("claude", "--teleport"), ("claude", "--cloud"), ("claude", "--bg"),
     ("claude", "--background"), ("claude", "--desktop"), ("claude", "--worktree"), ("claude", "-w"),
     ("claude", "--remote-control"), ("claude", "--no-session-persistence"), ("copilot", "--acp"), ("codex", "--cd"), ("codex", "-C"), ("codex", "--remote")],
)
def test_more_reserved_session_and_run_mode_options(kind, arg, repo):
    """Review N3: options of the same three classes found in each CLI's --help."""
    with pytest.raises(InvalidConfigError):
        P.check_static(AgentSpec(kind=kind, extra_args=[arg, "x"]), repo)


def test_short_non_reserved_options_still_pass(repo):
    argv = P.build_args(P.check_static(AgentSpec(kind="copilot", extra_args=["-s"]), repo), repo, session_id=SID)
    assert argv[0] == "-s"
