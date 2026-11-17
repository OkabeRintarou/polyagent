"""Task 7.1: prompt sanitisation (design D10), identical under both tool policies."""

import pytest

from fakeclient import FakeClient
from polyagent import _prompt as PR
from polyagent import agent as A
from polyagent import kinds as K
from polyagent.errors import InvalidPromptError

POLICIES = ["full_auto", "read_only"]
INPUT_AREA = {"codex": "› Ask Codex to do anything\n\n  gpt default · /tmp/repo\n"}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(A, "POLL_INTERVAL", 0.005)


def submitted(kind, policy, text, lock_home):
    """What reaches `agent prompt` for ``text``, via a real handle (None if nothing was sent)."""
    c = FakeClient()
    c.workspaces["w1"] = {"workspace_id": "w1", "label": "L"}
    c.add_agent("a", "w1")
    sent = []

    def on_prompt(fake, name, t, wait):
        sent.append(t)
        return H_err_timeout()

    c.on_prompt = on_prompt
    c.recent = INPUT_AREA.get(kind, "")  # screen-capture kinds only submit into a recognized input area (D12)
    agent = A.Agent(c, name="a", kind=kind, policy=policy, workspace_id="w1", pane_id="w1:p1", label="L")
    agent._log_path = None
    try:
        agent.ask(text, 0.3)
    except InvalidPromptError:
        assert not c.args_of("agent_prompt") and not c.args_of("agent_send_keys")
        return None
    except Exception:  # noqa: BLE001 - anything after submission is not under test here
        pass
    return sent[0] if sent else None


def H_err_timeout():
    from fakeclient import herr
    from polyagent import herdr as H

    return herr(H.TIMEOUT, "t")


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind", ["codex", "claude"])  # trusted-status kinds submit through herdr
@pytest.mark.parametrize("text", ["x\x1by", "a\x00b", "a\rb", "a\x7fb", "a\x85b", "\x07"])
def test_control_characters_are_refused(kind, policy, text, lock_home):
    """Scenario: 含控制字符的 prompt — nothing reaches the agent."""
    assert submitted(kind, policy, text, lock_home) is None


def test_crlf_is_normalised():
    assert PR.sanitize(K.adapter("claude"), "a\r\nb") == "a\nb"
    assert PR.sanitize(K.adapter("claude"), "a\tb") == "a\tb"


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_prompt_starting_with_a_dash(kind, policy, lock_home):
    """Scenario: 以连字符开头的 prompt — the text arrives (after the fixed guard) unchanged."""
    out = submitted(kind, policy, "-v please", lock_home)
    assert out is not None and not out.startswith("-")
    assert out.endswith("-v please")
    assert out in (" -v please", f"{K.GUIDE_LINE_TEXT}\n-v please")


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind,text", [("claude", "  /help"), ("claude", "\t!touch x"), ("codex", "  !touch x"),
                                       ("codex", " /exit")])
def test_prefix_after_leading_blanks_is_guarded(kind, policy, text, lock_home):
    """Scenario: 前导空白后的前缀 — judged by the first non-blank character."""
    out = submitted(kind, policy, text, lock_home)
    assert out is not None and out != text and out.endswith(text)


def test_copilot_leading_blanks_get_the_guide_line():
    """copilot strips leading blanks itself, so only the guide line protects `  /help` (D10)."""
    assert PR.sanitize(K.adapter("copilot"), "  /help") == f"{K.GUIDE_LINE_TEXT}\n  /help"


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind,text", [("codex", "#note"), ("codex", "  #note"), ("codex", "$x"), ("codex", "?")])
def test_unguardable_prefix_is_refused(kind, policy, text, lock_home):
    """Scenario: 无法防护的前缀."""
    assert submitted(kind, policy, text, lock_home) is None


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_multiline_prompt_is_one_message(kind, policy, lock_home):
    out = submitted(kind, policy, "first line\n/allow-all\n!touch x", lock_home)
    assert out == "first line\n/allow-all\n!touch x"


@pytest.mark.parametrize("kind", K.KINDS)
def test_every_kind_takes_multi_line_prompts(kind):
    """Task 13.41: all v1 kinds take a multi-line prompt as one message; there is no multi-line refusal."""
    assert PR.sanitize(K.adapter(kind), "first line\nsecond line").endswith("first line\nsecond line")


@pytest.mark.parametrize("policy", POLICIES)
def test_oversized_prompt_is_refused(policy, lock_home):
    assert submitted("claude", policy, "x" * (PR.MAX_PROMPT_BYTES + 1), lock_home) is None
    assert submitted("claude", policy, "é" * (PR.MAX_PROMPT_BYTES // 2 + 1), lock_home) is None  # counted in UTF-8 bytes
    assert PR.sanitize(K.adapter("claude"), "x" * (PR.MAX_PROMPT_BYTES - 64))


def test_copilot_takes_at_most_20_kib():
    """Task 13.14: copilot turns a paste of more than 20480 bytes into a file reference, so such a prompt
    is refused before anything is sent; the limit applies to the text as sent (guide line included)."""
    a = K.adapter("copilot")
    assert PR.sanitize(a, "x" * 20480)
    for text in ("x" * 20481, "é" * 10241, "/" + "x" * (20480 - len(K.GUIDE_LINE_TEXT) - 1)):
        with pytest.raises(InvalidPromptError, match="20480 bytes"):
            PR.sanitize(a, text)
    assert PR.sanitize(a, "/" + "x" * (PR.max_text_bytes(a) - 1))
    assert PR.sanitize(K.adapter("claude"), "x" * 20481)  # other kinds keep the library-wide limit


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_prompt_is_refused(text):
    with pytest.raises(InvalidPromptError):
        PR.sanitize(K.adapter("claude"), text)


@pytest.mark.parametrize("kind", K.KINDS)
@pytest.mark.parametrize("prefix", sorted(K.GUARDED_PREFIXES) + [" ", "\t", "  "])
def test_no_kind_ever_receives_a_leading_bang(kind, prefix):
    for text in (f"{prefix}!touch pwned", f"!{prefix}x", "!"):
        try:
            out = PR.sanitize(K.adapter(kind), text)
        except InvalidPromptError:
            continue
        assert not out.startswith("!") and not out.startswith("-")


@pytest.mark.parametrize("kind", K.KINDS)
def test_sanitising_does_not_depend_on_the_policy(kind):
    """The sanitiser takes no policy; the same table drives both policies."""
    import inspect

    assert list(inspect.signature(PR.sanitize).parameters) == ["adapter", "text"]


# ---- review C1: invisible characters before a prefix --------------------------------------------------

INVISIBLE = ["\ufeff", "\u200b", "\u2060", "\u200e", "\u00a0", "\u3000", "\u2028", " \ufeff "]


@pytest.mark.parametrize("kind", ["copilot", "claude"])
@pytest.mark.parametrize("lead", INVISIBLE)
@pytest.mark.parametrize("prefix", ["!touch pwned", "/exit", "#remember", "-v"])
def test_invisible_characters_do_not_hide_a_prefix(kind, lead, prefix):
    """A BOM (U+FEFF) is whitespace for JavaScript's trim(); so are other format/separator characters."""
    out = PR.sanitize(K.adapter(kind), lead + prefix)
    assert out == f"{K.GUIDE_LINE_TEXT}\n{lead}{prefix}"


@pytest.mark.parametrize("kind", ["codex"])
@pytest.mark.parametrize("lead", INVISIBLE)
def test_leading_space_kinds_guard_a_prefix_after_invisible_characters(kind, lead):
    """D10 step 3: the first effective character decides; the kind's own rule then applies (guard or refuse)."""
    assert PR.sanitize(K.adapter(kind), lead + "!touch pwned") == f" {lead}!touch pwned"
    with pytest.raises(InvalidPromptError):
        PR.sanitize(K.adapter(kind), lead + "#x")


@pytest.mark.parametrize("ch", ["\ufeff", "\u200b", "\u2060", "\u200e", "\u202e", "\u00a0", "\u2003", "\u2028",
                                "\u2029", "\n", "\t"])
def test_every_skipped_category_is_skipped(ch):
    """Zs, Zl, Zp, Cf, newline and tab are skipped; anything else is the first effective character."""
    assert PR.first_effective(ch * 2 + "!x") == "!"


@pytest.mark.parametrize("ch", ["\u3164", "\u2800", "x"])
def test_other_characters_are_effective(ch):
    """Hangul filler and braille blank are Lo/So, not separators: they count as content."""
    assert PR.first_effective(ch + "!x") == ch


@pytest.mark.parametrize("kind", K.KINDS)
def test_invisible_characters_before_plain_text_are_kept(kind):
    assert PR.sanitize(K.adapter(kind), "\ufeffhello") == "\ufeffhello"


@pytest.mark.parametrize("kind", K.KINDS)
def test_only_invisible_characters_is_empty(kind):
    with pytest.raises(InvalidPromptError):
        PR.sanitize(K.adapter(kind), "\ufeff\u200b\u00a0")
