"""claude transcript adapter (D12). The main transcript only (sub-agent files `agent-*.jsonl` and
side-chain entries are ignored). A turn ends with the `system`/`turn_duration` entry; the reply is the
text of the last `end_turn` assistant entry; an API-error entry is an agent-level error; the
"[Request interrupted by user" entry is an abort."""

from __future__ import annotations

from .base import Judgement, LogAdapter, Outcome, prompt_digest, text_parts

INTERRUPTED = "[Request interrupted by user"


def _message(e: dict) -> dict:
    m = e.get("message")
    return m if isinstance(m, dict) else {}


class ClaudeLog(LogAdapter):
    kind = "claude"

    @staticmethod
    def _main(e: dict) -> bool:
        return not e.get("isSidechain") and not e.get("agentId")

    @staticmethod
    def _user_text(e: dict) -> str | None:
        """A user prompt entry (not a tool result, not meta, not an interruption marker)."""
        if e.get("type") != "user" or e.get("isMeta"):
            return None
        content = _message(e).get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return None
        text = text_parts(content, ("text",))
        if text is None or text.startswith(INTERRUPTED):
            return None
        return text

    @staticmethod
    def _interrupted(e: dict) -> bool:
        if e.get("type") != "user":
            return False
        text = text_parts(_message(e).get("content"), ("text",))
        return isinstance(text, str) and text.startswith(INTERRUPTED)

    @staticmethod
    def _turn_end(e: dict) -> bool:
        return e.get("type") == "system" and e.get("subtype") == "turn_duration"

    def judge(self, mark: int, digest: str) -> Judgement:
        entries = [e for e in self.log.entries()[mark:] if self._main(e)]
        start = next((i for i, e in enumerate(entries)
                      if (t := self._user_text(e)) is not None and prompt_digest(t) == digest), None)
        if start is None:
            return Judgement(Outcome.NOT_SEEN)
        reply = None
        for e in entries[start + 1:]:
            if self._interrupted(e):
                return Judgement(Outcome.ABORTED)
            if e.get("type") == "assistant" and e.get("isApiErrorMessage"):
                return Judgement(Outcome.ERROR, text_parts(_message(e).get("content"), ("text",)) or "API error")
            if e.get("type") == "assistant" and _message(e).get("stop_reason") == "end_turn":
                text = text_parts(_message(e).get("content"), ("text",))
                if text and text.strip():
                    reply = text
            if self._turn_end(e):
                return Judgement(Outcome.REPLY, reply) if reply is not None else Judgement(Outcome.ABORTED)
            if self._user_text(e) is not None:  # the next prompt arrived without an end marker
                return Judgement(Outcome.ABORTED) if reply is None else Judgement(Outcome.REPLY, reply)
        return Judgement(Outcome.IN_PROGRESS)

    def turn_open(self, now: float | None = None) -> bool:
        for e in reversed(self.log.entries()):
            if not self._main(e):
                continue
            if self._turn_end(e) or self._interrupted(e) or (e.get("type") == "assistant" and e.get("isApiErrorMessage")):
                return False
            if self._user_text(e) is not None:
                return True
        return False
