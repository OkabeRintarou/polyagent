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

from longprompts import big, many_lines
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
        steps.append(("edge20k", big("LONGEDGE", K.COPILOT_PASTE_LIMIT), "LONGEDGE"))
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
    if kind == "copilot":
        assert by["big62k"]["outcome"] == "InvalidPromptError", by["big62k"]
    if kind == "codex":  # 13.15: the rollout is opened only once the first message is submitted
        assert obs["rollout_open_before_first"] == [] and obs["rollout_first_seen"].get("at") is not None, obs
