"""Discussion records, events, enums and redaction (task 10.1; agent-discussion "可序列化的讨论记录")."""

from __future__ import annotations

import json

import pytest

from polyagent import DEFAULT_TEMPLATE, InvalidConfigError
from polyagent.discussion import DiscussionRecord, Event, Member, Message
from polyagent.discussion.records import (
    REDACTED,
    EndReason,
    EventType,
    MessageStatus,
    MessageType,
    Redactor,
    ReplySource,
    Status,
)


def make_record() -> DiscussionRecord:
    rec = DiscussionRecord(
        id="0123456789abcdef0123456789abcdef", requirement="设计一个缓存 — needs QPS?",
        template=DEFAULT_TEMPLATE.to_dict(), started_at="2026-01-01T00:00:00.000+00:00",
        members=[Member("coordinator", "coordinator", "claude", "m-1", "compatible"),
                 Member("critic", "Finds flaws", "copilot", None, None, available=False)],
    )
    rec.messages = [
        Message(1, 0, "coordinator", None, "brief", {"goal": "g", "assumptions": ["QPS 1000"]}, "ok",
                "session_log", "2026-01-01T00:00:00.000+00:00", 1.25),
        Message(2, 1, "coordinator", "critic", "dispatch", {"question": "q"}, "ok", "session_log",
                "2026-01-01T00:00:01.000+00:00", 0.0),
        Message(3, 1, "critic", "coordinator", "reply", "答复 with \"quotes\"", "ok", "screen",
                "2026-01-01T00:00:02.000+00:00", 3),
        Message(4, 1, "critic", "coordinator", "reply", "AgentTimeoutError: late", "timeout", None,
                "2026-01-01T00:00:05.000+00:00", 30.5),
        Message(5, 1, "coordinator", None, "coordinator_output", {"output": "x", "errors": ["bad"]}, "invalid",
                "session_log", "2026-01-01T00:00:40.000+00:00", 2.0),
    ]
    rec.brief = {"goal": "g", "constraints": [], "assumptions": ["QPS 1000"], "acceptance_criteria": []}
    rec.summary = {"title": "t", "nested": {"n": [1, 2.5, True, None]}}
    rec.status = "completed"
    rec.end_reason = "budget_exhausted"
    rec.error = {"type": "X", "message": "y"}
    rec.finished_at = "2026-01-01T00:01:00.000+00:00"
    return rec


def test_record_round_trips_through_json():
    rec = make_record()
    restored = DiscussionRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert restored == rec
    assert restored.to_dict() == rec.to_dict()


def test_record_dict_has_only_json_native_types_and_the_spec_fields():
    d = make_record().to_dict()
    assert json.loads(json.dumps(d)) == d

    def native(v):
        if isinstance(v, dict):
            return all(isinstance(k, str) and native(x) for k, x in v.items())
        if isinstance(v, list):
            return all(native(x) for x in v)
        return v is None or type(v) in (str, int, float, bool)

    assert native(d)
    assert set(d) == {"schema_version", "id", "status", "end_reason", "error", "requirement", "brief", "members",
                      "messages", "summary", "template", "started_at", "finished_at"}
    assert d["schema_version"] == 1
    assert set(d["members"][0]) == {"name", "role", "kind", "model", "provider", "available"}
    assert set(d["messages"][0]) == {"seq", "round", "sender", "receiver", "type", "content", "status",
                                     "reply_source", "started_at", "duration"}
    assert d["template"]["fingerprint"] == DEFAULT_TEMPLATE.fingerprint


def test_new_record_defaults():
    rec = DiscussionRecord(id="a" * 32, requirement="r", template={})
    assert rec.status == "running" and rec.end_reason is None and rec.error is None and rec.summary is None
    assert rec.started_at.endswith("+00:00") and rec.finished_at is None


def test_enum_values_match_the_spec():
    assert {s.value for s in Status} == {"running", "completed", "failed", "cancelled"}
    assert {r.value for r in EndReason} == {"finished", "budget_exhausted", "coordinator_invalid",
                                            "participants_unavailable", "coordinator_failed", "summary_invalid",
                                            "start_failed", "callback_error", "cancelled", "internal_error"}
    assert {t.value for t in MessageType} == {"brief", "dispatch", "reply", "finish", "summary", "coordinator_output"}
    assert {s.value for s in MessageStatus} == {"ok", "invalid", "timeout", "blocked", "aborted", "error", "failed",
                                                "cancelled"}
    assert {s.value for s in ReplySource} == {"session_log", "screen"}
    assert {t.value for t in EventType} == {"discussion_started", "phase_changed", "message", "member_unavailable",
                                            "discussion_finished"}


def test_enum_members_are_stored_as_plain_strings():
    m = Message(1, 0, "a", None, MessageType.REPLY, "x", MessageStatus.OK, ReplySource.SCREEN, "t", 0.0)
    assert type(m.type) is str and type(m.status) is str and type(m.reply_source) is str
    assert json.loads(json.dumps(m.to_dict()))["type"] == "reply"


@pytest.mark.parametrize("field,value", [("type", "opinion"), ("status", "done"), ("reply_source", "file")])
def test_unknown_message_enum_values_are_rejected(field, value):
    d = make_record().messages[0].to_dict()
    d[field] = value
    with pytest.raises(InvalidConfigError):
        Message.from_dict(d)


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("summary"),
    lambda d: d.update(extra=1),
    lambda d: d.update(schema_version=2),
    lambda d: d.update(status="paused"),
    lambda d: d.update(end_reason="timeout"),
    lambda d: d["messages"][0].update(seq="1"),
    lambda d: d["messages"][0].update(duration=float("nan")),
    lambda d: d["members"][0].update(available="yes"),
    lambda d: d.update(error={"type": "X"}),
    lambda d: d.update(summary=["not", "a", "dict"]),
])
def test_from_dict_rejects_malformed_records(mutate):
    d = make_record().to_dict()
    mutate(d)
    with pytest.raises(InvalidConfigError):
        DiscussionRecord.from_dict(d)


def test_event_round_trips():
    ev = Event(3, "message", "a" * 32, "2026-01-01T00:00:00.000+00:00", {"message": make_record().messages[2].to_dict()})
    assert Event.from_dict(json.loads(json.dumps(ev.to_dict()))) == ev
    with pytest.raises(InvalidConfigError):
        Event(1, "tick", "a" * 32, "t", {})
    with pytest.raises(InvalidConfigError):
        Event.from_dict({**ev.to_dict(), "payload": [1]})


# ---- redaction -------------------------------------------------------------------------


def test_redactor_replaces_values_everywhere_including_keys():
    r = Redactor(["tok-secret-123", "short"])
    value = {"a": "x tok-secret-123 y", "tok-secret-123": ["tok-secret-123", 5, None], "n": {"m": "short"}}
    assert r(value) == {"a": f"x {REDACTED} y", REDACTED: [REDACTED, 5, None], "n": {"m": "short"}}


def test_redactor_ignores_values_shorter_than_eight_and_prefers_longer_overlaps():
    r = Redactor(["1234567", "abcdefgh", "abcdefghij"])
    assert r.text("1234567 abcdefghij abcdefgh") == f"1234567 {REDACTED} {REDACTED}"


@pytest.mark.parametrize("secrets", [[], ["tok-secret-123"]])
def test_redactor_always_returns_a_copy(secrets):
    v = {"a": ["b", {"c": "d"}]}
    out = Redactor(secrets)(v)
    assert out == v and out is not v and out["a"] is not v["a"] and out["a"][1] is not v["a"][1]
    out["a"][1]["c"] = "changed"
    assert v["a"][1]["c"] == "d"


@pytest.mark.parametrize("label,mutate", [
    ("schema_version True", lambda d: d.update(schema_version=True)),
    ("schema_version 1.0", lambda d: d.update(schema_version=1.0)),
    ("huge duration", lambda d: d["messages"][0].update(duration=10**400)),
    ("negative duration", lambda d: d["messages"][0].update(duration=-5)),
    ("negative seq", lambda d: d["messages"][0].update(seq=-1)),
    ("negative round", lambda d: d["messages"][0].update(round=-1)),
    ("bool seq", lambda d: d["messages"][0].update(seq=True)),
    ("garbage started_at", lambda d: d.update(started_at="garbage")),
    ("naive started_at", lambda d: d.update(started_at="2026-01-01T00:00:00")),
    ("garbage message time", lambda d: d["messages"][0].update(started_at="yesterday")),
    ("garbage finished_at", lambda d: d.update(finished_at="soon")),
    ("empty template", lambda d: d.update(template={})),
    ("completed without summary", lambda d: d.update(summary=None)),
    ("failed with summary", lambda d: d.update(status="failed")),
    ("status as list", lambda d: d.update(status=["completed"])),
    ("member name int", lambda d: d["members"][0].update(name=1)),
    ("running with an end reason", lambda d: d.update(status="running", summary=None)),
    ("completed with a failure reason", lambda d: d.update(end_reason="start_failed")),
    ("cancelled with another reason", lambda d: d.update(status="cancelled", summary=None)),
    ("failed without an end reason", lambda d: d.update(status="failed", summary=None, end_reason=None)),
])
def test_c10_from_dict_strictness(label, mutate):
    d = json.loads(json.dumps(make_record().to_dict()))
    mutate(d)
    with pytest.raises(InvalidConfigError):
        DiscussionRecord.from_dict(d)


def test_c10_event_time_is_checked():
    ev = Event(1, "message", "a" * 32, "2026-01-01T00:00:00.000+00:00", {})
    with pytest.raises(InvalidConfigError):
        Event.from_dict({**ev.to_dict(), "at": "now"})


def test_c6_to_dict_returns_independent_copies():
    rec = make_record()
    before = json.loads(json.dumps(rec.to_dict()))
    d = rec.to_dict()
    d["template"]["name"] = "x"
    d["brief"]["goal"] = "x"
    d["summary"]["nested"]["n"].append(9)
    d["messages"][0]["content"]["goal"] = "x"
    assert rec.to_dict() == before and rec == make_record()


def test_snapshot_of_a_template_invalid_under_newer_rules_still_restores():
    rec = make_record()
    old = {"name": "old", "version": 1, "description": None, "instructions": "i",
           "schema": {"type": "object", "properties": {"xs": {"type": "array"}}}, "fingerprint": "0" * 64}
    rec.template = old
    assert DiscussionRecord.from_dict(json.loads(json.dumps(rec.to_dict()))).template == old
