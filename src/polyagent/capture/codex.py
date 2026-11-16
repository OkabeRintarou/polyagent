"""codex rollout adapter (D12): user messages are `response_item` messages with role user; the turn
ends with `task_complete` (final text in `last_agent_message`) or `turn_aborted`."""

from __future__ import annotations

from .base import Judgement, LogAdapter, Outcome, prompt_digest, text_parts

_END = ("task_complete", "turn_aborted")


def _payload(e: dict) -> dict:
    p = e.get("payload")
    return p if isinstance(p, dict) else {}


class CodexLog(LogAdapter):
    kind = "codex"

    def _user_text(self, e: dict) -> str | None:
        p = _payload(e)
        if e.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
            return text_parts(p.get("content"))
        return None

    def _end(self, e: dict) -> str | None:
        p = _payload(e)
        return p.get("type") if e.get("type") == "event_msg" and p.get("type") in _END else None

    def judge(self, mark: int, digest: str) -> Judgement:
        entries = self.log.entries()
        start = None
        for i in range(mark, len(entries)):
            text = self._user_text(entries[i])
            if text is not None and prompt_digest(text) == digest:
                start = i
                break
        if start is None:
            return Judgement(Outcome.NOT_SEEN)
        for e in entries[start + 1:]:
            end = self._end(e)
            if end == "turn_aborted":
                return Judgement(Outcome.ABORTED)
            if end == "task_complete":
                text = _payload(e).get("last_agent_message")
                # A local shell pass-through also completes a task, with an empty message (D12).
                if isinstance(text, str) and text.strip():
                    return Judgement(Outcome.REPLY, text)
                return Judgement(Outcome.ABORTED)
        return Judgement(Outcome.IN_PROGRESS)

    def turn_open(self, now: float | None = None) -> bool:
        entries = self.log.entries()
        for e in reversed(entries):
            if self._end(e):
                return False
            p = _payload(e)
            if e.get("type") == "event_msg" and p.get("type") == "task_started":
                return True
        return False
