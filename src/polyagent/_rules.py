"""Name rules and library-generated error codes shared by ``herdr``, ``locks`` and ``manager``.

Depends only on ``errors`` so that every layer can use it (see review C12 / A3).
"""

from __future__ import annotations

import re

from .errors import InvalidConfigError

KINDS = ("codex", "copilot", "claude")  # supported agent kinds; adapters live in ``kinds``
# Kind names that v1 refuses with a message of their own (D7 "支持的 kind").
UNSUPPORTED_KINDS = ("cursor", "cursor-agent")
CURSOR_UNSUPPORTED = (
    "cursor is not supported in polyagent v1: its self-update and its writes to the user's home directory "
    "cannot be isolated, and its prompt guards do not hold; supporting it again is on the roadmap (see README)"
)

SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
DISCUSSION_ID_RE = re.compile(r"[0-9a-f]{32}")

# Error codes produced by the library itself (herdr's own codes live in ``herdr``).
USAGE = "usage"
BAD_RESPONSE = "bad_response"
UNKNOWN = "unknown"
UNRESPONSIVE = "unresponsive"
LAUNCH_FAILED = "launch_failed"
UNSUPPORTED_VERSION = "unsupported_version"
DEFAULT_SESSION_REFUSED = "default_session_refused"
SERVER_LOCK_TIMEOUT = "server_lock_timeout"
SERVER_START_FAILED = "server_start_failed"
SERVER_START_TIMEOUT = "server_start_timeout"
UNSUPPORTED_SHELL = "unsupported_shell"  # the pane's shell is not a POSIX shell (D6)


def validate_session_name(session: object) -> str:
    """Return ``session`` if it is a usable non-default session name, else raise ``InvalidConfigError``."""
    if not isinstance(session, str) or not session:
        raise InvalidConfigError("a herdr session name is required")
    if not SESSION_NAME_RE.fullmatch(session):
        raise InvalidConfigError(
            f"invalid herdr session name {session!r}: must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        )
    if session.lower() == "default":
        raise InvalidConfigError("polyagent never operates on herdr's default session")
    return session


def validate_agent_name(name: object) -> str:
    if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
        raise InvalidConfigError(f"invalid agent name {name!r}: must match [a-z][a-z0-9_-]{{0,31}}")
    return name


def validate_discussion_id(value: object) -> str:
    if not isinstance(value, str) or not DISCUSSION_ID_RE.fullmatch(value):
        raise InvalidConfigError(f"invalid discussion id {value!r}: must be 32 lowercase hex characters")
    return value
