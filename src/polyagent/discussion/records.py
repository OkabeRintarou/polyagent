"""Discussion records and events (design D14): JSON-native, round-trippable, redacted."""

from __future__ import annotations

import copy
import enum
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from ..errors import InvalidConfigError

__all__ = [
    "SCHEMA_VERSION", "REDACTED", "MIN_SECRET_LENGTH", "Status", "EndReason", "Phase", "MessageType",
    "MessageStatus", "ReplySource", "EventType", "Member", "Message", "Event", "DiscussionRecord", "Redactor",
    "utc_now",
]

SCHEMA_VERSION = 1
REDACTED = "***"
MIN_SECRET_LENGTH = 8  # shorter values are not replaced (they would mangle ordinary text, D6)


class _Values(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class Status(_Values):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EndReason(_Values):
    FINISHED = "finished"
    BUDGET_EXHAUSTED = "budget_exhausted"
    COORDINATOR_INVALID = "coordinator_invalid"
    PARTICIPANTS_UNAVAILABLE = "participants_unavailable"
    COORDINATOR_FAILED = "coordinator_failed"
    SUMMARY_INVALID = "summary_invalid"
    START_FAILED = "start_failed"
    CALLBACK_ERROR = "callback_error"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


# Which end reasons each terminal status can have (D14 terminal matrix).
END_REASONS = {
    "completed": {"finished", "budget_exhausted", "coordinator_invalid", "participants_unavailable"},
    "failed": {"coordinator_invalid", "summary_invalid", "budget_exhausted", "coordinator_failed", "start_failed",
               "callback_error", "internal_error"},
    "cancelled": {"cancelled"},
}


class Phase(_Values):
    STARTING = "starting"
    BRIEF = "brief"
    DISPATCH = "dispatch"
    SUMMARY = "summary"


class MessageType(_Values):
    BRIEF = "brief"
    DISPATCH = "dispatch"
    REPLY = "reply"
    FINISH = "finish"
    SUMMARY = "summary"
    COORDINATOR_OUTPUT = "coordinator_output"


class MessageStatus(_Values):
    OK = "ok"
    INVALID = "invalid"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    ABORTED = "aborted"
    ERROR = "error"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReplySource(_Values):
    SESSION_LOG = "session_log"
    SCREEN = "screen"


class EventType(_Values):
    DISCUSSION_STARTED = "discussion_started"
    PHASE_CHANGED = "phase_changed"
    MESSAGE = "message"
    MEMBER_UNAVAILABLE = "member_unavailable"
    DISCUSSION_FINISHED = "discussion_finished"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---- validation helpers for from_dict -----------------------------------------------


def _bad(what: str) -> InvalidConfigError:
    return InvalidConfigError(f"malformed discussion record: {what}")


def _keys(data: Any, what: str, required: Iterable[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _bad(f"{what} must be a dict")
    req = set(required)
    missing, extra = sorted(req - set(data)), sorted(set(data) - req)
    if missing or extra:
        raise _bad(f"{what} keys: missing {missing}, unknown {extra}")
    return data


def _enum(cls: type[_Values], value: Any, what: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    try:
        return cls(value).value
    except ValueError:
        raise _bad(f"{what} {value!r} is not one of {[m.value for m in cls]}") from None


def _str(value: Any, what: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _bad(f"{what} must be a string")
    return value


def _int(value: Any, what: str) -> int:
    if type(value) is not int or value < 0:
        raise _bad(f"{what} must be a non-negative integer")
    return value


def _number(value: Any, what: str) -> float:
    try:
        ok = type(value) in (int, float) and math.isfinite(value) and value >= 0
        result = float(value) if ok else 0.0
    except OverflowError:  # an int too large for a float (review C10)
        ok = False
    if not ok:
        raise _bad(f"{what} must be a finite non-negative number")
    return result


def _time(value: Any, what: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = _str(value, what)
    try:
        parsed = datetime.fromisoformat(text)  # type: ignore[arg-type]
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise _bad(f"{what} must be an ISO 8601 time with a UTC offset")
    return text


def _json(value: Any, what: str, depth: int = 0) -> Any:
    if depth > 64:
        raise _bad(f"{what} is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _bad(f"{what} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_json(v, what, depth + 1) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise _bad(f"{what} has a non-string key")
        return {k: _json(v, what, depth + 1) for k, v in value.items()}
    raise _bad(f"{what} contains a {type(value).__name__}, which is not a JSON value")


def _error(value: Any, what: str) -> dict[str, str] | None:
    if value is None:
        return None
    data = _keys(value, what, ("type", "message"))
    return {"type": _str(data["type"], f"{what}.type"), "message": _str(data["message"], f"{what}.message")}


# ---- dataclasses ---------------------------------------------------------------------


@dataclass
class Member:
    name: str
    role: str
    kind: str
    model: str | None = None
    provider: str | None = None  # provider type name only, never endpoints or credentials
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role, "kind": self.kind, "model": self.model,
                "provider": self.provider, "available": self.available}

    @classmethod
    def from_dict(cls, data: Any) -> "Member":
        d = _keys(data, "member", ("name", "role", "kind", "model", "provider", "available"))
        if not isinstance(d["available"], bool):
            raise _bad("member.available must be a bool")
        return cls(_str(d["name"], "member.name"), _str(d["role"], "member.role"), _str(d["kind"], "member.kind"),
                   _str(d["model"], "member.model", True), _str(d["provider"], "member.provider", True),
                   d["available"])


@dataclass
class Message:
    """One entry of the discussion. ``content`` is JSON-native: text for replies, the structured
    action part for coordinator actions, ``{"output", "errors"}`` for invalid coordinator output."""

    seq: int
    round: int
    sender: str
    receiver: str | None
    type: str
    content: Any
    status: str
    reply_source: str | None
    started_at: str
    duration: float

    def __post_init__(self) -> None:
        self.type = _enum(MessageType, self.type, "message.type")  # type: ignore[assignment]
        self.status = _enum(MessageStatus, self.status, "message.status")  # type: ignore[assignment]
        self.reply_source = _enum(ReplySource, self.reply_source, "message.reply_source", True)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "round": self.round, "sender": self.sender, "receiver": self.receiver,
                "type": self.type, "content": copy.deepcopy(self.content), "status": self.status,
                "reply_source": self.reply_source, "started_at": self.started_at, "duration": self.duration}

    @classmethod
    def from_dict(cls, data: Any) -> "Message":
        d = _keys(data, "message", ("seq", "round", "sender", "receiver", "type", "content", "status",
                                    "reply_source", "started_at", "duration"))
        return cls(_int(d["seq"], "message.seq"), _int(d["round"], "message.round"),
                   _str(d["sender"], "message.sender"), _str(d["receiver"], "message.receiver", True),  # type: ignore[arg-type]
                   d["type"], _json(d["content"], "message.content"), d["status"], d["reply_source"],
                   _time(d["started_at"], "message.started_at"), _number(d["duration"], "message.duration"))  # type: ignore[arg-type]


@dataclass
class Event:
    """A callback notification. ``payload`` is the matching record fragment (JSON-native)."""

    seq: int
    type: str
    discussion_id: str
    at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.type = _enum(EventType, self.type, "event.type")  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "discussion_id": self.discussion_id, "at": self.at,
                "payload": copy.deepcopy(self.payload)}

    @classmethod
    def from_dict(cls, data: Any) -> "Event":
        d = _keys(data, "event", ("seq", "type", "discussion_id", "at", "payload"))
        payload = _json(d["payload"], "event.payload")
        if not isinstance(payload, dict):
            raise _bad("event.payload must be a dict")
        return cls(_int(d["seq"], "event.seq"), d["type"], _str(d["discussion_id"], "event.discussion_id"),  # type: ignore[arg-type]
                   _time(d["at"], "event.at"), payload)  # type: ignore[arg-type]


_TEMPLATE_KEYS = {"name", "version", "description", "instructions", "schema", "fingerprint"}
_RECORD_KEYS = ("schema_version", "id", "status", "end_reason", "error", "requirement", "brief", "members",
                "messages", "summary", "template", "started_at", "finished_at")


@dataclass
class DiscussionRecord:
    id: str
    requirement: str
    template: dict[str, Any]
    members: list[Member] = field(default_factory=list)
    status: str = Status.RUNNING.value
    end_reason: str | None = None
    error: dict[str, str] | None = None  # {"type", "message"}
    brief: dict[str, Any] | None = None
    messages: list[Message] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.status = _enum(Status, self.status, "status")  # type: ignore[assignment]
        self.end_reason = _enum(EndReason, self.end_reason, "end_reason", True)

    def member(self, name: str) -> Member:
        return next(m for m in self.members if m.name == name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "status": self.status,
            "end_reason": self.end_reason,
            "error": dict(self.error) if self.error is not None else None,
            "requirement": self.requirement,
            "brief": copy.deepcopy(self.brief),
            "members": [m.to_dict() for m in self.members],
            "messages": [m.to_dict() for m in self.messages],
            "summary": copy.deepcopy(self.summary),
            "template": copy.deepcopy(self.template),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "DiscussionRecord":
        d = _keys(data, "record", _RECORD_KEYS)
        if type(d["schema_version"]) is not int or d["schema_version"] != SCHEMA_VERSION:
            raise _bad(f"unsupported schema_version {d['schema_version']!r} (this library writes {SCHEMA_VERSION})")
        for key in ("brief", "summary"):
            if d[key] is not None and not isinstance(d[key], dict):
                raise _bad(f"{key} must be a dict or null")
        if not isinstance(d["template"], dict) or set(d["template"]) != _TEMPLATE_KEYS:
            raise _bad(f"template must be a template snapshot with the keys {sorted(_TEMPLATE_KEYS)}")
        if (d["status"] == Status.COMPLETED.value) != (d["summary"] is not None):
            raise _bad("a record has a summary exactly when its status is completed")
        if d["status"] == Status.RUNNING.value:
            if d["end_reason"] is not None:
                raise _bad("a running record has no end reason")
        elif isinstance(d["status"], str) and d["end_reason"] not in END_REASONS.get(d["status"], ()):
            raise _bad(f"end reason {d['end_reason']!r} does not go with status {d['status']!r}")
        if not isinstance(d["members"], list) or not isinstance(d["messages"], list):
            raise _bad("members and messages must be lists")
        return cls(
            id=_str(d["id"], "id"),  # type: ignore[arg-type]
            requirement=_str(d["requirement"], "requirement"),  # type: ignore[arg-type]
            template=_json(d["template"], "template"),
            members=[Member.from_dict(m) for m in d["members"]],
            status=d["status"],
            end_reason=d["end_reason"],
            error=_error(d["error"], "error"),
            brief=_json(d["brief"], "brief"),
            messages=[Message.from_dict(m) for m in d["messages"]],
            summary=_json(d["summary"], "summary"),
            started_at=_time(d["started_at"], "started_at"),  # type: ignore[arg-type]
            finished_at=_time(d["finished_at"], "finished_at", True),
        )


class Redactor:
    """Replaces caller-provided credential values (at least ``MIN_SECRET_LENGTH`` characters) with
    ``***`` in every string of a JSON-native value, keys included (D6)."""

    def __init__(self, secrets: Iterable[str]):
        values = {s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LENGTH}
        self._secrets = sorted(values, key=len, reverse=True)  # longest first: overlapping values

    def text(self, value: str) -> str:
        for s in self._secrets:
            if s in value:
                value = value.replace(s, REDACTED)
        return value

    def __call__(self, value: Any) -> Any:
        """A redacted copy of ``value`` (always a copy, with or without secrets: review C6)."""
        return self._walk(value)

    def _walk(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self._walk(v) for v in value]
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self._walk(v) for k, v in value.items()}
        return value


Callback = Callable[[Event], Any]
