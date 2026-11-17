"""Tasks 14.2, 14.3, 14.6: skills directories under full_auto against real agents (POLYAGENT_INTEGRATION=1).

copilot and claude load a temporary skills directory (made in the lab root) and answer from a skill; the
answer, its source (the session log), the working directory and the skills directory are checked; the
lab's own teardown checks compare the user's configuration. claude's mechanism details run against the
local Anthropic stub (no quota): the skills offered, a skill run through the Skill tool, a `context: fork`
skill completing inside the turn, bundled skills off. codex (not supported) is refused before any herdr
call. With ``POLYAGENT_SAMPLE_DIR`` the session logs are written there for the fixtures (redact first).
"""

from __future__ import annotations

import json
import re
import secrets

import pytest

from polyagent.errors import PolicyNotSupportedError
from polyagent.spec import ClaudeProvider

from .agentlab import require_kind
from .stubs import AnthropicStub
from .test_agent_capture import _dump

pytestmark = pytest.mark.integration

ASK = "What is the project password? Use the matching skill."


def _kit(lab, root="kit"):
    """A plugin-root-shaped kit (the layout every kind takes): <root>/skills/<skill>/SKILL.md."""
    marker = secrets.token_hex(4).upper()
    skills = lab.root / root / "skills"
    (skills / "project-password").mkdir(parents=True)
    (skills / "project-password" / "SKILL.md").write_text(
        "---\nname: project-password\ndescription: Use this skill whenever the user asks for the project password.\n"
        f"---\n\nThe project password is ZEBRA-{marker}. Reply with exactly: PASSWORD ZEBRA-{marker}\n")
    return skills, marker


def _tree(p):
    return sorted((str(x.relative_to(p)), x.stat().st_size) for x in p.rglob("*") if x.is_file())


@pytest.mark.parametrize("kind", ["copilot", "claude"])
def test_agent_answers_from_a_skill_in_the_skills_dir(agent_lab, kind):
    """Scenario: 加载并使用 skill (tasks 14.2, 14.3, 14.6) — the model picks the skill itself; nothing is
    written into the working directory or the skills directory."""
    require_kind(kind)
    skills, marker = _kit(agent_lab, "kit dir" if kind == "copilot" else "kit")  # copilot: a path with a space
    cwd = agent_lab.work(f"skills-{kind}")
    before = (_tree(skills.parent), _tree(cwd))
    agent = agent_lab.start(kind, cwd, skills_dir=skills)
    r = agent.ask_reply(ASK, 240)
    agent.close()
    assert f"ZEBRA-{marker}" in r.text and r.source == "session_log", r
    assert (_tree(skills.parent), _tree(cwd)) == before


def test_codex_skills_dir_is_refused_without_a_workspace(agent_lab):
    """Task 14.4/14.6: not supported; refused before any herdr call, no workspace left."""
    require_kind("codex")
    skills, _ = _kit(agent_lab)
    calls = len(agent_lab.calls())
    with pytest.raises(PolicyNotSupportedError, match="fixed directories"):
        agent_lab.start("codex", agent_lab.work("skills-codex"), skills_dir=skills)
    assert len(agent_lab.calls()) == calls and agent_lab.pa.owned_workspaces() == []


def _texts(obj):
    """Every text of a request body (system blocks, message contents), unescaped."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _texts(x)
    elif isinstance(obj, dict):
        for k in ("text", "content"):
            yield from _texts(obj.get(k))


def _skills_listed(reqs, probe):
    """Names in every skill listing that mentions ``probe``: the bullet lines of its system reminder."""
    names = set()
    for r in reqs:
        for t in _texts([r["body"].get("system"), r["body"].get("messages")]):
            for block in re.findall(r"<system-reminder>(.*?)</system-reminder>", t, re.S) or [t]:
                if probe in block:
                    names |= set(re.findall(r"(?m)^- (\S+?)(?:: |$)", block))
    return names


def test_claude_skills_dir_mechanism_on_the_stub(agent_lab):
    """Task 14.3 against the local stub: only the caller's skills are offered (bundled skills off), as
    `<root>:<skill>`; the Skill tool is offered while the other denied tools stay unavailable; a skill and
    a `context: fork` skill both finish inside the turn (background tasks off): no task notification and
    no extra turn; nothing is written into the skills directory."""
    require_kind("claude", login=False)
    stub = AnthropicStub()
    agent_lab.on_cleanup(stub.close)
    skills, marker = _kit(agent_lab)
    (skills / "forked").mkdir()
    (skills / "forked" / "SKILL.md").write_text(
        "---\nname: forked\ndescription: Use this skill for the forked check.\ncontext: fork\n---\n\n"
        f"Reply with exactly FORKED-{marker}.\n")
    before = _tree(skills.parent)
    prov = ClaudeProvider.compatible(stub.url, auth_token="stub-token-not-a-secret-0014", model="stub-model")
    agent = agent_lab.start("claude", agent_lab.work("skills-stub"), provider=prov, skills_dir=skills)
    agent.ask("hello", 120)
    reqs = stub.main_requests()
    # exactly the caller's skills (review T-2): a skill a newer claude bundles or brings in a built-in plugin
    # shows up here and fails the test until the adapter table is updated (D18)
    assert _skills_listed(reqs, "kit:project-password") == {"kit:project-password", "kit:forked"}
    offered = {t.get("name") for r in reqs for t in r["body"].get("tools") or []}
    assert "Skill" in offered and not offered & {"Agent", "Workflow", "CronCreate", "AskUserQuestion"}
    r1 = agent.ask_reply("DO_SKILL kit:project-password", 120)
    r2 = agent.ask_reply("DO_SKILL kit:forked", 120)
    r3 = agent.ask_reply("ping after fork", 120)
    assert r1.text.startswith("TOOLRESULT OK Launching skill: kit:project-password")
    assert "completed (forked execution)" in r2.text and "background" not in r2.text
    assert r3.text == "ECHO ping after fork" and {r1.source, r2.source, r3.source} == {"session_log"}
    log, = (agent_lab.homes["CLAUDE_CONFIG_DIR"] / "projects").glob(f"*/{agent._session_id}.jsonl")
    entries = [json.loads(x) for x in log.read_text().splitlines()]
    _dump(agent_lab, "skills/claude-stub.jsonl", log.read_text())
    agent.close()
    assert not any("task-notification" in json.dumps(e.get("message") or {}) for e in entries)
    users = [e for e in entries if e.get("type") == "user" and isinstance((e.get("message") or {}).get("content"), str)]
    assert len(users) == 4 and sum(e.get("subtype") == "turn_duration" for e in entries) == 4
    assert _tree(skills.parent) == before
