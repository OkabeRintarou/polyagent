"""Screen capture (D12), used when a kind has no bound session log.

The baseline is the tail of the pre-submit screen content (above the footer), together with the
number n of times that block occurs on the pre-submit screen: the n-th occurrence is the pre-submit
position. The reply is what follows this prompt's echo (first line, first 60 characters) after the
n-th or a later occurrence, minus the prompt's continuation lines, up to the kind's footer marker,
with bullet prefixes and timestamp lines removed. Earlier occurrences are followed by old content,
whose echo may read the same (review N1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

ECHO_CHARS = 60
BASELINE_LINES = 3
BULLETS = ("•", "●", "⏺")
TIMESTAMP_RE = re.compile(r"^\s*(?:Worked|Ran|Thought|Cooked|Churned) for \S+.*?\d{1,2}:\d{2}(?:\s*[AP]M)?\s*$")


@dataclass(frozen=True)
class ScreenCapture:
    footer: Sequence[re.Pattern[str]]

    def _content(self, lines: list[str]) -> list[str]:
        """Lines above the first footer line."""
        for i, line in enumerate(lines):
            if any(p.search(line) for p in self.footer):
                return lines[:i]
        return lines

    def baseline(self, screen: str) -> list[str]:
        content = [line.rstrip() for line in self._content(screen.splitlines())]
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

    def _candidates(self, lines: list[str], baseline: list[str], count: int) -> list[list[str]]:
        """The text after the ``count``-th and each later occurrence of the baseline block, the last
        occurrence first. A screen that scrolled shows fewer early occurrences, so the ``count``-th one
        then lies at or after the pre-submit position: nothing found, or still the right place."""
        if not baseline:
            return [lines]
        ends = self._ends(lines, baseline)[max(count, 1) - 1:]
        return [lines[e + 1:] for e in reversed(ends)]

    @staticmethod
    def _echo_key(echo_lines: list[str]) -> str:
        return next((line.strip() for line in echo_lines if line.strip()), "")[:ECHO_CHARS]

    def _locate_echo(self, screen: str, baseline: list[str], echo_lines: list[str], count: int) -> list[str] | None:
        """Lines from this prompt's echo on: after the last baseline occurrence, from the ``count``-th
        on, that is followed by the echo (identical questions and answers repeat the block, C11, N1)."""
        key = self._echo_key(echo_lines)
        if not key:
            return None
        for after in self._candidates(screen.splitlines(), baseline, count):
            start = next((i for i, line in enumerate(after) if key in line), None)
            if start is not None:
                return after[start:]
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
        kept = [line.rstrip() for line in self._content(body) if not TIMESTAMP_RE.match(line)]
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
