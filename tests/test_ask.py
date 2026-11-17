"""Tasks 7.2 / 7.5: the ask state machine, interrupt, dismiss, wait_reply, cancel and close (D11, D12).

The herdr side is the in-memory ``FakeClient``; turns are written to real JSONL files in the format of
the task-3.x samples.
"""

import json
from pathlib import Path
import threading
import time

import pytest

from fakeclient import FakeClient, append_jsonl, herr, iso
from polyagent import agent as A
from polyagent import herdr as H
from polyagent import kinds as K
from polyagent.capture import prompt_digest
from polyagent.errors import (
    AgentBlockedError,
    AgentBusyError,
    AgentNotFoundError,
    AgentTimeoutError,
    AgentTurnAbortedError,
    AgentTurnError,
    AskCancelledError,
    HerdrError,
    InvalidConfigError,
    PromptNotDeliveredError,
    ReplyNotFoundError,
)


@pytest.fixture(autouse=True)
def fast(monkeypatch, lock_home):
    for name, value in (("POLL_INTERVAL", 0.005), ("SETTLE_GRACE", 0.15), ("CONFIRM_LIMIT", 0.3),
                        ("UNKNOWN_WAIT", 0.2), ("INTERRUPT_WAIT", 0.2), ("DISMISS_WAIT", 0.2)):
        monkeypatch.setattr(A, name, value)


# ---- log writers in the samples' formats ------------------------------------------------


def claude_user(p, text):
    append_jsonl(p, {"type": "user", "message": {"role": "user", "content": text}})


def claude_end(p, reply=None, *, error=None, interrupted=False):
    if interrupted:
        append_jsonl(p, {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
        return
    if error:
        append_jsonl(p, {"type": "assistant", "isApiErrorMessage": True,
                         "message": {"role": "assistant", "stop_reason": "stop_sequence", "content": [{"type": "text", "text": error}]}})
    elif reply is not None:
        append_jsonl(p, {"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
                                                          "content": [{"type": "text", "text": reply}]}})
    append_jsonl(p, {"type": "system", "subtype": "turn_duration", "durationMs": 5})


def copilot_user(p, text, **top):
    append_jsonl(p, {"type": "user.message", "timestamp": iso(), "data": {"content": text}, **top})


def copilot_end(p, reply=None, *, error=None):
    if error:
        append_jsonl(p, {"type": "session.error", "timestamp": iso(), "data": {"message": error}})
        return
    if reply is not None:
        append_jsonl(p, {"type": "assistant.message", "timestamp": iso(), "data": {"content": reply, "toolRequests": []}})
    append_jsonl(p, {"type": "session.usage_checkpoint", "timestamp": iso(), "data": {}})


def later(delay, fn, *args):
    t = threading.Timer(delay, fn, args)
    t.daemon = True
    t.start()
    return t


def make(tmp_path, kind="claude", policy="full_auto", log=True, name="a"):
    c = FakeClient()
    c.workspaces["w1"] = {"workspace_id": "w1", "label": "LABEL"}
    c.add_agent(name, "w1")
    agent = A.Agent(c, name=name, kind=kind, policy=policy, workspace_id="w1", pane_id="w1:p1", label="LABEL",
                    session_id="sid" if kind in ("claude", "copilot") else None)
    path = tmp_path / f"{name}.jsonl"
    if log:
        agent._log_path = str(path)
    else:
        agent._find_log_path = lambda: None  # type: ignore[method-assign]
    return c, agent, path


def answer_claude(path, reply="PONG", status_after="idle"):
    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        claude_end(path, reply)
        fake.set_status(name, status_after)
    return on_prompt


# ---- 7.2 normal flow ------------------------------------------------------------------------


def test_normal_reply_from_the_session_log(tmp_path):
    """Scenario: 正常回复 / 会话日志可用."""
    c, agent, path = make(tmp_path)
    c.on_prompt = answer_claude(path, "**PONG** `x`")
    r = agent.ask_reply("Reply PONG", 5)
    assert r.text == "**PONG** `x`" and r.source == "session_log" and r.duration >= 0
    (_, text, timeout_ms, wait), = c.args_of("agent_prompt")
    assert wait is True and isinstance(timeout_ms, int) and 0 < timeout_ms <= 5000
    assert agent.ask("Reply PONG", 5) == "**PONG** `x`"


def test_reply_finished_before_herdr_saw_work(tmp_path):
    """Scenario: 极快完成的回复 — `agent prompt --wait` returns at once; the log already has the turn."""
    c, agent, path = make(tmp_path)
    c.on_prompt = answer_claude(path, "fast")
    assert agent.ask("q", 5) == "fast"


def test_slow_start_after_a_stalled_prompt(tmp_path):
    """Scenario: 开始工作较慢 — herdr says agent_prompt_stalled; the turn starts later."""
    c, agent, path = make(tmp_path)

    def on_prompt(fake, name, text, wait):
        def start():
            claude_user(path, text)
            fake.set_status(name, "working")
        def finish():
            claude_end(path, "late")
            fake.set_status(name, "idle")
        later(0.1, start)
        later(0.2, finish)
        return herr(H.AGENT_PROMPT_STALLED, "stalled")

    c.on_prompt = on_prompt
    assert agent.ask("q", 5) == "late"
    assert len(c.args_of("agent_prompt")) == 1


def test_unconfirmed_delivery_is_not_resent(tmp_path):
    """Scenario: 未确认投递."""
    c, agent, _ = make(tmp_path)
    c.on_prompt = lambda *a: herr(H.AGENT_PROMPT_STALLED, "stalled")
    with pytest.raises(PromptNotDeliveredError):
        agent.ask("q", 5)
    assert len(c.args_of("agent_prompt")) == 1


def test_agent_error_and_abort_from_the_log(tmp_path):
    """Scenarios: agent 级错误 / 拒绝导致回合中止."""
    c, agent, path = make(tmp_path)
    c.on_prompt = lambda fake, n, t, w: (claude_user(path, t), claude_end(path, error="API Error: 429 rate limited"))
    with pytest.raises(AgentTurnError) as ei:
        agent.ask("q1", 5)
    assert "429" in str(ei.value)
    c.on_prompt = lambda fake, n, t, w: (claude_user(path, t), claude_end(path, interrupted=True))
    with pytest.raises(AgentTurnAbortedError):
        agent.ask("q2", 5)


def test_timeout_carries_a_json_pending_without_the_prompt(tmp_path):
    """Scenario: 超时 — the agent stays alive; pending is JSON-native and holds a digest, not the prompt."""
    c, agent, path = make(tmp_path)
    secret = "the launch code is 0000-SECRET-PROMPT-TEXT and more words follow here"

    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        fake.set_status(name, "working")

    c.on_prompt = on_prompt
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask(secret, 0.3)
    e = ei.value
    assert json.loads(json.dumps(e.pending)) == e.pending
    assert e.pending["source"] == "session_log" and e.pending["agent"] == "a" and e.pending["sid"] == "sid"
    # Scenario: 回合信息不含 prompt — no prompt text, no echo key, no log path (D11).
    assert "SECRET-PROMPT" not in json.dumps(e.pending) and "launch code" not in json.dumps(e.pending)
    assert set(e.pending) == {"v", "agent", "kind", "sid", "digest", "source", "mark"}
    assert e.diagnostics == "agent: claude\nstate: working" and e.status == "working"
    assert "a" in c.agents


def test_herdr_timeout_on_submit_is_a_timeout_with_pending(tmp_path):
    c, agent, _ = make(tmp_path)
    c.on_prompt = lambda *a: herr(H.TIMEOUT, "timed out")
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("q", 1)
    assert ei.value.pending and ei.value.pending["v"] == A.PENDING_VERSION


# ---- 7.2 pre-check --------------------------------------------------------------------------


def test_working_agent_is_waited_for_before_submitting(tmp_path):
    """Scenario: 超时后再次提问 — the earlier turn ends first; the reply belongs to the new prompt."""
    c, agent, path = make(tmp_path)
    claude_user(path, "first")
    c.set_status("a", "working")

    def wait_result(fake, name, timeout_ms, until):
        claude_end(path, "first reply")
        fake.set_status(name, "idle")
        return None

    c.wait_result = wait_result
    c.on_prompt = answer_claude(path, "second reply")
    assert agent.ask("second", 5) == "second reply"
    names = c.names()
    assert names.index("agent_wait") < names.index("agent_prompt")


def test_working_agent_that_does_not_settle_is_busy(tmp_path):
    c, agent, _ = make(tmp_path)
    c.set_status("a", "working")
    c.wait_result = lambda *a: herr(H.TIMEOUT, "t")
    with pytest.raises(AgentBusyError):
        agent.ask("q", 0.5)
    assert not c.args_of("agent_prompt")


def test_unknown_status_follows_the_log(tmp_path):
    """Scenario: 状态未知时提问 — the open turn ends first; with no end in time: busy, nothing submitted."""
    c, agent, path = make(tmp_path)
    claude_user(path, "earlier")
    c.set_status("a", "unknown")
    with pytest.raises(AgentBusyError):
        agent.ask("q", 0.2)
    assert not c.args_of("agent_prompt")
    later(0.1, claude_end, path, "earlier reply")
    c.on_prompt = answer_claude(path, "new reply")
    assert agent.ask("q", 5) == "new reply"


def test_unknown_status_without_a_log_waits_briefly(tmp_path):
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.set_status("a", "unknown")
    c.wait_result = lambda *a: herr(H.TIMEOUT, "t")
    with pytest.raises(AgentBusyError):
        agent.ask("q", 5)
    (_, timeout_ms, until), = c.args_of("agent_wait")
    assert until == ("idle", "done") and timeout_ms <= A.UNKNOWN_WAIT * 1000 + 1
    assert not c.args_of("agent_prompt")


# ---- 7.2 copilot: status not trusted ----------------------------------------------------------


def answer_copilot(path, reply="PONG"):
    def on_prompt(fake, name, text, wait):
        copilot_user(path, text)
        copilot_end(path, reply)
    return on_prompt


def test_copilot_submits_without_wait_and_confirms_by_log(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "PONG")
    assert agent.ask("q", 5) == "PONG"
    (_, _, timeout_ms, wait), = c.args_of("agent_prompt")
    assert wait is False and timeout_ms is None


def test_copilot_status_change_is_not_delivery_evidence(tmp_path):
    c, agent, _ = make(tmp_path, kind="copilot")
    c.on_prompt = lambda fake, name, text, wait: fake.set_status(name, "working")
    with pytest.raises(PromptNotDeliveredError):
        agent.ask("q", 5)


def test_copilot_without_a_log_is_busy(tmp_path):
    c, agent, _ = make(tmp_path, kind="copilot", log=False)
    with pytest.raises(AgentBusyError):
        agent.ask("q", 5)
    assert not c.args_of("agent_prompt")


def test_working_copilot_reported_idle(tmp_path):
    """Scenario: 工作中的 agent 被报告为空闲 — the log decides; neither turn is lost."""
    c, agent, path = make(tmp_path, kind="copilot")
    copilot_user(path, "first")
    submitted_at = []
    finished = []

    def finish_first():
        finished.append(time.monotonic())
        copilot_end(path, "first reply")

    def on_prompt(fake, name, text, wait):
        submitted_at.append(time.monotonic())
        copilot_user(path, text)
        copilot_end(path, "second reply")

    later(0.15, finish_first)
    c.on_prompt = on_prompt
    assert agent.ask("second", 5) == "second reply"
    assert submitted_at[0] >= finished[0]
    assert "first reply" in path.read_text()


def test_copilot_turn_started_by_itself(tmp_path):
    """Scenario: agent 自行开始的回合进行中 — a notification turn ends before the prompt is submitted."""
    c, agent, path = make(tmp_path, kind="copilot")
    copilot_user(path, "earlier")
    copilot_end(path, "STARTED")
    append_jsonl(path, {"type": "system.notification", "timestamp": iso(), "data": {"content": "<system_notification>"}},
                 {"type": "assistant.turn_start", "timestamp": iso(), "data": {}})
    finished = []
    later(0.15, lambda: (finished.append(time.monotonic()), copilot_end(path, "notification turn")))
    submitted = []
    c.on_prompt = lambda fake, n, t, w: (submitted.append(time.monotonic()), copilot_user(path, t), copilot_end(path, "ok"))
    assert agent.ask("q", 5) == "ok"
    assert submitted[0] >= finished[0]


def test_turnless_notification_does_not_block_forever(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    append_jsonl(path, {"type": "system.notification", "timestamp": iso(time.time() - 30), "data": {}})
    c.on_prompt = answer_copilot(path, "ok")
    assert agent.ask("q", 2) == "ok"


def test_prompt_received_during_a_turn_of_its_own_is_not_answered(tmp_path):
    """Scenario: prompt 与自行开始的回合交错 — ReplyNotFoundError, no text."""
    c, agent, path = make(tmp_path, kind="copilot")

    def on_prompt(fake, name, text, wait):  # a notification arrives in the pre-check/submit window
        append_jsonl(path, {"type": "system.notification", "timestamp": iso(), "data": {}})
        copilot_user(path, text)
        copilot_end(path, "which turn?")

    c.on_prompt = on_prompt
    with pytest.raises(ReplyNotFoundError):
        agent.ask("q", 5)


# ---- 7.2 dialogs ---------------------------------------------------------------------------------


def blocked_then(path, times, reply="done after refusal"):
    """on_prompt: the agent stops at a dialog ``times`` times (each Esc clears one), then answers."""
    state = {"left": times}

    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        fake.set_status(name, "blocked" if times else "idle")
        if not times:
            claude_end(path, reply)

    def on_keys(fake, name, keys):
        state["left"] -= 1
        if state["left"] > 0:
            fake.set_status(name, "blocked")
        else:
            claude_end(path, reply)
            fake.set_status(name, "idle")

    return on_prompt, on_keys


def test_read_only_refuses_a_dialog_and_continues(tmp_path):
    """Scenario: 单次越权."""
    c, agent, path = make(tmp_path, policy="read_only")
    c.on_prompt, c.on_keys = blocked_then(path, 1)
    c.screens = ["Do you want to create dm.txt? 1. Yes 2. No"]
    assert agent.ask("write it", 5) == "done after refusal"
    assert [k for _, *k in c.args_of("agent_send_keys")] == [["esc"]]


def test_repeated_dialogs_end_with_blocked(tmp_path):
    """Scenario: 反复越权 — the 4th dialog of one turn raises AgentBlockedError with the screen."""
    c, agent, path = make(tmp_path, policy="read_only")
    c.on_prompt, c.on_keys = blocked_then(path, 10)
    c.screens = ["approval dialog text"]
    with pytest.raises(AgentBlockedError) as ei:
        agent.ask("write it", 5)
    assert ei.value.screen == "approval dialog text"
    assert len(c.args_of("agent_send_keys")) == A.DENY_LIMIT


def test_full_auto_dialog_raises_then_dismiss_recovers(tmp_path):
    """Scenarios: 完全自动下出现对话框 / 完全自动下从阻塞恢复 / 处理阻塞."""
    c, agent, path = make(tmp_path, policy="full_auto")
    c.on_prompt, c.on_keys = blocked_then(path, 1, reply="unused")
    c.screens = ["some dialog"]
    with pytest.raises(AgentBlockedError) as ei:
        agent.ask("q", 5)
    assert ei.value.screen == "some dialog" and not c.args_of("agent_send_keys")
    agent.dismiss()
    assert [k for _, *k in c.args_of("agent_send_keys")] == [["esc"]]
    assert agent.status() in ("idle", "done")
    c.on_prompt, c.on_keys = answer_claude(path, "after recovery"), None
    assert agent.ask("q2", 5) == "after recovery"


def test_dismiss_does_nothing_when_not_blocked(tmp_path):
    c, agent, _ = make(tmp_path)
    agent.dismiss()
    assert not c.args_of("agent_send_keys")


def test_blocked_before_submitting_is_handled_first(tmp_path):
    c, agent, path = make(tmp_path, policy="read_only")
    c.set_status("a", "blocked")
    c.on_keys = lambda fake, name, keys: fake.set_status(name, "idle")
    c.on_prompt = answer_claude(path, "ok")
    assert agent.ask("q", 5) == "ok"
    names = c.names()
    assert names.index("agent_send_keys") < names.index("agent_prompt")


# ---- 7.4 screen capture through the handle ------------------------------------------------------------


def test_screen_capture_when_no_log(tmp_path):
    """Scenario: 退回屏幕截取 (cursor)."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "older output\n→ Add a follow-up\n  Auto Balance   Run Everything\n"

    def on_prompt(fake, name, text, wait):
        fake.recent = f"older output\n  {text}\n  The answer is 42.\n→ Add a follow-up\n  Auto Balance   Run Everything\n"

    c.on_prompt = on_prompt
    r = agent.ask_reply("What is the answer?", 5)
    assert (r.text, r.source) == ("The answer is 42.", "screen")


def test_reply_that_cannot_be_separated(tmp_path):
    """Scenario: 无法分离回复 — ReplyNotFoundError with the screen, never the raw screen as a reply."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "older output\n→ Add a follow-up\n  Auto Balance   Run Everything\n"
    c.screens = ["visible screen text"]
    c.on_prompt = lambda fake, name, text, wait: setattr(fake, "recent", "something else entirely\n")
    with pytest.raises(ReplyNotFoundError) as ei:
        agent.ask("What is the answer?", 5)
    assert ei.value.screen == "visible screen text"
    assert [a[2] for a in c.args_of("agent_read") if a[1] == "recent-unwrapped"][-2:] == [A.SCREEN_LINES, A.SCREEN_LINES * 4]


# ---- 7.5 interrupt / wait_reply / cancel / close ---------------------------------------------------


def test_timeout_then_wait_reply_on_another_handle(tmp_path):
    """Scenario: 超时后取回该轮回复 — on a second handle; handles keep no turn state."""
    c, agent, path = make(tmp_path)

    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        fake.set_status(name, "working")

    c.on_prompt = on_prompt
    before = dict(vars(agent))
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("slow question", 0.2)
    assert vars(agent).keys() == before.keys()
    binding = {"_log_path", "_log", "_log_bound_to", "_log_resolve", "_config"}  # where the log is, not turn state
    assert all(v == before[k] for k, v in vars(agent).items() if k not in binding)
    assert not any("slow question" in repr(v) for k, v in vars(agent).items() if k != "_h")  # _h: the fake's call log
    other = A.Agent(c, name="a", kind="claude", policy="full_auto", workspace_id="w1", pane_id="w1:p1",
                    label="LABEL", session_id="sid")
    other._log_path = str(path)
    later(0.1, lambda: (claude_end(path, "slow answer"), c.set_status("a", "idle")))
    r = other.wait_reply(ei.value.pending, 5)
    assert (r.text, r.source) == ("slow answer", "session_log")


def test_wait_reply_rejects_foreign_pending(tmp_path):
    _, agent, _ = make(tmp_path)
    for bad in (None, {}, {"v": 1, "agent": "other"}, {"v": 1, "agent": "a", "digest": "x", "echo": [], "source": "?"}):
        with pytest.raises(InvalidConfigError):
            agent.wait_reply(bad, 1)


def test_screen_pending_round_trip(tmp_path):
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "older output\n→ Add a follow-up\n  Auto Balance   Run Everything\n"
    c.on_prompt = lambda fake, name, text, wait: fake.set_status(name, "working")
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("What is the answer?", 0.2)
    assert ei.value.pending["source"] == "screen"
    c.recent = "older output\n  What is the answer?\n  42\n→ Add a follow-up\n  Auto Balance   Run Everything\n"
    c.set_status("a", "idle")
    assert agent.wait_reply(ei.value.pending, 5).text == "42"


def test_interrupt_after_timeout(tmp_path):
    """Scenario: 超时后中断 — codex uses Esc, never ctrl+c."""
    c, agent, _ = make(tmp_path, kind="codex")
    c.set_status("a", "working")
    c.on_keys = lambda fake, name, keys: fake.set_status(name, "idle")
    agent.interrupt()
    assert [k for _, *k in c.args_of("agent_send_keys")] == [["esc"]]
    assert agent.status() == "idle"


def test_unverified_interrupt_key_waits_bounded_and_is_not_resent(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    assert K.adapter("copilot").capability(K.INTERRUPT_KEY).state == K.UNVERIFIED
    copilot_user(path, "long task")
    with pytest.raises(AgentTimeoutError):
        agent.interrupt()
    assert len(c.args_of("agent_send_keys")) == 1


def test_cancel_interrupts_and_raises(tmp_path):
    """Scenario: 取消提问."""
    c, agent, path = make(tmp_path)

    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        fake.set_status(name, "working")

    c.on_prompt = on_prompt
    c.on_keys = lambda fake, name, keys: fake.set_status(name, "idle")
    cancel = threading.Event()
    later(0.1, cancel.set)
    with pytest.raises(AskCancelledError):
        agent.ask("q", 5, cancel=cancel)
    assert [k for _, *k in c.args_of("agent_send_keys")] == [["esc"]]


def test_close_during_an_ask(tmp_path):
    """Scenario: 提问进行中关闭 — close does not wait; the ask fails."""
    c, agent, path = make(tmp_path)

    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        fake.set_status(name, "working")

    c.on_prompt = on_prompt
    later(0.1, agent.close)
    t0 = time.monotonic()
    with pytest.raises(AgentNotFoundError):
        agent.ask("q", 5)
    assert time.monotonic() - t0 < 2
    assert "w1" not in c.workspaces


# ---- phase-4 review regressions -------------------------------------------------------------------


def strict_prompt(path, reply="PONG"):
    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        claude_end(path, reply)
        fake.set_status(name, "idle")
    return on_prompt


def test_dialog_left_by_the_earlier_turn_is_handled_then_the_prompt_is_sent(tmp_path):
    """Review C3: the pre-check waits, finds a dialog, refuses it, then submits (read_only)."""
    c, agent, path = make(tmp_path, policy="read_only")
    c.set_status("a", "working")

    def wait_result(fake, name, timeout_ms, until):
        if fake.agents[name]["agent_status"] == "working":
            fake.set_status(name, "blocked")
        return None

    c.wait_result = wait_result
    c.on_prompt = strict_prompt(path, "after the dialog")
    c.on_keys = lambda fake, name, keys: fake.set_status(name, "idle")
    assert agent.ask("hello", 5) == "after the dialog"
    assert len(c.args_of("agent_prompt")) == 1 and [k for _, *k in c.args_of("agent_send_keys")] == [["esc"]]


def test_dialog_still_there_after_one_refusal(tmp_path):
    """Review C3: refusals in the pre-check repeat (shared budget) until the dialog is gone."""
    c, agent, path = make(tmp_path, policy="read_only")
    c.set_status("a", "blocked")
    left = {"n": 2}

    def on_keys(fake, name, keys):
        left["n"] -= 1
        fake.set_status(name, "blocked" if left["n"] > 0 else "idle")

    c.on_keys = on_keys
    c.on_prompt = strict_prompt(path, "ok")
    assert agent.ask("hello", 5) == "ok"
    assert len(c.args_of("agent_send_keys")) == 2


@pytest.mark.parametrize("kind", ["claude", "copilot"])
def test_blocked_on_submit_is_handled_and_resubmitted(tmp_path, kind):
    """Review C3/C8: herdr refuses the prompt with agent_blocked (nothing sent); refuse, then resend."""
    c, agent, path = make(tmp_path, kind=kind, policy="read_only")
    delivered = []

    def on_prompt(fake, name, text, wait):
        delivered.append(text)
        if kind == "copilot":
            copilot_user(path, text)
            copilot_end(path, "resent")
        else:
            claude_user(path, text)
            claude_end(path, "resent")

    orig = c.agent_prompt

    def agent_prompt(name, text, timeout_ms=None, wait=True, cancel=None):
        if not c.args_of("agent_send_keys"):  # the first attempt finds a dialog
            c.set_status(name, "blocked")
        return orig(name, text, timeout_ms, wait, cancel)

    c.agent_prompt = agent_prompt
    c.on_prompt = on_prompt
    c.on_keys = lambda fake, name, keys: fake.set_status(name, "idle")
    assert agent.ask("hello", 5) == "resent"
    assert delivered == ["hello"] and len(c.args_of("agent_send_keys")) == 1


def test_copilot_blocked_on_submit_under_full_auto_is_agent_blocked(tmp_path):
    """Review C8: AgentBlockedError, not a raw HerdrError."""
    c, agent, path = make(tmp_path, kind="copilot", policy="full_auto")
    orig = c.agent_prompt

    def agent_prompt(name, text, timeout_ms=None, wait=True, cancel=None):
        c.set_status(name, "blocked")  # a dialog popped up between the pre-check and the submit
        return orig(name, text, timeout_ms, wait, cancel)

    c.agent_prompt = agent_prompt
    c.screens = ["dialog"]
    with pytest.raises(AgentBlockedError) as ei:
        agent.ask("hello", 2)
    assert ei.value.screen == "dialog" and not c.args_of("agent_send_keys")


def test_cancel_before_submitting_sends_no_key(tmp_path):
    """Review C5: the ask still waits for an earlier turn; that turn is not interrupted."""
    c, agent, _ = make(tmp_path)
    c.set_status("a", "working")
    cancel = threading.Event()
    c.wait_result = lambda fake, name, timeout_ms, until: cancel.set()
    with pytest.raises(AskCancelledError):
        agent.ask("hello", 5, cancel=cancel)
    assert not c.args_of("agent_prompt") and not c.args_of("agent_send_keys")


def test_cancel_while_waiting_for_the_lock_sends_no_key(tmp_path):
    from polyagent import locks

    c, agent, _ = make(tmp_path)
    held = locks.try_lock(c.session, "a")
    cancel = threading.Event()
    later(0.05, cancel.set)
    try:
        with pytest.raises(AskCancelledError):
            agent.ask("q", 5, cancel=cancel)
    finally:
        held.release()
    assert not c.args_of("agent_send_keys") and not c.args_of("agent_prompt")


def test_close_while_waiting_for_the_lock_is_agent_not_found(tmp_path):
    """Review C7 / D11: closed, not cancelled."""
    from polyagent import locks

    c, agent, _ = make(tmp_path)
    held = locks.try_lock(c.session, "a")
    later(0.05, agent.close)
    try:
        with pytest.raises(AgentNotFoundError):
            agent.ask("q", 5)
    finally:
        held.release()
    assert not c.args_of("agent_prompt") and not c.args_of("agent_send_keys")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "abc", None, True])
def test_timeout_must_be_a_finite_positive_number(tmp_path, timeout):
    """Review C7."""
    c, agent, _ = make(tmp_path)
    with pytest.raises(InvalidConfigError):
        agent.ask("q", timeout)
    with pytest.raises(InvalidConfigError):
        agent.wait_reply({"v": 1}, timeout)
    assert not c.args_of("agent_prompt")


def test_deadline_before_submitting_is_busy_without_submission(tmp_path):
    """Review C7 / D11: nothing submitted -> AgentBusyError, not a timeout with a pending turn."""
    c, agent, _ = make(tmp_path)
    with pytest.raises(AgentBusyError):
        agent.ask("q", 1e-9)
    assert not c.args_of("agent_prompt")


def test_rewritten_pending_does_not_choose_the_log(tmp_path):
    """Scenario: 被改写的回合信息 — a path written into pending is never read; the handle binds its own log."""
    c, agent, path = make(tmp_path)
    foreign = tmp_path / "someone-else.jsonl"
    claude_user(foreign, "their question")
    claude_end(foreign, "their private answer")
    claude_user(path, "my question")
    pending = {"v": A.PENDING_VERSION, "agent": "a", "kind": "claude", "sid": "sid",
               "digest": prompt_digest("their question"), "source": "session_log", "mark": 0, "log": str(foreign)}
    with pytest.raises(PromptNotDeliveredError):  # judged on the handle's own log, where it never appears
        agent.wait_reply(pending, 2)
    later(0.05, lambda: claude_end(path, "my answer"))
    r = agent.wait_reply({**pending, "digest": prompt_digest("my question")}, 2)
    assert (r.text, r.source) == ("my answer", "session_log")


def test_pending_of_another_agent_or_session_is_refused(tmp_path):
    """Scenario: 另一个 agent 的回合信息; a restarted agent of the same name has another session id."""
    c, agent, path = make(tmp_path)
    good = {"v": A.PENDING_VERSION, "agent": "a", "kind": "claude", "sid": "sid", "digest": "d",
            "source": "session_log", "mark": 0}
    for bad in ({**good, "agent": "b"}, {**good, "sid": "other-session"}, {**good, "sid": None},
                {**good, "kind": "codex"}, {**good, "v": 1}, {**good, "mark": -1}, {**good, "mark": True}):
        with pytest.raises(InvalidConfigError):
            agent.wait_reply(bad, 1)
    assert not c.args_of("agent_get")


def claude_home_log(sid, project="-some-project-dir"):
    from polyagent import locks

    d = locks.user_home() / ".claude" / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{sid}.jsonl"


def test_claude_log_found_by_session_id_without_a_cwd(tmp_path):
    """Scenario: 接管后回复来源不变 — a handle has no working directory at all; the log is found by session id."""
    c = FakeClient()
    c.workspaces["w1"] = {"workspace_id": "w1", "label": "L"}
    c.add_agent("a", "w1")
    sid = "11111111-2222-3333-4444-555555555555"
    agent = A.Agent(c, name="a", kind="claude", policy="full_auto", workspace_id="w1", pane_id="w1:p1", label="L",
                    session_id=sid)
    path = claude_home_log(sid)
    c.on_prompt = answer_claude(path, "from the log")
    r = agent.ask_reply("q", 5)
    assert (r.text, r.source) == ("from the log", "session_log")


def test_claude_log_created_after_binding_is_picked_up(tmp_path):
    """Scenario: 工作目录含特殊字符 — the transcript appears only with the first message, in a project
    directory whose name the library never computes; the binding finds it by session id (a miss is
    not cached)."""
    c = FakeClient()
    c.workspaces["w1"] = {"workspace_id": "w1", "label": "L"}
    c.add_agent("a", "w1")
    sid = "21111111-2222-3333-4444-555555555555"
    agent = A.Agent(c, name="a", kind="claude", policy="full_auto", workspace_id="w1", pane_id="w1:p1", label="L",
                    session_id=sid)
    assert agent._bind_log() is not None and not agent._bind_log().exists()
    path = claude_home_log(sid, project="-srv-my project-app@2-ü")
    c.on_prompt = answer_claude(path, "found")
    assert agent.ask_reply("q", 5).source == "session_log"


def test_claude_log_that_never_appears_is_reply_not_found(tmp_path):
    """Scenario: 屏幕截取规则未经验证的 kind — no log file after the turn ended: the log is unavailable,
    and claude takes no reply from the screen (unsupported, task 13.16)."""
    c, agent, path = make(tmp_path)
    c.recent = "older output\n"

    def on_prompt(fake, name, text, wait):
        fake.recent = f"older output\n> {text}\n\nPONG\n"
        fake.set_status(name, "done")

    c.on_prompt = on_prompt
    c.screens = ["visible"]
    with pytest.raises(ReplyNotFoundError) as ei:
        agent.ask_reply("hello", 5)
    assert "session log" in str(ei.value) and "unavailable" in str(ei.value) and ei.value.screen == "visible"
    assert "13.16" not in str(ei.value) and "verified" not in str(ei.value)  # unsupported, not pending (13.16)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["claude", "copilot"])
def test_unverified_screen_capture_is_never_used(tmp_path, kind):
    """Without any log (claude without a session id): the turn ends, ReplyNotFoundError; copilot cannot even
    submit without its log (D11)."""
    c, agent, _ = make(tmp_path, kind=kind, log=False)
    c.recent = "older output\n"
    c.on_prompt = lambda fake, name, text, wait: setattr(fake, "recent", f"older output\n> {text}\n\nPONG\n")
    with pytest.raises(AgentBusyError if kind == "copilot" else ReplyNotFoundError):
        agent.ask("hello", 5)


def test_codex_without_its_log_uses_the_screen(tmp_path):
    """codex has screen samples: an unbound rollout (task 13.15) falls back to the screen."""
    c, agent, _ = make(tmp_path, kind="codex", log=False)
    footer = "› Ask Codex to do anything\n\n  gpt default · /tmp/repo · ← for agents\n"
    c.recent = "older output\n\n" + footer
    c.on_prompt = lambda fake, name, text, wait: setattr(
        fake, "recent", f"older output\n\n› {text}\n\n• PONG\n\n{footer}")
    r = agent.ask_reply("hello", 5)
    assert (r.text, r.source) == ("PONG", "screen")


def test_codex_dialog_is_not_an_input_area(tmp_path):
    """Scenario: 对话框不是输入区 (task 13.31) — codex at its trust dialog: nothing is submitted."""
    c, agent, _ = make(tmp_path, kind="codex", log=False)
    c.recent = (Path(__file__).parent / "fixtures/screens/codex/trust-dialog.txt").read_text()
    c.on_prompt = lambda fake, name, text, wait: None
    with pytest.raises(AgentBusyError, match="recognizable input state"):
        agent.ask("What is the answer?", 2)
    assert not c.args_of("agent_prompt")


def test_no_baseline_read_when_the_log_file_exists(tmp_path):
    """D11 step 4: the screen baseline is only recorded when there is no log file yet."""
    c, agent, path = make(tmp_path)
    claude_user(path, "earlier")
    claude_end(path, "earlier answer")
    c.on_prompt = answer_claude(path, "ok")
    assert agent.ask("q", 5) == "ok"
    assert not [a for a in c.args_of("agent_read") if a[1] == "recent-unwrapped"]


def test_bound_adapter_is_reused(tmp_path):
    """Review C12: reads stay incremental across asks and status() calls."""
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "one")
    agent.ask("q1", 5)
    first = agent._bind_log()
    agent.status()
    c.on_prompt = answer_copilot(path, "two")
    assert agent.ask("q2", 5) == "two"
    assert agent._bind_log() is first


def test_screen_delivery_evidence_is_the_echo(tmp_path):
    """Review C11 (second point): the echo alone confirms delivery; the reply may come later."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "older output\n→ Add a follow-up\n  Auto Balance   Run Everything\n"

    def on_prompt(fake, name, text, wait):
        fake.recent = f"older output\n  {text}\n"
        later(0.05, lambda: setattr(fake, "recent", f"older output\n  {text}\n  42\n→ Add a follow-up\n  Auto Balance   Run Everything\n"))
        later(0.1, fake.set_status, name, "idle")
        return herr(H.AGENT_PROMPT_STALLED, "stalled")

    c.on_prompt = on_prompt
    c.agents["a"]["state_change_seq"] = 1
    assert agent.ask("What is the answer?", 5) == "42"


def test_copilot_after_an_interrupt_without_an_end_marker_stays_busy(tmp_path):
    """D11 known degradation until task 13.3: no end marker after the interrupt, so no prompt is submitted."""
    c, agent, path = make(tmp_path, kind="copilot")
    copilot_user(path, "long task")
    with pytest.raises(AgentTimeoutError):
        agent.interrupt()
    with pytest.raises(AgentBusyError):
        agent.ask("next", 0.2)
    assert not c.args_of("agent_prompt")


def test_screen_echo_is_no_delivery_evidence_without_screen_samples(tmp_path):
    """D11/D12: claude without a log: the echo on screen does not confirm delivery (screen rules unverified)."""
    c, agent, _ = make(tmp_path, log=False)
    c.recent = "older output\n"

    def on_prompt(fake, name, text, wait):
        fake.recent = f"older output\n> {text}\n"
        return herr(H.AGENT_PROMPT_STALLED, "stalled")

    c.on_prompt = on_prompt
    with pytest.raises(PromptNotDeliveredError):
        agent.ask("hello", 5)


# ---- phase-4 final review (N1, N2) --------------------------------------------------------------------


def cursor_three_pings(tmp_path):
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "\n".join(["Cursor Agent"] + ["> ping", "PONG"] * 3 + ["→ Add a follow-up", "  Auto Balance   Run Everything"]) + "\n"
    return c, agent


def test_undelivered_fourth_identical_prompt_is_not_answered_with_the_third_reply(tmp_path):
    """Review N1: three identical ping/PONG turns on screen; the fourth prompt never shows up."""
    c, agent = cursor_three_pings(tmp_path)
    c.on_prompt = lambda *a: None  # herdr reports success, nothing reaches the screen
    with pytest.raises(ReplyNotFoundError):
        agent.ask_reply("ping", 2)


def test_delivered_fourth_identical_prompt_gets_its_own_reply(tmp_path):
    c, agent = cursor_three_pings(tmp_path)
    c.on_prompt = lambda fake, name, text, wait: setattr(
        fake, "recent", fake.recent.replace("→ Add a follow-up", f"> {text}\nPONG 4\n→ Add a follow-up"))
    r = agent.ask_reply("ping", 2)
    assert (r.text, r.source) == ("PONG 4", "screen")


def test_undelivered_identical_prompt_is_not_confirmed_by_an_old_echo(tmp_path):
    """Review N1: `echo_seen` used for delivery confirmation has the same rule."""
    c, agent = cursor_three_pings(tmp_path)
    c.on_prompt = lambda *a: herr(H.AGENT_PROMPT_STALLED, "stalled")
    with pytest.raises(PromptNotDeliveredError):
        agent.ask("ping", 5)


def test_occurrences_are_counted_on_the_widest_window(tmp_path, monkeypatch):
    """Review N1: the reply retry reads 4x more lines; old occurrences that only it shows must not count
    as after the pre-submit position."""
    monkeypatch.setattr(A, "SCREEN_LINES", 4)
    c, agent = cursor_three_pings(tmp_path)

    def agent_read(name, source="recent-unwrapped", lines=None):
        text = c.recent if source != "visible" else "visible"
        return "\n".join(text.splitlines()[-lines:]) if lines else text

    c.agent_read = agent_read
    c.on_prompt = lambda *a: None
    with pytest.raises(ReplyNotFoundError):
        agent.ask_reply("ping", 2)


@pytest.mark.parametrize("before", ["working", "unknown"])
def test_state_change_evidence_starts_after_the_pre_check_wait(tmp_path, before):
    """Review N2: the state compared for delivery is the one after waiting for the earlier turn; a
    stalled, undelivered prompt is PromptNotDelivered, not a 'state changed' delivery."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "older output\n→ Add a follow-up\n  Auto Balance   Run Everything\n"
    c.set_status("a", before)
    c.wait_result = lambda fake, name, ms, until: fake.set_status(name, "idle") if fake.agents[name][
        "agent_status"] != "idle" else None
    c.on_prompt = lambda *a: herr(H.AGENT_PROMPT_STALLED, "stalled")
    with pytest.raises(PromptNotDeliveredError):
        agent.ask("What is the answer?", 5)


# ---- phase-4 spot check (S1, S3) ----------------------------------------------------------------------


def cursor_one_ping(tmp_path):
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "Cursor Agent\n> ping\nPONG\n→ Add a follow-up\n  Auto Balance   Run Everything\n"
    return c, agent


@pytest.mark.parametrize("turn", ["not_delivered", "stalled", "delivered"])
def test_failed_pre_submit_read_is_not_an_empty_screen(tmp_path, turn):
    """Review S1: the read that records the baseline fails; an empty baseline would make the old
    ping/PONG pass for this prompt's echo and reply. Nothing is submitted, herdr's error is raised."""
    c, agent = cursor_one_ping(tmp_path)
    orig = c.agent_read
    failed = []

    def agent_read(name, source="recent-unwrapped", lines=None):
        if source == "recent-unwrapped" and not failed:
            failed.append(1)
            raise herr(H.TIMEOUT, "read timed out")
        return orig(name, source, lines)

    c.agent_read = agent_read
    c.on_prompt = {
        "not_delivered": lambda *a: None,
        "stalled": lambda *a: herr(H.AGENT_PROMPT_STALLED, "stalled"),
        "delivered": lambda fake, name, text, wait: setattr(
            fake, "recent", fake.recent.replace("→ Add", f"> {text}\nNEW ANSWER\n→ Add")),
    }[turn]
    with pytest.raises(HerdrError) as ei:
        agent.ask_reply("ping", 2)
    assert ei.value.code == H.TIMEOUT and failed
    assert not c.args_of("agent_prompt") and not c.args_of("agent_send_keys")


def test_pre_submit_read_of_a_vanished_agent_is_agent_not_found(tmp_path):
    c, agent = cursor_one_ping(tmp_path)
    c.agent_read = lambda *a, **k: (_ for _ in ()).throw(herr(H.AGENT_NOT_FOUND, "gone"))
    with pytest.raises(AgentNotFoundError):
        agent.ask("ping", 2)
    assert not c.args_of("agent_prompt")


def timed_out_fourth_ping(tmp_path):
    c, agent = cursor_three_pings(tmp_path)
    c.on_prompt = lambda fake, name, text, wait: fake.set_status(name, "working")
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("ping", 0.2)
    pending = json.loads(json.dumps(ei.value.pending))
    assert pending["source"] == "screen" and pending["count"] == 2 and pending["baseline"]
    other = A.Agent(c, name="a", kind="cursor", policy="full_auto", workspace_id="w1", pane_id="w1:p1", label="LABEL")
    other._find_log_path = lambda: None  # type: ignore[method-assign]
    return c, other, pending


def test_wait_reply_applies_the_count_when_the_prompt_never_arrived(tmp_path):
    """Review S3: on another handle, the pending's count keeps the three old turns out of reach."""
    c, other, pending = timed_out_fourth_ping(tmp_path)
    c.set_status("a", "idle")
    with pytest.raises(ReplyNotFoundError):
        other.wait_reply(pending, 1)


def test_wait_reply_applies_the_count_when_the_prompt_arrived(tmp_path):
    c, other, pending = timed_out_fourth_ping(tmp_path)
    c.recent = c.recent.replace("→ Add", "> ping\nPONG 4\n→ Add")
    c.set_status("a", "idle")
    assert other.wait_reply(pending, 2).text == "PONG 4"


@pytest.mark.parametrize("count", ["missing", True, 0, -1, 1.5, "2", [2], None])
def test_malformed_count_is_refused(tmp_path, count):
    """Review S3: with a non-empty baseline the count must be an int >= 1."""
    c, other, pending = timed_out_fourth_ping(tmp_path)
    bad = {k: v for k, v in pending.items() if k != "count"} if count == "missing" else {**pending, "count": count}
    calls = len(c.calls)
    with pytest.raises(InvalidConfigError):
        other.wait_reply(bad, 1)
    assert len(c.calls) == calls  # refused before any herdr call


def test_empty_baseline_with_count_zero_is_accepted(tmp_path):
    """A fresh screen (first prompt) has no baseline and a count of 0."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    pending = {"v": A.PENDING_VERSION, "agent": "a", "kind": "cursor", "sid": None, "digest": prompt_digest("q"),
               "source": "screen", "baseline": [], "count": 0, "echo": ["q"]}
    c.recent = "> q\nanswer\n→ Add a follow-up\n  Auto Balance   Run Everything\n"
    assert agent.wait_reply(pending, 2).text == "answer"


def test_timeout_diagnostics_keep_only_the_detection_result(tmp_path):
    """Task 7.11 / review C10: the real `agent explain` output recorded in tests/fixtures/herdr/README.md;
    its `evidence:` line quotes the screen (here with the prompt's echo), `manifest:` names a home path.
    Only agent/state/rule stay, in the diagnostics, the message and the repr."""
    readme = (Path(__file__).parent / "fixtures" / "herdr" / "README.md").read_text()
    sample = readme.split("agent: copilot\n", 1)[1].split("```", 1)[0]
    real = "agent: copilot\n" + "\n".join(line.strip() for line in sample.splitlines())
    real = real.replace("[Current]", "> SECRET-PROMPT-ECHO [Current]")
    assert "evidence:" in real and "manifest:" in real
    c, agent, path = make(tmp_path)
    c.explain = {"explanation": real}

    def on_prompt(fake, name, text, wait):
        claude_user(path, text)
        fake.set_status(name, "working")

    c.on_prompt = on_prompt
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("SECRET-PROMPT-ECHO", 0.3)
    e = ei.value
    assert e.diagnostics == ("agent: copilot\nstate: blocked\n"
                             "rule: selection_blocker (region=whole_recent priority=300)")
    for text in (e.diagnostics, str(e), repr(e)):
        assert "SECRET-PROMPT-ECHO" not in text and ".local/state" not in text


def test_no_diagnostics_when_explain_has_none_of_the_kept_lines(tmp_path):
    c, agent, path = make(tmp_path)
    c.explain = {"explanation": "evidence: \"only screen text\"\n"}
    c.on_prompt = lambda fake, name, text, wait: (claude_user(path, text), fake.set_status(name, "working"))
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask("q", 0.3)
    assert ei.value.diagnostics is None


# ---- 7.10 submission record (D11, kinds whose herdr status is not trusted) -------------------------


from polyagent import locks  # noqa: E402
from polyagent.capture.copilot import CopilotLog  # noqa: E402


def record_file(name="a"):
    return locks.submission_path("polyagent", name)


def prompts_by_call(*handlers):
    """``on_prompt`` that runs the n-th handler on the n-th submission."""
    calls = []

    def on_prompt(fake, name, text, wait):
        calls.append(time.monotonic())
        handlers[len(calls) - 1](fake, name, text, wait)
    return on_prompt, calls


def nothing(fake, name, text, wait):
    pass


def test_previous_prompt_not_yet_in_the_log_is_waited_for(tmp_path, monkeypatch):
    """Scenario: 上一次提交还没有出现在会话日志里."""
    monkeypatch.setattr(A, "CONFIRM_LIMIT", 3.0)
    c, agent, path = make(tmp_path, kind="copilot")
    on_prompt, calls = prompts_by_call(nothing, answer_copilot(path, "second reply"))
    c.on_prompt = on_prompt
    with pytest.raises(AgentTimeoutError) as first:
        agent.ask("first question", 0.2)
    assert record_file().exists()
    ended = []
    later(0.2, copilot_user, path, "first question")
    later(0.4, lambda: (ended.append(time.monotonic()), copilot_end(path, "first reply")))
    assert agent.ask("second question", 5) == "second reply"
    assert len(calls) == 2 and calls[1] >= ended[0]
    assert agent.wait_reply(first.value.pending, 5).text == "first reply"
    assert not record_file().exists()  # removed once the first prompt showed up; the second was confirmed


def test_previous_prompt_that_never_arrives_stops_counting_after_the_limit(tmp_path, monkeypatch):
    """Scenario: 上一次提交始终没有出现."""
    monkeypatch.setattr(A, "CONFIRM_LIMIT", 0.5)
    c, agent, path = make(tmp_path, kind="copilot")
    on_prompt, calls = prompts_by_call(nothing, answer_copilot(path, "new reply"))
    c.on_prompt = on_prompt
    with pytest.raises(AgentTimeoutError):
        agent.ask("lost", 0.2)
    written_at = json.loads(record_file().read_text())["at"]
    assert agent.ask("new", 5) == "new reply"
    assert time.time() - written_at >= 0.5 and len(calls) == 2


def test_record_holds_no_prompt_text_and_is_private(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = nothing
    with pytest.raises(AgentTimeoutError):
        agent.ask("very secret question text", 0.2)
    f = record_file()
    data = f.read_text()
    assert "secret" not in data and set(json.loads(data)) == {"v", "digest", "mark", "at"}
    assert json.loads(data)["digest"] == prompt_digest("very secret question text")
    assert f.stat().st_mode & 0o777 == 0o600


def write_record(at, digest="d" * 64, mark=0):
    record_file().write_text(json.dumps({"v": 1, "digest": digest, "mark": mark, "at": at}))


def test_waiting_for_a_record_is_busy_and_submits_nothing(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "x")
    write_record(time.time())
    with pytest.raises(AgentBusyError, match="not reached its session log"):
        agent.ask("q", 0.2)
    assert not c.args_of("agent_prompt")


def test_record_from_another_handle_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "CONFIRM_LIMIT", 3.0)
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = nothing
    with pytest.raises(AgentTimeoutError):
        agent.ask("first", 0.2)
    other = A.Agent(c, name="a", kind="copilot", policy="full_auto", workspace_id="w1", pane_id="w1:p1",
                    label="LABEL", session_id="sid")
    other._log_path = str(path)
    with pytest.raises(AgentBusyError):
        other.ask("second", 0.3)
    assert len(c.args_of("agent_prompt")) == 1


def test_record_written_by_another_process_counts(tmp_path):
    import subprocess
    import sys
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "x")
    target = record_file()
    code = ("import json, sys, time; open(sys.argv[1], 'w').write(json.dumps("
            "{'v': 1, 'digest': 'e' * 64, 'mark': 0, 'at': time.time()}))")
    subprocess.run([sys.executable, "-c", code, str(target)], check=True)
    with pytest.raises(AgentBusyError):
        agent.ask("q", 0.2)
    assert not c.args_of("agent_prompt")


def test_expired_record_is_ignored_and_removed(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "fresh")
    write_record(time.time() - 3600)
    assert agent.ask("q", 2) == "fresh" and not record_file().exists()


def test_record_from_the_future_is_aged_from_the_first_ask_that_sees_it(tmp_path, monkeypatch):
    """Task 13.32 / phase-7 review C3: the clock was set back; the record's time is rewritten to now when
    first seen, so asks shorter than the limit stop waiting once the limit has passed in total."""
    monkeypatch.setattr(A, "CONFIRM_LIMIT", 0.6)
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "later")
    write_record(time.time() + 3600)
    t0 = time.time()
    busy = 0
    while True:
        try:
            reply = agent.ask("q", 0.2)
            break
        except AgentBusyError:
            busy += 1
            if busy == 1:
                at = json.loads(record_file().read_text())["at"]
                assert t0 <= at <= time.time()  # rewritten to the time it was first seen
            assert busy < 8, "the record from the future never expired"
    assert reply == "later" and busy >= 2 and time.time() - t0 >= 0.6


@pytest.mark.parametrize("content", ["not json", json.dumps({"digest": 1}), json.dumps({"v": 1, "digest": "d",
                                                                                      "mark": -1, "at": 1.0}),
                                     json.dumps({"v": 1, "digest": "d", "mark": 0, "at": 10 ** 400}),  # 13.32
                                     "[" * 100_000 + "]" * 100_000])
def test_malformed_record_is_ignored(tmp_path, content):
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "ok")
    record_file().write_text(content)
    assert agent.ask("q", 2) == "ok" and not record_file().exists()


def test_close_removes_the_record(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    write_record(time.time())
    agent.close()
    assert not record_file().exists()


def test_trusted_kinds_write_no_record(tmp_path):
    c, agent, path = make(tmp_path, kind="claude")
    c.on_prompt = lambda fake, name, text, wait: (claude_user(path, text), fake.set_status(name, "working"))
    with pytest.raises(AgentTimeoutError):
        agent.ask("q", 0.2)
    assert not record_file().exists()


def test_queued_prompt_in_turns_of_their_own_is_ambiguous(tmp_path):
    """D12: the second prompt was queued during the first turn and then got a turn of its own."""
    path = tmp_path / "events.jsonl"
    copilot_user(path, "first")
    copilot_user(path, "second")
    copilot_end(path, "answer one")
    append_jsonl(path, {"type": "assistant.turn_start", "timestamp": iso(), "data": {}})
    copilot_end(path, "answer two")
    log = CopilotLog(str(path))
    assert log.judge(0, prompt_digest("first")).outcome is A.Outcome.AMBIGUOUS
    assert log.judge(0, prompt_digest("second")).outcome is A.Outcome.AMBIGUOUS


# ---- task 13.21 at the handle level ------------------------------------------------------------------


def test_unrecognized_screen_before_submitting_is_busy(tmp_path):
    """Scenario: 屏幕不在可识别的输入状态 — herdr says idle, the screen bottom is not cursor's input area."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "Cursor Agent\n  some dialog we do not know\n  [y] yes  [n] no\n"
    c.on_prompt = lambda fake, name, text, wait: None
    with pytest.raises(AgentBusyError, match="recognizable input state"):
        agent.ask("What is the answer?", 2)
    assert not c.args_of("agent_prompt")


def test_prompt_left_in_the_input_box_raises_instead_of_returning_the_status_line(tmp_path):
    """Scenario: prompt 留在输入框里没有提交."""
    c, agent, _ = make(tmp_path, kind="cursor", log=False)
    c.recent = "older output\n→ Add a follow-up\n  Auto Balance   Run Everything\n"

    def on_prompt(fake, name, text, wait):  # typed into the input line, never submitted
        fake.recent = f"older output\n→ {text}\n  Auto Balance   Run Everything\n"

    c.on_prompt = on_prompt
    with pytest.raises((ReplyNotFoundError, PromptNotDeliveredError)):
        agent.ask("What is the answer?", 2)


# ---- task 13.32: the rest of the submission record (D11) --------------------------------------------


def test_failed_record_write_submits_nothing_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    readonly = tmp_path / "ro"
    readonly.mkdir()
    readonly.chmod(0o500)
    monkeypatch.setattr(A.locks, "submission_path", lambda session, name: readonly / f"{name}.submitted")
    c, agent, path = make(tmp_path, kind="copilot")
    c.on_prompt = answer_copilot(path, "x")
    try:
        with pytest.raises(AgentBusyError, match="submission record"):
            agent.ask("q", 2)
        assert not c.args_of("agent_prompt") and list(readonly.iterdir()) == []
    finally:
        readonly.chmod(0o700)


def test_record_is_removed_when_herdr_reports_blocked(tmp_path):
    """herdr sent nothing (`agent_blocked`): no record of a submission that did not happen."""
    from fakeclient import herr
    from polyagent import herdr as H

    c, agent, path = make(tmp_path, kind="copilot")

    def on_prompt(fake, name, text, wait):
        assert record_file().exists()  # written before submitting
        fake.agents[name]["agent_status"] = "blocked"
        return herr(H.AGENT_BLOCKED, "blocked")

    c.on_prompt = on_prompt
    with pytest.raises(AgentBlockedError):
        agent.ask("q", 2)
    assert not record_file().exists()


def test_removing_its_own_record_leaves_a_newer_one(tmp_path):
    c, agent, path = make(tmp_path, kind="copilot")
    mine = agent._write_submission("a" * 64, 0)
    newer = {"v": 1, "digest": "b" * 64, "mark": 3, "at": time.time() + 1}
    record_file().write_text(json.dumps(newer))
    agent._drop_submission(mine)
    assert json.loads(record_file().read_text()) == newer
    agent._drop_submission(newer)
    assert not record_file().exists()


# ---- task 13.15: codex, bind the log after submitting (D12 "提交后再绑定") ---------------------------------

CODEX_IDLE = "› Ask Codex to do anything\n\n  gpt default · /tmp/repo · ← for agents\n"
CODEX_SAMPLE = Path(__file__).parent / "fixtures" / "logs" / "codex" / "long-prompts.rollout.jsonl"
PONG_PROMPT = "Reply with exactly one word: PONG"


def _sample(turns):
    """The first ``turns`` turns of the real codex rollout (each ends with `task_complete`)."""
    out, done = [], 0
    for line in CODEX_SAMPLE.read_text().splitlines(keepends=True):
        if done == turns:
            break
        out.append(line)
        if '"task_complete"' in line:
            done += 1
    return out


def _codex(tmp_path, monkeypatch, lines, prompt_reply="SCREEN REPLY", starts=(777,), files=1):
    """A codex handle with no rollout before submitting; submitting writes ``lines`` to a rollout that
    the agent process (pid 4242) then holds. ``starts``: successive start times read for that pid. A
    `git` child (pid 5000) is in the foreground group before submitting and gone after it (phase 9)."""
    c, agent, _ = make(tmp_path, kind="codex", log=False)
    seen = {"submitted": False, "open": 0, "start": 0}
    agent._agent_processes = lambda: [(4242, "codex")] + ([] if seen["submitted"] else [(5000, "git")])  # type: ignore[method-assign]
    rollout = tmp_path / "rollout-2026-11-17T00-00-00-x.jsonl"
    starts = list(starts)

    def process_start(pid, root="/proc"):
        if pid == 5000:  # the git child: gone after submitting
            return None if seen["submitted"] else 999
        seen["start"] += 1
        return starts.pop(0) if len(starts) > 1 else starts[0]

    def open_rollouts(pids, root="/proc"):
        seen["open"] += 1
        assert 4242 in pids
        return [str(rollout)] * files if seen["submitted"] else []

    monkeypatch.setattr(A.locate, "process_start", process_start)
    monkeypatch.setattr(A.locate, "open_rollouts", open_rollouts)
    c.recent = CODEX_IDLE

    def on_prompt(fake, name, text, wait):
        rollout.write_text("".join(lines))
        seen["submitted"] = True
        fake.recent = f"› {text}\n\n• {prompt_reply}\n\n" + CODEX_IDLE
        fake.set_status(name, "done")

    c.on_prompt = on_prompt
    return c, agent, seen


def test_codex_first_turn_is_read_from_the_rollout_it_opens_after_submitting(tmp_path, monkeypatch):
    """Scenario: 会话日志在提交之后才出现 — the real first turn (environment context, then the prompt)."""
    c, agent, seen = _codex(tmp_path, monkeypatch, _sample(1))
    r = agent.ask_reply(PONG_PROMPT, 5)
    assert (r.text, r.source) == ("PONG", "session_log")
    assert agent._log_path is not None and agent._log_path.endswith(".jsonl")  # bound for the next asks


def test_codex_rollout_with_an_earlier_turn_is_not_adopted(tmp_path, monkeypatch):
    """Scenario: 提交之后出现的日志里已有别的回合 — the second prompt of the sample, after an ended turn."""
    multi = ("Use your file tools to read alpha.txt and beta.txt in the current directory, then answer with a "
             "markdown bullet list with one bullet per file, each giving the file name in bold and its number.")
    c, agent, seen = _codex(tmp_path, monkeypatch, _sample(2))
    r = agent.ask_reply(multi, 5)
    assert (r.text, r.source) == ("SCREEN REPLY", "screen")


def test_codex_rollout_where_the_prompt_occurs_twice_is_not_adopted(tmp_path, monkeypatch):
    first = _sample(1)
    user = next(ln for ln in first if PONG_PROMPT in ln and '"role": "user"' in ln)
    lines = first[:first.index(user) + 1] + [user] + first[first.index(user) + 1:]
    c, agent, seen = _codex(tmp_path, monkeypatch, lines)
    assert agent.ask_reply(PONG_PROMPT, 5).source == "screen"


def test_codex_process_holding_two_rollouts_is_not_adopted(tmp_path, monkeypatch):
    c, agent, seen = _codex(tmp_path, monkeypatch, _sample(1), files=2)
    assert agent.ask_reply(PONG_PROMPT, 5).source == "screen"


def test_codex_process_that_changed_is_not_looked_at_again(tmp_path, monkeypatch):
    """A different start time for the pid (the pid was reused): the turn stays with the screen and the
    rollouts are not looked for any more."""
    c, agent, seen = _codex(tmp_path, monkeypatch, _sample(1), starts=(777, 888))
    assert agent.ask_reply(PONG_PROMPT, 5).source == "screen"
    assert seen["open"] == 1  # only the pre-submit look


def test_codex_bound_before_submitting_does_not_take_this_path(tmp_path, monkeypatch):
    c, agent, path = make(tmp_path, kind="codex")
    path.write_text("".join(_sample(1)))
    calls = []
    monkeypatch.setattr(A.locate, "process_start", lambda pid, root="/proc": calls.append(pid) or 1)

    def on_prompt(fake, name, text, wait):
        with open(path, "a") as f:
            f.write("".join(_sample(1)))
        fake.set_status(name, "done")

    c.on_prompt = on_prompt
    assert agent.ask_reply(PONG_PROMPT, 5).source == "session_log"
    assert calls == []


def test_codex_unrecognized_screen_still_submits_nothing(tmp_path, monkeypatch):
    c, agent, seen = _codex(tmp_path, monkeypatch, _sample(1))
    c.recent = (Path(__file__).parent / "fixtures/screens/codex/trust-dialog.txt").read_text()
    with pytest.raises(AgentBusyError):
        agent.ask(PONG_PROMPT, 2)
    assert not c.args_of("agent_prompt") and not seen["submitted"]


def test_codex_timeout_after_adoption_gives_a_log_pending(tmp_path, monkeypatch):
    """`pending` of an adopted turn: from the log at position 0; a screen `pending` is not rebound."""
    lines = [ln for ln in _sample(1) if '"task_complete"' not in ln]  # the turn has not ended
    c, agent, seen = _codex(tmp_path, monkeypatch, lines)
    c.on_prompt, real = None, c.on_prompt

    def slow(fake, name, text, wait):
        real(fake, name, text, wait)
        fake.set_status(name, "working")
        return herr(H.TIMEOUT, "timeout")

    c.on_prompt = slow
    with pytest.raises(AgentTimeoutError) as ei:
        agent.ask(PONG_PROMPT, 0.5)
    assert {k: ei.value.pending[k] for k in ("source", "mark")} == {"source": "session_log", "mark": 0}
