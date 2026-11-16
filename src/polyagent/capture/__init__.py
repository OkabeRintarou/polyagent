"""Reply sources (design D12): session-log adapters for codex, copilot and claude, and the screen capture."""

from .base import Judgement, JsonlLog, LogAdapter, Outcome, prompt_digest
from .claude import ClaudeLog
from .codex import CodexLog
from .copilot import CopilotLog
from .screen import ScreenCapture

ADAPTERS = {"codex": CodexLog, "copilot": CopilotLog, "claude": ClaudeLog}

__all__ = ["ADAPTERS", "ClaudeLog", "CodexLog", "CopilotLog", "Judgement", "JsonlLog", "LogAdapter", "Outcome",
           "ScreenCapture", "prompt_digest"]
