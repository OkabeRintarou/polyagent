"""Prompt sanitisation (design D10): the same under both tool policies."""

from __future__ import annotations

import re
import unicodedata

from . import kinds as K
from .errors import InvalidPromptError

MAX_PROMPT_BYTES = 100 * 1024
_FORBIDDEN_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # C0 except \t \n, DEL, C1
_SKIPPED_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cf"})


def _skipped(ch: str) -> bool:
    """D10 step 3: characters skipped before the first effective one — Unicode separators (Zs, Zl,
    Zp), format characters (Cf: BOM, zero-width, direction marks), newline and tab. JavaScript's
    trim() removes the BOM, Python's lstrip() does not (review C1); skipping by category covers
    every implementation's notion of blank."""
    return ch in "\n\t" or unicodedata.category(ch) in _SKIPPED_CATEGORIES


def first_effective(text: str) -> str:
    """The first character that is not skipped, or "" when there is none."""
    return next((ch for ch in text if not _skipped(ch)), "")


def sanitize(adapter: K.KindAdapter, text: object) -> str:
    """Return the text to submit, or raise ``InvalidPromptError``; nothing is sent on error."""
    if not isinstance(text, str):
        raise InvalidPromptError("the prompt must be a string")
    text = text.replace("\r\n", "\n")
    bad = _FORBIDDEN_RE.search(text)
    if bad:
        raise InvalidPromptError(f"the prompt contains a control character (U+{ord(bad.group()):04X})")
    first = first_effective(text)
    if not first:
        raise InvalidPromptError("the prompt is empty")
    guard = adapter.prompt_guard
    if first in guard.rejected:
        task = guard.rejected[first]
        why = f"unverified, task {task}" if task else "no guard works for it"
        raise InvalidPromptError(f"{adapter.kind} cannot safely receive a prompt starting with {first!r} ({why})")
    if first in guard.applies_to:
        if guard.method == K.GUIDE_LINE:
            text = f"{K.GUIDE_LINE_TEXT}\n{text}"
        elif guard.method == K.LEADING_SPACE:
            text = " " + text
        else:  # pragma: no cover - table invariant
            raise AssertionError(guard.method)
    limit = max_bytes(adapter)
    if len(text.encode("utf-8")) > limit:
        why = "" if guard.max_bytes is None else f"; {adapter.kind} does not take a longer prompt as message text"
        raise InvalidPromptError(f"the prompt is longer than {limit} bytes (UTF-8){why}")
    return text


def max_bytes(adapter: K.KindAdapter) -> int:
    """The largest prompt ``adapter``'s kind takes, in UTF-8 bytes after the guard (D10 step 5)."""
    kind_limit = adapter.prompt_guard.max_bytes
    return MAX_PROMPT_BYTES if kind_limit is None else min(MAX_PROMPT_BYTES, kind_limit)


def max_text_bytes(adapter: K.KindAdapter) -> int:
    """The largest text that fits :func:`max_bytes` whatever guard it gets."""
    guard = adapter.prompt_guard
    reserve = len(K.GUIDE_LINE_TEXT.encode("utf-8")) + 1 if guard.method == K.GUIDE_LINE else 1
    return max_bytes(adapter) - reserve
