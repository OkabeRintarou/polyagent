"""Task 2.3: session name validation and herdr version check (design D2, D1)."""

import pytest

from polyagent import herdr as H
from polyagent.errors import HerdrError, InvalidConfigError
from polyagent.manager import PolyAgent


def test_default_session_targets_polyagent(fake):
    """Scenario: 默认 session."""
    PolyAgent()
    session_argvs = [a for a in fake.argvs if a[1:] != ["--version"]]
    assert session_argvs and all(a[1:3] == ["--session", "polyagent"] for a in session_argvs)


def test_custom_session(fake):
    """Scenario: 自定义 session."""
    pa = PolyAgent(session="svc-a")
    pa.cleanup()
    session_argvs = [a for a in fake.argvs if a[1:] != ["--version"]]
    assert session_argvs and all(a[1:3] == ["--session", "svc-a"] for a in session_argvs)


@pytest.mark.parametrize("name", ["default", "Default", "DEFAULT", "", None])
def test_refuse_default_session(fake, name):
    """Scenario: 拒绝 default session."""
    with pytest.raises(InvalidConfigError):
        PolyAgent(session=name)
    assert fake.calls == []


@pytest.mark.parametrize("name", ["../x", "..", ".", ".hidden", "-x", "a/b", "a b", "x" * 65, "ü"])
def test_illegal_session_name(fake, name):
    """Scenario: 非法 session 名."""
    with pytest.raises(InvalidConfigError):
        PolyAgent(session=name)
    assert fake.calls == []


@pytest.mark.parametrize("name", ["default", "", None, ".."])
def test_client_refuses_bad_session_too(herdr_bin, name):
    with pytest.raises(InvalidConfigError):
        H.HerdrClient(name, herdr_bin)


@pytest.mark.parametrize("flag", [True, None, "false", 0])
def test_listed_session_not_explicitly_non_default_is_refused(fake, flag):
    """Scenario: 名字合法但被标记为 default (a missing or non-boolean flag counts as default)."""
    fake.running = False
    fake.sessions_default = flag
    with pytest.raises(HerdrError) as ei:
        PolyAgent(session="svc-a")
    assert ei.value.code == "default_session_refused"
    rest = [a[3:] for a in fake.argvs if a[1:] != ["--version"]]
    assert rest == [["session", "list", "--json"]]  # no probe, no server spawn


def test_unlisted_session_name_is_accepted(fake):
    fake.running = False
    fake.session_listed = False
    PolyAgent(session="svc-a")
    assert [a for a in fake.argvs if a[3:] == ["server"]]


def test_default_check_runs_after_version_check(fake):
    fake.version = "herdr 0.9.4"
    fake.sessions_default = True
    with pytest.raises(HerdrError) as ei:
        PolyAgent()
    assert ei.value.code == "unsupported_version"


def test_version_too_old(fake):
    """Scenario: 版本过低."""
    fake.version = "herdr 0.9.4"
    with pytest.raises(HerdrError) as ei:
        PolyAgent()
    assert ei.value.code == "unsupported_version"
    assert "0.9.4" in str(ei.value) and "0.9.5" in str(ei.value)
    assert fake.argvs == [[fake.argvs[0][0], "--version"]]


@pytest.mark.parametrize("out", ["herdr dev-build", ""])
def test_unparsable_version(fake, out):
    fake.version = out
    with pytest.raises(HerdrError) as ei:
        PolyAgent()
    assert ei.value.code == "unsupported_version" and "0.9.5" in ei.value.message


@pytest.mark.parametrize("out", ["herdr 0.9.5", "herdr 0.10.0", "herdr 1.0.0-beta"])
def test_supported_versions(fake, out):
    fake.version = out
    PolyAgent()


def test_herdr_not_installed_entry_object(monkeypatch):
    """Scenario: herdr 未安装 — the safety net makes ``shutil.which('herdr')`` return None."""
    calls = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError))
    from polyagent.errors import HerdrNotFoundError

    with pytest.raises(HerdrNotFoundError):
        PolyAgent()
    assert calls == []
