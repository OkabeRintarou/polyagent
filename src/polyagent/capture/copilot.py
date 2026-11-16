"""copilot `events.jsonl` adapter (D12, copilot rules from task 13.1).

Events carrying a top-level ``agentId`` belong to a sub-agent and are ignored everywhere. A turn of
this prompt runs from its (main) ``user.message`` to the first main ``session.usage_checkpoint``
(success) or ``session.error``. A ``system.notification`` starts a prompt-less turn of its own; a
prompt received while such a turn is still open cannot be attributed (``AMBIGUOUS``), and neither can
one that shares its turn with another prompt (copilot queues a prompt sent during a turn into it).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .base import Judgement, LogAdapter, Outcome, prompt_digest

END = "session.usage_checkpoint"
ERROR = "session.error"
USER = "user.message"
NOTIFICATION = "system.notification"
# Main-session events showing that a notification did start a turn (D11).
TURN_ACTIVITY = frozenset({"assistant.turn_start", "assistant.message", "tool.execution_start", "model.turn_started",
                           "model.model_call_started"})
NOTIFICATION_GRACE = 10.0  # seconds without activity after which a notification is taken as turn-less


def _data(e: dict) -> dict:
    d = e.get("data")
    return d if isinstance(d, dict) else {}


def _ts(e: dict) -> float | None:
    raw = e.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CopilotLog(LogAdapter):
    kind = "copilot"

    def _main(self) -> list[tuple[int, dict[str, Any]]]:
        return [(i, e) for i, e in enumerate(self.log.entries()) if not e.get("agentId")]

    def judge(self, mark: int, digest: str) -> Judgement:
        main = self._main()
        pos = next((k for k, (i, e) in enumerate(main) if i >= mark and e.get("type") == USER
                    and isinstance(_data(e).get("content"), str) and prompt_digest(_data(e)["content"]) == digest), None)
        if pos is None:
            return Judgement(Outcome.NOT_SEEN)
        # Received while a notification turn was open? A notification followed by no turn activity
        # for NOTIFICATION_GRACE before this prompt started no turn (the D11 pre-check rule) and does
        # not make the prompt ambiguous (phase-4 finding P4-2).
        user_ts = _ts(main[pos][1])
        activity = False
        for _, e in reversed(main[:pos]):
            t = e.get("type")
            if t in (END, ERROR):
                break
            if t == USER:  # an earlier prompt's turn was still open: both went into one turn (phase 5)
                return Judgement(Outcome.AMBIGUOUS)
            if t in TURN_ACTIVITY:
                activity = True
            if t == NOTIFICATION:
                ts = _ts(e)
                if activity or ts is None or user_ts is None or user_ts - ts < NOTIFICATION_GRACE:
                    return Judgement(Outcome.AMBIGUOUS)
                break
        final = None
        for _, e in main[pos + 1:]:
            t = e.get("type")
            if t == USER:  # another prompt joined this turn: the final message may answer either
                return Judgement(Outcome.AMBIGUOUS)
            if t == ERROR:
                msg = _data(e).get("message")
                return Judgement(Outcome.ERROR, msg if isinstance(msg, str) and msg else "agent error")
            if t == "assistant.message" and not _data(e).get("toolRequests"):
                content = _data(e).get("content")
                if isinstance(content, str) and content.strip():
                    final = content
            if t == END:
                return Judgement(Outcome.REPLY, final) if final is not None else Judgement(Outcome.ABORTED)
        return Judgement(Outcome.IN_PROGRESS)

    def turn_open(self, now: float | None = None) -> bool:
        """Working when the last main user message, or the last main notification, has no later
        end/error marker; a notification followed by no turn activity for 10 s does not count (D11)."""
        now = time.time() if now is None else now
        main = [e for _, e in self._main()]
        activity = False
        for e in reversed(main):
            t = e.get("type")
            if t in (END, ERROR):
                return False
            if t == USER:
                return True
            if t in TURN_ACTIVITY:
                activity = True
            if t == NOTIFICATION:
                if activity:
                    return True
                ts = _ts(e)
                return ts is None or now - ts < NOTIFICATION_GRACE
        return False
