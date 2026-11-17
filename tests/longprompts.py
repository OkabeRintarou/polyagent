"""Long multi-line prompts for task 13.14 (the discussion engine sends prompts of up to 64 KiB).

Deterministic, so the unit tests can rebuild the exact text of a recorded sample and its digest.
"""

from __future__ import annotations

FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit "


def _wrap(token: str, body: list[str]) -> str:
    head = ("This message tests long input. Ignore the numbered filler lines and reply with exactly one word: "
            f"{token}")
    return "\n".join([head, *body, f"End of the test message. Reply with exactly one word: {token}"])


def many_lines(token: str, lines: int = 240) -> str:
    """More than 200 short lines (about 7 KiB)."""
    return _wrap(token, [f"filler line {i:03d}: nothing to do here" for i in range(1, lines + 1)])


def big(token: str, size: int = 62 * 1024) -> str:
    """A multi-line prompt of about ``size`` bytes (UTF-8), lines of about 200 characters."""
    body: list[str] = []
    while len(_wrap(token, body).encode()) < size:
        body.append(f"filler line {len(body) + 1:03d}: " + FILLER * 3)
    return _wrap(token, body[:-1])


def exact(token: str, size: int) -> str:
    """A multi-line prompt of exactly ``size`` bytes (UTF-8, ASCII text): :func:`big` padded in its last
    filler line."""
    text = big(token, size)
    deficit = size - len(text.encode())
    lines = text.split("\n")
    lines[-2] += "z" * deficit
    out = "\n".join(lines)
    assert len(out.encode()) == size
    return out
