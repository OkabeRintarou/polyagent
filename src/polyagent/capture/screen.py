"""Screen capture (D12), used when a kind has no bound session log.

The baseline is the tail of the pre-submit screen content (above the footer), together with the
number n of times that block occurs on the pre-submit screen: the n-th occurrence is the pre-submit
position. The reply is what follows this prompt's echo (first line, first 60 characters) after the
n-th or a later occurrence, minus the prompt's continuation lines, up to the kind's footer (line
markers or a bottom footer block), with bullet prefixes and timestamp lines removed. Earlier occurrences are followed by old content,
whose echo may read the same (review N1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

ECHO_CHARS = 60
BASELINE_LINES = 3
BULLETS = ("•", "●", "⏺")
TIMESTAMP_RE = re.compile(r"^\s*(?:Worked|Ran|Thought|Cooked|Churned) for \S+.*?\d{1,2}:\d{2}(?:\s*[AP]M)?\s*$")


@dataclass(frozen=True)
class ScreenCapture:
    """``footer``: line markers; ``block``: a bottom footer block (``kinds.FooterBlock``), which
    replaces the markers. Echo and reply only come from the content area: after the baseline position
    and before the footer (D12 "内容区和页脚"); text in the input line is never an echo."""

    footer: Sequence[re.Pattern[str]]
    block: Any = None

    def _block_start(self, lines: list[str]) -> int | None:
        b = self.block
        idx = next((i for i in range(len(lines) - 1, -1, -1) if b.input_line.search(lines[i])), None)
        if idx is None:
            return None
        after = [line for line in lines[idx + 1:] if line.strip()]
        if len(after) > b.max_after:
            return None
        if b.status and not any(m.search(line) for line in after for m in b.status):
            return None
        return idx

    def content_end(self, lines: list[str], start: int = 0) -> int | None:
        """Index where the content area ends (the footer's first line) for content starting at
        ``start``; ``None`` when the kind uses a footer block and the screen has none it recognizes."""
        if self.block is not None:
            end = self._block_start(lines)
            return None if end is None else max(end, start)
        return next((i for i in range(start, len(lines)) if any(p.search(lines[i]) for p in self.footer)),
                    len(lines))

    def recognized(self, screen: str) -> bool:
        """The screen shows the kind's input area (always true for line markers)."""
        return self.content_end(screen.splitlines()) is not None

    def baseline(self, screen: str) -> list[str]:
        lines = screen.splitlines()
        end = self.content_end(lines)
        content = [line.rstrip() for line in lines[:len(lines) if end is None else end]]
        tail = [line for line in content if line.strip()]
        return tail[-BASELINE_LINES:]

    @staticmethod
    def _ends(lines: list[str], baseline: list[str]) -> list[int]:
        """Index of the last line of each occurrence of the baseline block, in screen order."""
        stripped = [line.rstrip() for line in lines]
        nonblank = [i for i, line in enumerate(stripped) if line.strip()]
        n = len(baseline)
        return [nonblank[k + n - 1] for k in range(len(nonblank) - n + 1)
                if [stripped[i] for i in nonblank[k:k + n]] == baseline]

    def count(self, screen: str, baseline: list[str]) -> int:
        """How many times the baseline block occurs on ``screen`` (recorded before submitting)."""
        return len(self._ends(screen.splitlines(), baseline)) if baseline else 0

    def _starts(self, lines: list[str], baseline: list[str], count: int) -> list[int]:
        """Where the content after the ``count``-th and each later occurrence of the baseline block
        starts, the last occurrence first. A screen that scrolled shows fewer early occurrences, so the
        ``count``-th one then lies at or after the pre-submit position: nothing found, or still the
        right place."""
        if not baseline:
            return [0]
        ends = self._ends(lines, baseline)[max(count, 1) - 1:]
        return [e + 1 for e in reversed(ends)]

    @staticmethod
    def _echo_key(echo_lines: list[str]) -> str:
        return next((line.strip() for line in echo_lines if line.strip()), "")[:ECHO_CHARS]

    def _locate_echo(self, screen: str, baseline: list[str], echo_lines: list[str], count: int) -> list[str] | None:
        """The content lines from this prompt's echo on: after the last baseline occurrence, from the
        ``count``-th on, whose content area holds the echo (identical questions and answers repeat the
        block, C11, N1). ``None`` without an echo or a recognizable footer."""
        key = self._echo_key(echo_lines)
        if not key:
            return None
        lines = screen.splitlines()
        for start in self._starts(lines, baseline, count):
            end = self.content_end(lines, start)
            if end is None:
                return None
            hit = next((i for i in range(start, end) if key in lines[i]), None)
            if hit is not None:
                return lines[hit:end]
        return None

    def echo_seen(self, screen: str, baseline: list[str], echo_lines: list[str], *, count: int) -> bool:
        """Delivery evidence on the screen: the echo after the pre-submit position (D11)."""
        return self._locate_echo(screen, baseline, echo_lines, count) is not None

    def extract(self, screen: str, baseline: list[str], echo_lines: list[str], *, count: int) -> str | None:
        """The reply to the prompt whose submitted lines are ``echo_lines``, or ``None``. ``count`` is
        :meth:`count` of the baseline on the pre-submit screen."""
        after = self._locate_echo(screen, baseline, echo_lines, count)
        if after is None:
            return None
        body = after[1:]
        # Skip the rest of the echo: the prompt's further lines and the screen's wrapping of long lines,
        # matched in order against what the echo has not shown yet. A reply line that merely occurs
        # somewhere in the prompt (a one-word answer the prompt asked for) is not part of the echo.
        flat = " ".join(" ".join(line.split()) for line in echo_lines if line.strip())
        shown = " ".join(after[0].split())
        key = self._echo_key(echo_lines)
        seg = shown[shown.find(" ".join(key.split())):]
        pos = len(seg) if flat.startswith(seg) else len(" ".join(key.split()))
        while body:
            nxt = next((line for line in body if line.strip()), None)
            if nxt is None:
                break
            words = " ".join(nxt.split())
            rest = flat[pos:].lstrip()
            if not rest.startswith(words):
                break
            pos = len(flat) - len(rest) + len(words)
            body = body[body.index(nxt) + 1:]
        kept = [line.rstrip() for line in body if not TIMESTAMP_RE.match(line)]
        indent = min((len(x) - len(x.lstrip()) for x in kept if x.strip()), default=0)
        out: list[str] = []
        hanging = 0
        for line in (x[indent:] for x in kept):
            bullet = next((b for b in BULLETS if line.startswith(b)), None)
            if bullet is not None:
                line, hanging = line[len(bullet):].lstrip(" "), len(bullet) + 1
            elif hanging and line.startswith(" " * hanging):
                line = line[hanging:]
            out.append(line)
        text = "\n".join(out).strip()
        return text or None
