"""polyagent: drive locally installed coding-agent CLIs through herdr from Python."""

from .errors import (
    AgentBlockedError,
    AgentBusyError,
    AgentNameConflictError,
    AgentNotFoundError,
    AgentTimeoutError,
    AgentTurnAbortedError,
    AgentTurnError,
    AskCancelledError,
    HerdrError,
    HerdrNotFoundError,
    HerdrUnavailableError,
    InvalidConfigError,
    InvalidPromptError,
    PolicyNotSupportedError,
    PolyAgentError,
    PromptNotDeliveredError,
    ReplyNotFoundError,
)
from .agent import Agent, Reply
from .manager import PolyAgent
from .spec import AgentSpec, ClaudeProvider, ToolPolicy

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "Reply",
    "AgentSpec",
    "ClaudeProvider",
    "ToolPolicy",
    "AgentBlockedError",
    "AgentBusyError",
    "AgentNameConflictError",
    "AgentNotFoundError",
    "AgentTimeoutError",
    "AgentTurnAbortedError",
    "AgentTurnError",
    "AskCancelledError",
    "HerdrError",
    "HerdrNotFoundError",
    "HerdrUnavailableError",
    "InvalidConfigError",
    "InvalidPromptError",
    "PolicyNotSupportedError",
    "PolyAgent",
    "PolyAgentError",
    "PromptNotDeliveredError",
    "ReplyNotFoundError",
]
