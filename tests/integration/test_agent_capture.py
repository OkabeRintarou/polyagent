"""Tasks 13.14, 13.15, 13.16 against real agents (POLYAGENT_INTEGRATION=1; consumes model quota).

One agent per kind answers four prompts: a one-word PONG, a multi-step markdown answer, a prompt of
more than 200 lines and one of about 62 KiB (the discussion engine's coordinator inputs go up to
64 KiB). Recorded per prompt: where the reply came from, and how the session log holds the user
message (13.14); for codex, when the agent process opens its rollout file (13.15); the screens after
each turn (13.16). With ``POLYAGENT_SAMPLE_DIR`` set, the raw observations, screens and session log
are written there (outside the repository) as material for ``tests/fixtures``, which must be redacted
by hand before anything is committed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from longprompts import big, exact, many_lines
from polyagent._prompt import sanitize
from polyagent.capture import locate
from polyagent.capture.base import prompt_digest
from polyagent.capture.claude import ClaudeLog
from polyagent.capture.codex import CodexLog
from polyagent.errors import PolyAgentError
from polyagent import kinds as K

from .agentlab import require_kind
from .test_agent_ask import FILES, MULTI, PONG

pytestmark = pytest.mark.integration

SAMPLES = os.environ.get("POLYAGENT_SAMPLE_DIR")
POLICY = {"codex": None, "copilot": "read_only", "claude": "read_only"}  # read_only as in a discussion
PLACEHOLDER = re.compile(r"\[Pasted|\[Pasted Content|\[\.\.\.|chars\]|lines\]")


def _user_texts(kind: str, entries: list[dict]) -> list[str]:
    out: list[str] = []
    for e in entries:
        if kind == "codex":
            t = CodexLog("")._user_text(e)
        elif kind == "claude":
            t = ClaudeLog._user_text(e) if not e.get("isSidechain") and not e.get("agentId") else None
        else:
            d = e.get("data") if isinstance(e.get("data"), dict) else {}
            t = d.get("content") if e.get("type") == "user.message" and not e.get("agentId") else None
        if isinstance(t, str):
            out.append(t)
    return out


def _dump(lab, name: str, data) -> None:
    """Write a sample outside the repository with the lab's root and session names replaced (they must
    not escape the lab, and are the first step of the redaction)."""
    if not SAMPLES:
        return
    text = data if isinstance(data, str) else json.dumps(data, indent=1, ensure_ascii=False)
    for old, new in ((str(lab.root), "/tmp/LAB_ROOT"), (lab.root.name, "LAB_ROOT"), (lab.name, "polyagent-test-LAB")):
        text = text.replace(old, new)
    p = Path(SAMPLES) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.parametrize("kind", ["codex", "copilot", "claude"])
def test_long_prompts_logs_and_screens(agent_lab, kind):
    require_kind(kind, POLICY[kind])
    agent = agent_lab.start(kind, agent_lab.work(kind, files=FILES), POLICY[kind])
    adapter = K.adapter(kind)
    obs: dict = {"kind": kind, "policy": POLICY[kind], "steps": []}

    # 13.15: is the rollout open before the first message, and when does it appear?
    seen: dict = {}
    stop = threading.Event()
    if kind == "codex":
        sessions = Path(agent_lab.homes["CODEX_HOME"]) / "sessions"
        obs["rollout_open_before_first"] = locate.open_rollouts(agent._agent_pids())
        obs["rollout_files_before_first"] = len(list(sessions.rglob("rollout-*.jsonl"))) if sessions.exists() else 0

        def watch(t0: float) -> None:
            while not stop.is_set():
                found = locate.open_rollouts(agent._agent_pids())
                if found and "at" not in seen:
                    seen["at"] = round(time.monotonic() - t0, 2)
                    seen["user_messages_at_first_sight"] = len(_user_texts(kind, CodexLog(found[0]).log.entries()))
                    seen["path"] = found[0]
                time.sleep(0.05)

    steps = [("pong", PONG, "PONG"), ("multi", MULTI, "41"), ("lines240", many_lines("LONGLINES"), "LONGLINES")]
    if kind == "copilot":  # just under copilot's paste limit (task 13.14)
        steps.append(("edge20k", exact("LONGEDGE", K.COPILOT_PASTE_LIMIT), "LONGEDGE"))
    steps.append(("big62k", big("LONGBIG"), "LONGBIG"))
    for step, text, token in steps:
        try:
            sent = sanitize(adapter, text)
        except PolyAgentError as e:  # refused before anything is sent
            obs["steps"].append({"step": step, "bytes": len(text.encode()), "outcome": type(e).__name__})
            continue
        log = agent._bind_log()
        mark = len(log.log.entries()) if log is not None else 0
        rec: dict = {"step": step, "lines": sent.count("\n") + 1, "bytes": len(sent.encode()), "bound_before": log is not None}
        watcher = None
        if kind == "codex" and step == "pong":
            t0 = time.monotonic()
            watcher = threading.Thread(target=watch, args=(t0,), daemon=True)
            watcher.start()
        started = time.monotonic()
        try:
            reply = agent.ask_reply(text, 300)
            rec.update(outcome="reply", source=reply.source, token=token in reply.text, chars=len(reply.text))
        except PolyAgentError as e:
            rec.update(outcome=type(e).__name__, message=str(e)[:300])
        rec["seconds"] = round(time.monotonic() - started, 1)
        if watcher is not None:
            stop.set()
            watcher.join(5)
            obs["rollout_first_seen"] = dict(seen)
        log = agent._bind_log()
        if log is not None:
            users = _user_texts(kind, log.log.entries()[mark:])
            rec["log_user_messages"] = len(users)
            rec["log_user_bytes"] = [len(u.encode()) for u in users]
            rec["log_verbatim"] = any(u.strip() == sent.strip() for u in users)
            rec["log_digest_match"] = any(prompt_digest(u) == prompt_digest(sent) for u in users)
            rec["log_placeholder"] = any(PLACEHOLDER.search(u) and not PLACEHOLDER.search(sent) for u in users)
        for source, lines in (("recent-unwrapped", 200), ("visible", None)):
            try:
                _dump(agent_lab, f"{kind}/{step}-{source}.txt", agent.read(source, lines))
            except PolyAgentError as e:
                _dump(agent_lab, f"{kind}/{step}-{source}.err", repr(e))
        obs["steps"].append(rec)
    log = agent._bind_log()
    if log is not None and SAMPLES and log.exists():
        _dump(agent_lab, f"{kind}/session-log.jsonl", Path(log.path).read_text())
    obs["log_bound_at_end"] = log is not None
    _dump(agent_lab, f"{kind}/observations.json", obs)
    agent.close()
    print(json.dumps(obs, indent=1))

    by = {r["step"]: r for r in obs["steps"]}
    # 13.14: long prompts reach the agent as one message, logged as written; copilot takes at most 20 KiB.
    long_steps = ("lines240", "edge20k") if kind == "copilot" else ("lines240", "big62k")
    for step in long_steps:
        r = by[step]
        assert r["outcome"] == "reply" and r["source"] == "session_log" and r["token"], r
        assert r["log_user_messages"] == 1 and r["log_digest_match"], r
    if kind == "copilot":  # exactly at the limit: delivered as text, logged as those bytes (review T3)
        assert by["edge20k"]["bytes"] == K.COPILOT_PASTE_LIMIT == by["edge20k"]["log_user_bytes"][0], by["edge20k"]
        assert by["edge20k"]["log_verbatim"], by["edge20k"]
        assert by["big62k"]["outcome"] == "InvalidPromptError", by["big62k"]
    if kind == "codex":  # 13.15: the rollout is opened only once the first message is submitted, and the
        # library binds it then: the first turn's reply comes from the session log
        assert obs["rollout_open_before_first"] == [] and obs["rollout_first_seen"].get("at") is not None, obs
        assert by["pong"]["source"] == "session_log", by["pong"]


def test_copilot_paste_limit_boundary_without_submitting(agent_lab):
    """Task 13.14 / review T3: where copilot stops taking a paste as text, reproducibly and without quota:
    text is pasted into the input box (`pane send-text`, never submitted) and the box is cleared again.
    20480 bytes stay text; 20481 bytes become a saved-file reference. Single- and multi-line alike."""
    require_kind("copilot", "read_only")
    agent = agent_lab.start("copilot", agent_lab.work("paste"), "read_only")
    seen = {}
    for size in (K.COPILOT_PASTE_LIMIT, K.COPILOT_PASTE_LIMIT + 1):
        for multiline in (False, True):
            text = exact("PASTE", size) if multiline else ("y" * 79 + " ") * (size // 80) + "y" * (size % 80)
            assert len(text.encode()) == size and ("\n" in text) is multiline
            assert agent_lab.herdr("pane", "send-text", agent.pane_id, text).returncode == 0
            time.sleep(2)  # the paste has settled into the input box (phase-8 probe)
            screen = agent.read("visible")
            # Kept as text, a long paste may be shown collapsed (`[Paste #1 - 113 lines]`); over the limit it
            # is `[Saved pasted content to workspace (…) id=…]`.
            assert any(x in screen for x in ("Saved pasted content", "[Paste #", "yyyy")), screen[-600:]
            seen[(size, multiline)] = "Saved pasted content" in screen
            agent.send_keys("ctrl+c")  # clears the input box; nothing was submitted
            time.sleep(1)
    agent.close()
    print(seen)
    assert seen == {(K.COPILOT_PASTE_LIMIT, False): False, (K.COPILOT_PASTE_LIMIT, True): False,
                    (K.COPILOT_PASTE_LIMIT + 1, False): True, (K.COPILOT_PASTE_LIMIT + 1, True): True}


def _status_line(screen: str) -> str | None:
    lines = [ln for ln in screen.splitlines() if ln.strip()]
    idx = max((i for i, ln in enumerate(lines) if ln.startswith("›")), default=None)
    return lines[idx + 1] if idx is not None and idx + 1 < len(lines) else None


CODEX_CASES = {
    "home": None,  # filled in the test: a directory under a temporary HOME of the pane
    "spaces": "dir with spaces",
    "long": "/".join(["a-rather-long-directory-name-segment"] * 5),
}


@pytest.mark.parametrize("case", ["home", "spaces", "long", "enter-title"])
def test_codex_status_line_samples(agent_lab, case):
    """Task 13.37 (after the rule change): real codex screens for the status-line rule, before and after a
    first turn. The second field is compared with the working directory (and with `~` for the home case);
    the result is written to the observations, and the idle screens must be recognized (D12)."""
    from polyagent.capture import ScreenCapture

    require_kind("codex")
    kw = {}
    if case == "home":  # the pane's HOME in the lab root; nothing is created in the user's real home
        home = agent_lab.root / "home"
        home.mkdir()
        cwd = home / "proj"
        cwd.mkdir()
        kw["env"] = {"HOME": str(home)}
    elif case == "enter-title":
        cwd = agent_lab.work("title")
    else:
        cwd = agent_lab.work(CODEX_CASES[case])
    agent = agent_lab.start("codex", cwd, **kw)
    cap = ScreenCapture(K.adapter("codex").footer, K.adapter("codex").footer_block)
    before = agent.read("recent-unwrapped", 200)
    # The session title is taken from the first exchange (`PONG` in the other cases).
    question = ("Reply with exactly these three words: Enter key handling" if case == "enter-title"
                else "Reply with exactly one word: PONG")
    reply = agent.ask_reply(question, 300)
    after = agent.read("recent-unwrapped", 200)
    for _ in range(30):  # the session title may be drawn a moment later
        fields = (_status_line(after) or "").strip().split(" · ")
        if case != "enter-title" or (len(fields) > 2 and "for agents" not in fields[2]):
            break
        time.sleep(0.5)
        after = agent.read("recent-unwrapped", 200)
    agent.close()
    obs = {"case": case, "cwd_len": len(str(cwd)), "source": reply.source}
    for name, screen in (("before", before), ("after", after)):
        status = _status_line(screen)
        field = status.strip().split(" · ")[1] if status and " · " in status else None
        obs[name] = {"recognized": cap.recognized(screen), "field_is_cwd": field == str(cwd),
                     "field_is_tilde_form": field is not None and case == "home" and field == "~/proj",
                     "field_len": len(field or "")}
        _dump(agent_lab, f"codex-status/{case}-{name}.txt", screen)
    _dump(agent_lab, f"codex-status/{case}-observations.json", obs)
    print(json.dumps(obs))
    assert reply.source == "session_log"
    assert obs["before"]["recognized"] and obs["after"]["recognized"], obs
