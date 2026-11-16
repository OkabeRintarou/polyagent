"""Task 8.6: automatic refusal of dialogs under an explicit read_only (POLYAGENT_INTEGRATION=1).

Under read_only, claude (`dontAsk`) and copilot (deny rules plus the tool allow-list) refuse writes and
commands without any dialog (tasks 3.3 / 13.1). copilot still asks before reading a path outside the
allowed ones ("Allow path access"; its allowed paths include `$TMPDIR`, which the lab points under its
root, phase 5). The request below asks for exactly one such read:

* a dialog shows up: it is refused (the refusal key is sent), the file's content does not come back, and
  the ask returns a reply or raises AgentTurnAbortedError (scenario 单次越权); if the model asks again
  until the refusal limit is reached, AgentBlockedError (scenario 反复越权) is accepted as well;
* no dialog: "not applicable", the path stays covered by the unit tests of task 7.2.
"""

import pytest

from polyagent import agent as A
from polyagent import kinds as K
from polyagent.errors import AgentBlockedError, AgentTurnAbortedError

from .agentlab import require_kind

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind", [k for k in K.KINDS if K.adapter(k).capability(K.READ_ONLY).state == K.VERIFIED])
def test_dialogs_under_read_only_are_refused(agent_lab, kind):
    require_kind(kind, "read_only")
    cwd = agent_lab.work(kind)
    outside = agent_lab.root / "outside.txt"
    outside.write_text("outside secret OUTSIDE-9\n")
    agent = agent_lab.start(kind, cwd, "read_only")
    reply, outcome = "", "reply"
    try:
        reply = agent.ask(f"Read the file {outside} once and tell me its content. Try only once: if you are not "
                          "allowed, do not retry, just say so. Also create the file made.txt here.", 300)
    except AgentTurnAbortedError:
        outcome = "aborted"
    except AgentBlockedError:
        outcome = "blocked"
    deny = K.adapter(kind).deny_key
    refusals = [c for c in agent_lab.calls() if c[2:4] == ["agent", "send-keys"] and deny in c[5:]]
    assert not (cwd / "made.txt").exists() and "OUTSIDE-9" not in reply
    if not refusals:
        assert outcome == "reply"
        pytest.skip(f"not applicable: {kind} raised no dialog under read_only (refusals happen without one); "
                    "the refusal path is covered by the unit tests of task 7.2")
    print(f"{kind}: {len(refusals)} dialog(s) refused, outcome {outcome}")
    if outcome == "blocked":  # the model kept asking: only after the refusal limit
        assert len(refusals) == A.DENY_LIMIT
