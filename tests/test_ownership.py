"""Task 2.6: ownership marker in the workspace label and cleanup modes (design D4)."""

from datetime import datetime, timezone

import pytest

from conftest import err, ok
from polyagent import locks
from polyagent.errors import HerdrError, InvalidConfigError
from polyagent.manager import Marker, PolyAgent

DISC = "0123456789abcdef0123456789abcdef"
SID = "0b6f2c1e-9a7d-4c43-8f5e-2d1b3a4c5d6e"


def ws(wid, label):
    return {"workspace_id": wid, "label": label}


def agent(name, wid):
    return {"name": name, "workspace_id": wid, "pane_id": f"{wid}:p1", "agent_status": "idle"}


def mk(name, created=100, disc=None, kind="codex", policy="read_only", sid=None):
    return Marker(name, kind, policy, created, sid, disc).encode()


# ---- marker encoding -------------------------------------------------------


def test_marker_round_trip():
    m = Marker("abcdefghij-klmnopqrst_uvwxyz0123", "copilot", "read_only", 1794804550, SID, DISC)
    label = m.encode()
    assert label == f"polyagent:1:abcdefghij-klmnopqrst_uvwxyz0123:copilot:ro:{SID}:{DISC}:1794804550"
    assert Marker.parse(label) == m
    bare = Marker("rev", "claude", "full_auto", created=5)
    assert bare.encode() == "polyagent:1:rev:claude:fa:-:-:5"
    assert Marker.parse(bare.encode()) == bare


@pytest.mark.parametrize(
    "label",
    [
        "my renamed ws",
        "polyagent:1:rev",
        "polyagent:2:rev:codex:ro:-:-:5",
        "polyagent:1:rev:codex:ro:-:-:5:extra",
        "polyagent:1:rev:codex:rw:-:-:5",
        "polyagent:1:rev:foo:ro:-:-:5",
        "polyagent:1:Rev:codex:ro:-:-:5",
        "polyagent:1:rev:codex:ro:-:-:-5",
        "polyagent:1:rev:codex:ro:-:-:５",
        "polyagent:1:rev:codex:ro:-:nothex:5",
        "polyagent:1:rev:codex:ro:a b:-:5",
        "Polyagent:1:rev:codex:ro:-:-:5",
        None,
    ],
)
def test_parse_rejects_anything_else(label):
    assert Marker.parse(label) is None


def test_marker_refuses_invalid_fields():
    with pytest.raises(InvalidConfigError):
        Marker("a:b", "codex", "read_only", 1)
    with pytest.raises(InvalidConfigError):
        Marker("rev", "codex", "read_only", 1, session_id="x:y")
    with pytest.raises(InvalidConfigError):
        Marker("rev", "codex", "read_only", 1, session_id="-")  # C15: would decode as "no sid"
    with pytest.raises(TypeError):
        Marker("rev", "codex", "read_only")  # C15: created is mandatory


# ---- cleanup ----------------------------------------------------------------


@pytest.fixture
def pa(fake):
    return PolyAgent()


def closed(fake):
    return [a[5] for a in fake.argvs if a[3:5] == ["workspace", "close"]]


def test_foreign_resources_untouched(pa, fake):
    """Scenario: 非本库资源不受影响 (incl. a workspace whose marker was renamed away)."""
    fake.workspaces = [ws("w1", "manual"), ws("w2", "my renamed ws"), ws("w3", "polyagent:1:manual")]
    fake.agents = [agent("manual", "w1")]
    assert pa.owned_workspaces() == []
    assert pa.cleanup() == []
    assert pa.cleanup(older_than=10**12) == []
    assert pa.cleanup(discussion_id=DISC) == []
    assert closed(fake) == []


def test_crashed_agent_is_reclaimed(pa, fake):
    """Scenario: agent 崩溃后回收."""
    fake.workspaces = [ws("w1", mk("alive")), ws("w2", mk("crashed")), ws("w3", "manual")]
    fake.agents = [agent("alive", "w1"), {"name": None, "workspace_id": "w2"}]
    assert pa.cleanup() == ["crashed"]
    assert closed(fake) == ["w2"]
    assert [w["workspace_id"] for w in fake.workspaces] == ["w1", "w3"]


def test_same_name_alive_elsewhere_counts_as_orphan(pa, fake):
    fake.workspaces = [ws("w1", mk("rev")), ws("w2", "manual")]
    fake.agents = [agent("rev", "w2")]
    assert pa.cleanup() == ["rev"]


def test_reclaim_by_discussion_closes_live_agents(pa, fake):
    """Scenario: 调用方崩溃后按讨论回收."""
    other = "f" * 32
    fake.workspaces = [ws("w1", mk("coordinator-01234567", disc=DISC)), ws("w2", mk("critic-01234567", disc=DISC)),
                       ws("w3", mk("x-ffffffff", disc=other)), ws("w4", mk("solo"))]
    fake.agents = [agent("coordinator-01234567", "w1"), agent("critic-01234567", "w2"), agent("x-ffffffff", "w3")]
    held = locks.acquire_for_start("polyagent", "critic-01234567")  # locks are ignored in this mode
    try:
        assert pa.cleanup(discussion_id=DISC) == ["coordinator-01234567", "critic-01234567"]
    finally:
        held.release()
    assert closed(fake) == ["w1", "w2"]


def test_reclaim_by_creation_time(pa, fake):
    fake.workspaces = [ws("w1", mk("old", created=100)), ws("w2", mk("new", created=200))]
    fake.agents = [agent("old", "w1"), agent("new", "w2")]
    assert pa.cleanup(older_than=150) == ["old"]
    fake.workspaces.append(ws("w3", mk("older", created=50)))
    assert pa.cleanup(older_than=datetime.fromtimestamp(150, tz=timezone.utc)) == ["older"]


def test_cleanup_signature_has_no_orphans_only(pa):
    """A2: cleanup(*, older_than=None, discussion_id=None) — keyword-only, no orphans_only."""
    with pytest.raises(TypeError):
        pa.cleanup(orphans_only=False)
    with pytest.raises(TypeError):
        pa.cleanup(100)


def test_invalid_discussion_id_rejected(pa, fake):
    """C15: a mistyped discussion id must not silently close nothing."""
    with pytest.raises(InvalidConfigError):
        pa.cleanup(discussion_id="not-a-discussion")


def test_agent_being_started_is_not_reclaimed(pa, fake):
    """Scenario: 启动中的 agent 不被回收 (lock held => skipped)."""
    fake.workspaces = [ws("w1", mk("starting"))]
    held = locks.acquire_for_start("polyagent", "starting")
    try:
        assert pa.cleanup() == []
        assert closed(fake) == []
    finally:
        held.release()
    assert pa.cleanup() == ["starting"]


def snapshot_then(fake, mutate):
    """Run ``mutate`` right after cleanup's first ``agent list`` (its snapshot) is answered."""
    state = {"done": False}

    def handler(argv):
        if argv[3:5] == ["agent", "list"] and not state["done"]:
            state["done"] = True
            resp = ok({"type": "agent_list", "agents": list(fake.agents)})
            mutate()
            return resp
        return None

    fake.handler = handler


def test_agent_that_finished_starting_after_snapshot_is_kept(pa, fake):
    """Scenario: 回收期间启动完成 (C2): orphan per snapshot, then the starter finishes and releases
    the lock -> re-check under lock."""
    fake.workspaces = [ws("w1", mk("rev"))]
    starter = locks.acquire_for_start("polyagent", "rev")

    def start_completes():
        fake.agents.append(agent("rev", "w1"))
        starter.release()

    snapshot_then(fake, start_completes)
    assert pa.cleanup() == []
    assert closed(fake) == []


@pytest.mark.parametrize("kwargs", [{}, {"discussion_id": DISC}, {"older_than": 10**12}])
def test_label_changed_after_snapshot_is_not_closed(pa, fake, kwargs):
    """Scenario: 回收期间标记被改写 (C2): the label is re-read before closing in every mode
    (rename / renumbered workspace)."""
    fake.workspaces = [ws("w1", mk("rev", disc=DISC))]

    def rename():
        fake.workspaces[0]["label"] = "my renamed ws"

    snapshot_then(fake, rename)
    assert pa.cleanup(**kwargs) == []
    assert closed(fake) == []


def test_close_failure_does_not_stop_cleanup(pa, fake):
    """C15: one failing close is reported after the others were processed."""
    fake.workspaces = [ws("w1", mk("a")), ws("w2", mk("b")), ws("w3", mk("c"))]
    fake.handler = lambda argv: err("pane_busy", "nope") if argv[3:6] == ["workspace", "close", "w2"] else None
    with pytest.raises(HerdrError) as ei:
        pa.cleanup()
    assert ei.value.code == "pane_busy"
    assert ei.value.closed == ["a", "c"]
    assert [w["workspace_id"] for w in fake.workspaces] == ["w2"]


def test_cleanup_logs_closed_workspaces(pa, fake, caplog):
    fake.workspaces = [ws("w1", mk("gone"))]
    with caplog.at_level("INFO", logger="polyagent"):
        assert pa.cleanup() == ["gone"]
    assert any("w1" in r.getMessage() and "gone" in r.getMessage() for r in caplog.records)


def test_workspace_vanishing_during_cleanup_is_not_an_error(pa, fake):
    fake.workspaces = [ws("w1", mk("gone"))]
    fake.handler = lambda argv: err("workspace_not_found", "workspace w1 not found") if argv[3:5] == ["workspace", "close"] else None
    assert pa.cleanup() == []


def test_owned_workspaces_reports_liveness(pa, fake):
    fake.workspaces = [ws("w1", mk("a", kind="copilot", policy="full_auto", sid=SID)), ws("w2", mk("b"))]
    fake.agents = [agent("a", "w1")]
    owned = {o.workspace_id: o for o in pa.owned_workspaces()}
    assert owned["w1"].live and owned["w1"].marker.session_id == SID and owned["w1"].marker.policy == "full_auto"
    assert not owned["w2"].live


def test_older_than_type_checked(pa):
    with pytest.raises(InvalidConfigError):
        pa.cleanup(older_than="yesterday")


def test_server_lost_during_cleanup_merges_both_passes(pa, fake, caplog):
    """N5: names closed before the server vanished are not lost, and no per-entry warning spam."""
    fake.workspaces = [ws("w1", mk("a")), ws("w2", mk("b")), ws("w3", mk("c"))]
    state = {"stopped": False}

    def handler(argv):
        if argv[3:6] == ["workspace", "close", "w1"] and not state["stopped"]:
            state["stopped"] = True
            resp = fake.default(argv)
            fake.running = False
            return resp
        return None

    fake.handler = handler
    with caplog.at_level("WARNING", logger="polyagent"):
        assert pa.cleanup() == ["a", "b", "c"]
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert len([a for a in fake.argvs if a[3:] == ["server"]]) == 1


def test_second_pass_error_carries_first_pass_names(pa, fake):
    """2.10 / D4: the server vanishes after closing `a`; in the second pass closing `c` fails.
    The error's `closed` holds both passes (`a` from the first, `b` from the second)."""
    fake.workspaces = [ws("w1", mk("a")), ws("w2", mk("b")), ws("w3", mk("c"))]
    state = {"stopped": False}

    def handler(argv):
        if argv[3:6] == ["workspace", "close", "w1"] and not state["stopped"]:
            state["stopped"] = True
            resp = fake.default(argv)
            fake.running = False
            return resp
        if argv[3:6] == ["workspace", "close", "w3"]:
            return err("pane_busy", "nope")
        return None

    fake.handler = handler
    with pytest.raises(HerdrError) as ei:
        pa.cleanup()
    assert ei.value.code == "pane_busy"
    assert ei.value.closed == ["a", "b"]


def test_second_pass_restart_failure_carries_first_pass_names(pa, fake):
    fake.workspaces = [ws("w1", mk("a")), ws("w2", mk("b"))]

    def handler(argv):
        if argv[3:6] == ["workspace", "close", "w1"]:
            resp = fake.default(argv)
            fake.running = False
            fake.server_starts = False
            fake.server_exit = 1
            return resp
        return None

    fake.handler = handler
    pa._server_start_timeout = 0.3
    with pytest.raises(HerdrError) as ei:
        pa.cleanup()
    assert ei.value.closed == ["a"]


def test_cleanup_result_is_deduplicated(pa, fake):
    """A name may own a restored workspace next to a newer one; it is reported once."""
    fake.workspaces = [ws("w1", mk("a")), ws("w2", mk("a", created=101)), ws("w3", mk("b"))]
    assert pa.cleanup() == ["a", "b"]
    assert closed(fake) == ["w1", "w2", "w3"]
