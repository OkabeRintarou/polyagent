import pickle

import pytest

import polyagent
from polyagent import errors as E


def test_every_exported_error_derives_from_base():
    for name in E.__all__:
        cls = getattr(E, name)
        assert issubclass(cls, E.PolyAgentError)
        assert getattr(polyagent, name) is cls


@pytest.mark.parametrize(
    "child,parent",
    [
        (E.InvalidPromptError, E.InvalidConfigError),
        (E.PolicyNotSupportedError, E.InvalidConfigError),
        (E.HerdrUnavailableError, E.HerdrError),
    ],
)
def test_subclass_relations(child, parent):
    assert issubclass(child, parent)


def test_policy_and_prompt_errors_caught_as_invalid_config():
    for exc in (E.PolicyNotSupportedError("p"), E.InvalidPromptError("x")):
        with pytest.raises(E.InvalidConfigError):
            raise exc


def test_unrelated_errors_are_not_config_or_herdr_errors():
    assert not issubclass(E.HerdrNotFoundError, E.HerdrError)
    assert not issubclass(E.AgentTimeoutError, E.HerdrError)
    assert not issubclass(E.HerdrError, E.InvalidConfigError)


def test_herdr_error_fields():
    e = E.HerdrError("agent_not_found", "no such agent", ["herdr", "agent", "get", "x"])
    assert (e.code, e.message, e.argv) == ("agent_not_found", "no such agent", ["herdr", "agent", "get", "x"])
    assert "agent_not_found" in str(e) and "no such agent" in str(e)
    assert E.HerdrError("c", "m").argv == []


def test_herdr_unavailable_error_fields():
    e = E.HerdrUnavailableError("protocol_mismatch", "restart the server")
    assert e.code == "protocol_mismatch" and e.message == "restart the server"


def test_blocked_error_carries_screen():
    e = E.AgentBlockedError("agent rev is blocked", "Allow write? [y/n]", agent="rev")
    assert e.agent == "rev" and e.screen == "Allow write? [y/n]" and str(e) == "agent rev is blocked"
    assert E.AgentBlockedError("blocked", "S").agent is None


def test_timeout_error_fields():
    pending = {"kind": "log", "offset": 12, "prompt": "hi"}
    e = E.AgentTimeoutError("timed out", status="working", diagnostics="still thinking", pending=pending)
    assert e.status == "working" and e.diagnostics == "still thinking" and e.pending == pending
    bare = E.AgentTimeoutError("t")
    assert bare.status is None and bare.diagnostics is None and bare.pending is None


def test_turn_error_and_reply_not_found_fields():
    assert E.AgentTurnError("rate limited").message == "rate limited"
    r = E.ReplyNotFoundError("no echo", screen="SCREEN")
    assert r.screen == "SCREEN" and str(r) == "no echo"


@pytest.mark.parametrize(
    "exc",
    [
        E.HerdrError("c", "m", ["herdr", "x"]),
        E.HerdrUnavailableError("protocol_mismatch", "m", ["herdr"]),
        E.AgentBlockedError("blocked", "screen", agent="rev"),
        E.AgentTimeoutError("t", status="working", diagnostics="d", pending={"offset": 1}),
        E.AgentTurnError("rate limited"),
        E.ReplyNotFoundError("no echo", "screen"),
        E.AgentBusyError("busy"),
        E.InvalidPromptError("bad"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_exceptions_survive_pickling(exc):
    exc.closed = ["a"]  # extra attributes (e.g. cleanup's partial result) survive too
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is type(exc) and str(back) == str(exc)
    assert back.__dict__ == exc.__dict__
