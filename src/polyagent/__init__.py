"""polyagent: drive locally installed coding-agent CLIs (codex, copilot, claude) through herdr
from Python, and let several of them discuss a requirement under a coordinator agent.

Read the README before deploying it. In short:

* an agent started on its own runs ``full_auto`` by default: it can write files, run any command, use
  the web and every configured MCP tool, and read the variables and provider credentials injected into
  it. Choose ``ToolPolicy.READ_ONLY`` for untrusted repositories or untrusted input;
* discussion members always run ``read_only``;
* ``read_only`` relies on the user's agent CLI configuration and shell start-up files not having been
  changed by a ``full_auto`` agent of the same operating-system user: run them as different users.

Entry points: :class:`PolyAgent` (one herdr session), :class:`AgentSpec`, :class:`Agent`,
:class:`Discussion`, :class:`SummaryTemplate`, :class:`DiscussionRecord`.
"""

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
from .template import DEFAULT_TEMPLATE, SummaryTemplate
from .discussion import Discussion, DiscussionRecord, Event, Member, Message, Participant

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "Reply",
    "AgentSpec",
    "ClaudeProvider",
    "ToolPolicy",
    "SummaryTemplate",
    "DEFAULT_TEMPLATE",
    "Discussion",
    "Participant",
    "DiscussionRecord",
    "Member",
    "Message",
    "Event",
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
