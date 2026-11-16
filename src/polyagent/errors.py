"""Exception hierarchy of polyagent (design D16).

Every exception raised by the library derives from :class:`PolyAgentError`.
"""

from __future__ import annotations

from typing import Any


class PolyAgentError(Exception):
    """Base class for all polyagent errors.

    Subclasses with extra constructor fields define ``_init_args`` so that instances survive
    pickling (e.g. across ``multiprocessing`` workers) with every field intact.
    """

    def _init_args(self) -> tuple[Any, ...]:
        return self.args

    def __reduce__(self):  # type: ignore[override]
        return (type(self), self._init_args(), dict(self.__dict__))


class InvalidConfigError(PolyAgentError):
    """A parameter or configuration is invalid."""


class InvalidPromptError(InvalidConfigError):
    """A prompt contains control characters, is too long, or cannot be delivered safely."""


class PolicyNotSupportedError(InvalidConfigError):
    """``read_only`` cannot be enforced exactly, or the configuration tries to widen it."""


class HerdrError(PolyAgentError):
    """A herdr CLI call failed. ``code`` is herdr's machine-readable error code."""

    def __init__(self, code: str, message: str, argv: list[str] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.argv = list(argv or [])

    def _init_args(self) -> tuple[Any, ...]:
        return (self.code, self.message, self.argv)


class HerdrUnavailableError(HerdrError):
    """The herdr server is not running, could not be started, or did not respond."""


class HerdrNotFoundError(PolyAgentError):
    """The herdr binary is not installed or not on PATH."""


class AgentNotFoundError(PolyAgentError):
    """The agent does not exist, was not created by polyagent, has exited or was closed."""


class AgentNameConflictError(PolyAgentError):
    """The name is used by a live agent, or an agent with that name is being started."""


class AgentBusyError(PolyAgentError):
    """The ask did not get its turn in time, the previous turn has not ended, or idleness is unknown."""


class AgentBlockedError(PolyAgentError):
    """The agent is waiting at a dialog that was not handled automatically; ``screen`` is the visible screen."""

    def __init__(self, message: str, screen: str, agent: str | None = None):
        super().__init__(message)
        self.screen = screen
        self.agent = agent

    def _init_args(self) -> tuple[Any, ...]:
        return (str(self), self.screen, self.agent)


class AgentTimeoutError(PolyAgentError):
    """The caller's timeout elapsed. ``pending`` (JSON-native) can be passed to ``wait_reply``."""

    def __init__(
        self,
        message: str,
        status: str | None = None,
        diagnostics: str | None = None,
        pending: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.diagnostics = diagnostics
        self.pending = pending

    def _init_args(self) -> tuple[Any, ...]:
        return (str(self), self.status, self.diagnostics, self.pending)


class PromptNotDeliveredError(PolyAgentError):
    """It could not be confirmed that the agent received the prompt; it was not resent."""


class AgentTurnAbortedError(PolyAgentError):
    """The turn was rejected or interrupted and produced no final reply."""


class AgentTurnError(PolyAgentError):
    """The turn ended with an agent-side error (rate limit, quota, auth, context overflow, ...)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def _init_args(self) -> tuple[Any, ...]:
        return (self.message,)


class ReplyNotFoundError(PolyAgentError):
    """The turn ended but its reply could not be separated; ``screen`` holds the screen text."""

    def __init__(self, message: str, screen: str):
        super().__init__(message)
        self.screen = screen

    def _init_args(self) -> tuple[Any, ...]:
        return (str(self), self.screen)


class AskCancelledError(PolyAgentError):
    """The ask was cancelled from another thread."""


__all__ = [
    "PolyAgentError",
    "InvalidConfigError",
    "InvalidPromptError",
    "PolicyNotSupportedError",
    "HerdrError",
    "HerdrUnavailableError",
    "HerdrNotFoundError",
    "AgentNotFoundError",
    "AgentNameConflictError",
    "AgentBusyError",
    "AgentBlockedError",
    "AgentTimeoutError",
    "PromptNotDeliveredError",
    "AgentTurnAbortedError",
    "AgentTurnError",
    "ReplyNotFoundError",
    "AskCancelledError",
]
