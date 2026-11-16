"""Shared pieces of the reply sources (design D12): outcomes, prompt digests, an incremental JSONL reader."""

from __future__ import annotations

import enum
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable


class Outcome(enum.Enum):
    NOT_SEEN = "not_seen"  # this prompt's user message is not in the log (yet)
    IN_PROGRESS = "in_progress"  # user message seen, no end marker yet
    REPLY = "reply"  # final message of the turn
    ERROR = "error"  # the turn ended with an agent-level error
    ABORTED = "aborted"  # refused or interrupted, or ended without a final message
    AMBIGUOUS = "ambiguous"  # the log cannot tell which end marker belongs to this prompt


@dataclass(frozen=True)
class Judgement:
    outcome: Outcome
    text: str | None = None  # REPLY: the final message; ERROR: the agent's reason


def prompt_digest(text: str) -> str:
    """Digest of a prompt as the logs record it (surrounding whitespace dropped: codex and claude
    strip leading blanks, D10). Pending turns carry this instead of the prompt text (D1)."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class JsonlLog:
    """Append-only JSONL file read incrementally; only complete lines are parsed. Entries are
    numbered from 0, so ``len(entries)`` is a stable position (a mark).

    ``resolve`` (optional) is asked for the real path while ``path`` is empty or does not exist; the
    first answer replaces ``path`` (logs bound by session id appear with the first message, D12).
    Safe to share between threads of one handle; a file that shrank yields nothing new."""

    def __init__(self, path: str, resolve: Callable[[], str | None] | None = None):
        self.path = path
        self._resolve = resolve
        self._offset = 0
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def _locate(self) -> None:
        if self._resolve is not None and (not self.path or not os.path.exists(self.path)):
            found = self._resolve()
            if found:
                self.path, self._resolve = found, None

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            self._locate()
            try:
                with open(self.path, "rb") as f:
                    if os.fstat(f.fileno()).st_size < self._offset:
                        # Replaced or truncated: logs are append-only, so keep the entries (and the marks
                        # that point into them) and see nothing new rather than renumber.
                        return list(self._entries)
                    f.seek(self._offset)
                    data = f.read()
            except OSError:
                return list(self._entries)
            end = data.rfind(b"\n")
            if end >= 0:
                for raw in data[: end + 1].splitlines():
                    try:
                        item = json.loads(raw)
                    except ValueError:
                        item = {}
                    self._entries.append(item if isinstance(item, dict) else {})
                self._offset += end + 1
            return list(self._entries)

    def exists(self) -> bool:
        with self._lock:
            self._locate()
            return os.path.exists(self.path)


class LogAdapter:
    """Interface of the per-kind session-log adapters."""

    kind = ""

    def __init__(self, path: str, resolve: Callable[[], str | None] | None = None):
        self.log = JsonlLog(path, resolve)

    @property
    def path(self) -> str:
        return self.log.path

    def exists(self) -> bool:
        return self.log.exists()

    def mark(self) -> int:
        return len(self.log.entries())

    def judge(self, mark: int, digest: str) -> Judgement:  # pragma: no cover - interface
        raise NotImplementedError

    def turn_open(self, now: float | None = None) -> bool:
        """True while the agent's last turn has not ended (the pre-check's "working")."""
        raise NotImplementedError  # pragma: no cover - interface


def text_parts(content: Any, kinds: tuple[str, ...] = ("text", "input_text", "output_text")) -> str | None:
    """Plain text of a message content (a string, or a list of typed parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text") for p in content if isinstance(p, dict) and p.get("type") in kinds]
        if parts and all(isinstance(p, str) for p in parts):
            return "".join(parts)
    return None
