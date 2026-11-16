"""Tasks 4.1 / 4.2: AgentSpec static validation and ClaudeProvider (design D7, D8, D16).

Endpoints and credentials below are placeholders (example.com, fake tokens): provider data only ever
comes from the caller (tests/test_no_hardcoded_llm.py).
"""

import dataclasses
import json
import os
import pickle

import pytest

from polyagent import kinds as K
from polyagent.errors import InvalidConfigError
from polyagent.spec import AgentSpec, ClaudeProvider, ToolPolicy

TOKEN = "fake-token-0123456789"
ENDPOINT = "https://llm.example.com/anthropic"


# ---- 4.1 AgentSpec ------------------------------------------------------------------


def test_unsupported_kind():
    """Scenario: 未支持的种类."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="foo")


def test_cursor_agent_alias_and_known_kinds():
    assert AgentSpec(kind="cursor-agent").kind == "cursor"
    assert {AgentSpec(kind=k).kind for k in K.KINDS} == set(K.KINDS)


@pytest.mark.parametrize("name", ["1BAD", "A-B", "A B", "", "Ä"])
def test_invalid_env_name(name):
    """Scenario: 非法变量名."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", env={name: "x"})


def test_env_values_must_be_strings_without_nul():
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", env={"A": 1})
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", env={"A": "x\0y"})


@pytest.mark.parametrize("model", ["m; touch /tmp/x", "$(id)", "a b", "`x`", "m\n", ""])
def test_model_with_shell_metacharacters(model):
    """Scenario: 含 shell 元字符的模型名 — rejected while building the spec, before any herdr call."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", model=model)


@pytest.mark.parametrize("model", ["gpt-5", "claude-haiku-4.5", "us.anthropic.claude:0", "org/model@v1", "m[effort=high],x+y"])
def test_model_names_accepted(model):
    assert AgentSpec(kind="codex", model=model).model == model


@pytest.mark.parametrize("arg", ["a\nb", "x\0", "\x1b[31m"])
def test_extra_args_with_control_characters(arg):
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", extra_args=[arg])


def test_extra_args_must_be_a_sequence():
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", extra_args="--foo")


@pytest.mark.parametrize("entry", ["docs", "docs/search", "a.b-c_d", "srv/tool.v2"])
def test_mcp_allow_entries(entry):
    assert AgentSpec(kind="claude", policy="read_only", mcp_allow=[entry]).mcp_allow == (entry,)


@pytest.mark.parametrize("entry", ["", "a b", "a/b/c", "a/", "/b", "mcp:x", 3])
def test_bad_mcp_allow_entries(entry):
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="read_only", mcp_allow=[entry])


def test_default_policy_is_decided_by_context():
    """Scenario: 默认策略 — unset policy: full_auto for a lone start, read_only for discussion members."""
    spec = AgentSpec(kind="codex")
    assert spec.policy is None
    assert spec.effective_policy("full_auto") == "full_auto"
    assert spec.resolve("full_auto").policy == "full_auto"
    assert spec.resolve(ToolPolicy.READ_ONLY).policy == "read_only"
    assert AgentSpec(kind="codex", policy=ToolPolicy.READ_ONLY).resolve("full_auto").policy == "read_only"


def test_unknown_policy():
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", policy="yolo")


def test_allow_list_under_explicit_full_auto():
    """Scenario: 完全自动下提供白名单 (explicit)."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="full_auto", mcp_allow=["docs"])


def test_allow_list_under_default_full_auto():
    """Scenario: 完全自动下提供白名单 (default policy of a lone start)."""
    spec = AgentSpec(kind="claude", mcp_allow=["docs"])
    with pytest.raises(InvalidConfigError):
        spec.resolve("full_auto")
    assert spec.resolve("read_only").mcp_allow == ("docs",)


def test_mcp_config_only_for_read_only_claude(tmp_path):
    f = tmp_path / "mcp.json"
    f.write_text("{}")
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="copilot", mcp_config=str(f))
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="full_auto", mcp_config=str(f))
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", mcp_config=str(f)).resolve("full_auto")
    spec = AgentSpec(kind="claude", policy="read_only", mcp_allow=["docs"], mcp_config=str(f))
    assert spec.mcp_config == str(f)


def test_mcp_config_must_be_a_readable_regular_file(tmp_path):
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="read_only", mcp_allow=["docs"], mcp_config=str(tmp_path / "missing.json"))
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="read_only", mcp_allow=["docs"], mcp_config=str(tmp_path))  # a directory
    f = tmp_path / "unreadable.json"
    f.write_text("{}")
    f.chmod(0)
    try:
        if not os.access(f, os.R_OK):  # root can read anything
            with pytest.raises(InvalidConfigError):
                AgentSpec(kind="claude", policy="read_only", mcp_allow=["docs"], mcp_config=str(f))
    finally:
        f.chmod(0o600)
    rel = AgentSpec(kind="claude", policy="read_only", mcp_allow=["docs"], mcp_config=os.path.relpath(f)).mcp_config
    assert rel == str(f)


def test_provider_only_for_claude():
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", provider=ClaudeProvider.official())
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", provider={"type": "official"})


def test_env_cannot_carry_provider_variables_next_to_a_provider():
    p = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN)
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", provider=p, env={"ANTHROPIC_CUSTOM_HEADERS": "x: y"})
    AgentSpec(kind="claude", env={"ANTHROPIC_CUSTOM_HEADERS": "x: y"})  # no provider: caller's own business


def test_auto_trust_type():
    assert AgentSpec(kind="codex").auto_trust is True
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", auto_trust="no")


def test_spec_repr_hides_values_and_pickles():
    spec = AgentSpec(kind="claude", env={"GH_TOKEN": TOKEN}, provider=ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN))
    assert TOKEN not in repr(spec) and "GH_TOKEN" in repr(spec) and ENDPOINT not in repr(spec)
    again = pickle.loads(pickle.dumps(spec))
    assert again == spec and again.env["GH_TOKEN"] == TOKEN and again.provider.env() == spec.provider.env()
    with pytest.raises(TypeError):
        spec.env["X"] = "y"  # type: ignore[index]
    assert TOKEN in spec.secret_values()


# ---- 4.2 ClaudeProvider ---------------------------------------------------------------


def test_official_provider_exports_nothing():
    p = ClaudeProvider.official()
    assert p.env() == {} and p.is_official and p.to_dict() == {"type": "official"}


def test_compatible_provider():
    p = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, model="m1", small_fast_model="m2")
    assert p.env() == {
        "ANTHROPIC_BASE_URL": ENDPOINT,
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_MODEL": "m1",
        "ANTHROPIC_SMALL_FAST_MODEL": "m2",
    }
    k = ClaudeProvider.compatible(ENDPOINT, api_key=TOKEN)
    assert set(k.env()) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"}
    assert p.secret_values() == (TOKEN,)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"auth_token": TOKEN, "api_key": TOKEN}, {"auth_token": ""}, {"auth_token": TOKEN, "model": "a b"}],
)
def test_compatible_provider_validation(kwargs):
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.compatible(ENDPOINT, **kwargs)


@pytest.mark.parametrize("url", ["", "llm.example.com", "ftp://x.example.com", "https://x.example.com\n"])
def test_compatible_provider_url(url):
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.compatible(url, auth_token=TOKEN)


def test_bedrock_provider():
    p = ClaudeProvider.bedrock("us-east-1", access_key_id="AKIDEXAMPLE", secret_access_key=TOKEN, model="m1")
    assert p.env() == {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": TOKEN,
        "ANTHROPIC_MODEL": "m1",
    }
    assert ClaudeProvider.bedrock("eu-west-1", profile="dev").env()["AWS_PROFILE"] == "dev"
    assert "AWS_BEARER_TOKEN_BEDROCK" in ClaudeProvider.bedrock("eu-west-1", bearer_token=TOKEN).env()
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.bedrock("us-east-1", access_key_id="AKIDEXAMPLE")
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.bedrock("us-east-1", session_token=TOKEN)


def test_vertex_provider():
    p = ClaudeProvider.vertex("us-east5", "proj-1", credentials_file="/x/sa.json", model="m1")
    assert p.env() == {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLOUD_ML_REGION": "us-east5",
        "ANTHROPIC_VERTEX_PROJECT_ID": "proj-1",
        "GOOGLE_APPLICATION_CREDENTIALS": "/x/sa.json",
        "ANTHROPIC_MODEL": "m1",
    }


def test_explicit_provider_clears_every_provider_family():
    """Scenarios: 继承环境不干扰 / 继承的自定义请求头 — the clear rules cover the backend switches,
    credentials and custom headers of every provider type."""
    prefixes, names = ClaudeProvider.clear_rules()
    inherited = ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_BASE_URL",
                 "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "AWS_PROFILE", "AWS_SECRET_ACCESS_KEY",
                 "GOOGLE_APPLICATION_CREDENTIALS", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID"]
    assert all(n.startswith(prefixes) or n in names for n in inherited)
    for p in (ClaudeProvider.official(), ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN),
              ClaudeProvider.bedrock("us-east-1", profile="dev"), ClaudeProvider.vertex("r", "p")):
        assert all(n.startswith(prefixes) or n in names for n in p.env())  # everything it sets is also cleared first


def test_two_providers_do_not_share_state():
    """Scenario: 多实例不同 provider."""
    a = ClaudeProvider.bedrock("us-east-1", profile="dev")
    b = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN)
    assert "CLAUDE_CODE_USE_BEDROCK" not in b.env() and "ANTHROPIC_BASE_URL" not in a.env()
    env = a.env()
    env["X"] = "1"
    assert "X" not in a.env()


def test_serialisation_has_no_credentials():
    for p in (ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, model="m1"),
              ClaudeProvider.bedrock("us-east-1", access_key_id="AKIDEXAMPLE", secret_access_key=TOKEN),
              ClaudeProvider.vertex("r", "p", credentials_file="/x/sa.json")):
        d = p.to_dict()
        assert set(d) == {"type"} and d["type"] in ("compatible", "bedrock", "vertex")
        assert TOKEN not in repr(p) and ENDPOINT not in repr(p) and "AKIDEXAMPLE" not in repr(p)


# ---- phase-3 review regressions --------------------------------------------------------


def test_mcp_config_without_allow_list_is_an_error(tmp_path):
    """Review C6: under read_only the file would be silently ignored."""
    f = tmp_path / "mcp.json"
    f.write_text("{}")
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="read_only", mcp_config=str(f))
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", mcp_config=str(f)).resolve("read_only")


@pytest.mark.parametrize("model", ["--dangerously-skip-permissions", "-m", "-"])
def test_model_must_not_start_with_a_dash(model):
    """Review C4: `--model --x` may be parsed as an option by the CLI."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", model=model)
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, model=model)
    assert AgentSpec(kind="claude", model="m-1").model == "m-1"


@pytest.mark.parametrize("entry", ["a__b", "a/b__c", "x__/y", "__"])
def test_mcp_names_with_double_underscore(entry):
    """Review C5: `a__b` (server) and `a/b` (tool) would both become `mcp__a__b`."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="read_only", mcp_allow=[entry])
    assert AgentSpec(kind="claude", policy="read_only", mcp_allow=["a_b", "a/b_c"]).mcp_allow == ("a_b", "a/b_c")


@pytest.mark.parametrize(
    "ptype,env",
    [
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "CLAUDE_CONFIG_DIR": "/x"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "BASH_ENV": "/x"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "LD_PRELOAD": "/x"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "CLAUDE_CODE_FOO": "1"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_CUSTOM_HEADERS": "a b"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_CUSTOM_HEADERS": "a b: c"}),
        ("bedrock", {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "r", "ANTHROPIC_BASE_URL": ENDPOINT}),
        ("official", {"ANTHROPIC_API_KEY": TOKEN}),
    ],
)
def test_direct_construction_cannot_carry_foreign_variables(ptype, env):
    """Review C2: only the variables the type's class method would produce are accepted."""
    with pytest.raises(InvalidConfigError):
        ClaudeProvider(ptype, env)


@pytest.mark.parametrize(
    "ptype,env",
    [
        ("bedrock", {}),
        ("bedrock", {"AWS_REGION": "r"}),
        ("bedrock", {"CLAUDE_CODE_USE_BEDROCK": "0", "AWS_REGION": "r"}),
        ("vertex", {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "r"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT}),
        ("compatible", {"ANTHROPIC_AUTH_TOKEN": TOKEN}),
        ("compatible", {"ANTHROPIC_BASE_URL": "not a url", "ANTHROPIC_AUTH_TOKEN": TOKEN}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN, "ANTHROPIC_MODEL": "a b"}),
        ("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": "x\ny"}),
    ],
)
def test_direct_construction_needs_the_backend_selection(ptype, env):
    """Review C2: an empty or half-filled non-official provider is not an explicit backend."""
    with pytest.raises(InvalidConfigError):
        ClaudeProvider(ptype, env)


def test_direct_construction_equals_the_class_method():
    env = {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN}
    assert ClaudeProvider("compatible", env) == ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN)


def test_asdict_does_not_render_credentials():
    """Review C3: the common `json.dumps(asdict(spec), default=str)` logging idiom leaks nothing."""
    p = ClaudeProvider.bedrock("us-east-1", access_key_id="AKIDEXAMPLE", secret_access_key=TOKEN, session_token="sess-" + TOKEN)
    spec = AgentSpec(kind="claude", provider=p, env={"GH_TOKEN": "gh-" + TOKEN})
    dumped = json.dumps(dataclasses.asdict(spec), default=str)
    assert TOKEN not in dumped and '"provider": {"type": "bedrock"}' in dumped
    assert set(p.secret_values()) == {TOKEN, "sess-" + TOKEN}
    assert set(spec.secret_values()) == {TOKEN, "sess-" + TOKEN, "gh-" + TOKEN}


def test_spec_does_not_import_kinds():
    """Review C10: `spec` sits below `kinds`; the kind list lives in `_rules`."""
    import inspect

    import polyagent.spec as S
    from polyagent import _rules

    assert S.KINDS is _rules.KINDS is K.KINDS
    assert "from .kinds" not in inspect.getsource(S) and "import kinds" not in inspect.getsource(S)


# ---- phase-3 second rework (design notes items 2, 5, 6) ------------------------------------


@pytest.mark.parametrize("model", ["@file", "[x]", "=x", "+x", ".x", "/x", ":x", ",x", "_x"])
def test_model_must_start_with_a_letter_or_digit(model):
    """D8: `@` may mean "read arguments from a file" in some parsers; only alphanumerics may lead."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="codex", model=model)
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, small_fast_model=model)
    assert AgentSpec(kind="codex", model="9b@x").model == "9b@x"


@pytest.mark.parametrize("entry", ["_a", "a_", "a/_b", "a_/b", "a/b_"])
def test_mcp_names_with_edge_underscores(entry):
    """D8 / scenario 有歧义的名字: server `a_` + tool `b` and server `a` + tool `_b` both give `mcp__a___b`."""
    with pytest.raises(InvalidConfigError):
        AgentSpec(kind="claude", policy="read_only", mcp_allow=[entry])


HEADER_VALUE = "hv-" + TOKEN


def test_custom_header_is_exported_as_one_line():
    """Scenario: 自定义请求头 (static part) — the variable content of a provider with a header."""
    p = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, custom_headers={"X-Example-Header": HEADER_VALUE})
    assert p.env()["ANTHROPIC_CUSTOM_HEADERS"] == f"X-Example-Header: {HEADER_VALUE}"
    assert HEADER_VALUE in p.secret_values() and TOKEN in p.secret_values()
    assert "ANTHROPIC_CUSTOM_HEADERS" not in ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN).env()


@pytest.mark.parametrize(
    "headers",
    [{"X A": "v"}, {"X:A": "v"}, {"": "v"}, {"X-A": ""}, {"X-A": "  "}, {"X-A": "v\r\nX-B: w"}, {"X-A": "v\x00"},
     {"X-A": 1}, {}, ["X-A", "v"]],
)
def test_custom_header_validation(headers):
    with pytest.raises(InvalidConfigError):
        ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, custom_headers=headers)


def test_several_custom_headers_one_per_line():
    """Task 13.12 (verified with the local stub): several headers, values with colons and spaces verbatim."""
    p = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN,
                                  custom_headers={"X-A": "1", "X-B": "v:with colon and  spaces", "X-C": HEADER_VALUE})
    assert p.env()["ANTHROPIC_CUSTOM_HEADERS"] == f"X-A: 1\nX-B: v:with colon and  spaces\nX-C: {HEADER_VALUE}"
    assert {"v:with colon and  spaces", HEADER_VALUE} <= set(p.secret_values())
    direct = ClaudeProvider("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN,
                                           "ANTHROPIC_CUSTOM_HEADERS": "X-A: 1\nX-B: 2"})
    assert direct == ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, custom_headers={"X-A": "1", "X-B": "2"})


@pytest.mark.parametrize("text", ["X-A: 1\r\nX-B: 2", "X-A: 1\nX-B: 2\x00", "X-A: 1\n\nX-B: 2", "X-A: 1\nbad line",
                                  "X-A: 1\nX-B: \x1b[0m"])
def test_directly_constructed_header_lines_are_still_checked(text):
    with pytest.raises(InvalidConfigError):
        ClaudeProvider("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN,
                                      "ANTHROPIC_CUSTOM_HEADERS": text})


def test_custom_headers_only_for_compatible():
    with pytest.raises(InvalidConfigError):
        ClaudeProvider("bedrock", {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "r", "ANTHROPIC_CUSTOM_HEADERS": "X-A: v"})
    p = ClaudeProvider("compatible", {"ANTHROPIC_BASE_URL": ENDPOINT, "ANTHROPIC_AUTH_TOKEN": TOKEN,
                                      "ANTHROPIC_CUSTOM_HEADERS": "X-A: v1"})
    assert p == ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, custom_headers={"X-A": "v1"})


def test_text_and_dict_forms_carry_no_header_value_or_endpoint():
    """Scenario: 文本表示与序列化不含凭据 (with a custom header)."""
    p = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, custom_headers={"X-Example-Header": HEADER_VALUE})
    spec = AgentSpec(kind="claude", provider=p)
    for text in (repr(spec), str(spec), repr(p), json.dumps(dataclasses.asdict(spec), default=str), json.dumps(p.to_dict())):
        assert TOKEN not in text and HEADER_VALUE not in text and "llm.example.com" not in text


# ---- phase-3 re-review regressions (N4, N5) --------------------------------------------------------


def test_asdict_of_a_provider_is_the_type_name_only():
    """Review N4 / D7: even an encoder that expands every mapping finds no variables."""
    p = ClaudeProvider.compatible(ENDPOINT, auth_token=TOKEN, custom_headers={"X-Example-Header": HEADER_VALUE})
    assert dataclasses.asdict(p) == {"type": "compatible"}
    assert dataclasses.asdict(AgentSpec(kind="claude", provider=p))["provider"] == {"type": "compatible"}
    again = pickle.loads(pickle.dumps(p))
    assert again == p and again.env() == p.env() and hash(again) == hash(p)
    assert p != ClaudeProvider.compatible(ENDPOINT, auth_token="other-" + TOKEN)
    for ptype in ("compatible", "official", "bedrock"):
        with pytest.raises(InvalidConfigError):
            dataclasses.replace(p, type=ptype)  # the variables cannot be dropped or swapped silently


@pytest.mark.parametrize(
    "url",
    ["https://user:secret-pass@llm.example.com/v1", "https://user@llm.example.com", "https://llm.example.com/v1?api-key=abc",
     "https://llm.example.com/v1?", "https://llm.example.com/#frag"],
)
def test_endpoint_url_must_not_carry_credentials(url):
    """Review N5: user info or a query string would carry secrets past secret_values()."""
    with pytest.raises(InvalidConfigError) as ei:
        ClaudeProvider.compatible(url, auth_token=TOKEN)
    assert "secret-pass" not in str(ei.value) and "abc" not in str(ei.value)
    with pytest.raises(InvalidConfigError):
        ClaudeProvider("compatible", {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": TOKEN})
    assert ClaudeProvider.compatible("http://127.0.0.1:8080/anthropic", auth_token=TOKEN)
