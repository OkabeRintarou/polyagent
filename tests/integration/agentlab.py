"""Safety lab for the real-agent integration tests (design D17, task 8.1).

What it guarantees for every test that uses it:

* a random ``polyagent-test-*`` herdr session whose server the lab starts itself, behind the guarding
  shim, with ``CODEX_HOME``, ``CLAUDE_CONFIG_DIR`` and ``COPILOT_HOME`` pointing at 0700 directories
  under one ``mktemp`` root;
* login state copied field by field from the user's configuration (copilot: login keys; codex: the
  model provider; claude: the repository-root ``claude`` settings file, which is git-excluded and read
  at run time only);
* content digests of the user's agent configuration, installs and entry links are compared before
  and after: any difference fails the test;
* ``TMPDIR`` and ``XDG_CACHE_HOME`` of the server (and so of every pane and agent) point under the
  root too (claude writes logs, sockets and caches there; herdr itself ignores both), and so
  does ``HISTFILE``: a pane's interactive shell writes the command lines herdr typed to it when it
  exits, and the library unsets the history file only in panes it prepares (D6, D17);
* teardown, also after a failure: close the agents, stop and delete the session, kill processes that
  inherited the lab's nonce (detached helpers), delete the root;
* a check that does not depend on any list of known paths: after teardown, no entry in the user's
  configuration directories, ``~/.cache``, the system temporary directory or the regular files at the
  top of the home directory may carry the root's name, and no file there changed during the lab may
  contain it. Only the current user's entries count. Limits: ``LEAK_DEPTH`` directory levels per area,
  files up to ``LEAK_MAX_BYTES``. In addition the library's preparation sentinel must not appear more
  often in the user's shell history files than before;
* a pre-flight check per pytest session: leftovers of a run that was killed (processes with a lab
  nonce, ``pa-it-*`` roots, ``polyagent-test-*`` sessions) stop the run with instructions.

Nothing here prints credential values; only names.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import re
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from polyagent import kinds as K
from polyagent import locks
from polyagent import _launch
from polyagent.herdr import HerdrClient
from polyagent.manager import PolyAgent
from polyagent.spec import AgentSpec, ClaudeProvider

from .shim import build_shim, is_scoped, read_records

REPO = Path(__file__).resolve().parents[2]
CLAUDE_FILE = REPO / "claude"  # local, git-excluded claude settings fragment (env: endpoint, token, models)
HOME = Path.home()
COPILOT_MODEL = "claude-haiku-4.5"  # cheapest model of the copilot subscription used in tasks 3.x / 13.1

# User files whose content must not change (approvals, trust, settings, auth, hooks, MCP, rc files).
STRICT = [
    ".codex/config.toml", ".codex/rules", ".codex/hooks.json", ".codex/AGENTS.md",
    ".copilot/config.json", ".copilot/settings.json", ".copilot/permissions-config.json",
    ".copilot/mcp-config.json", ".copilot/lsp-config.json", ".copilot/hooks", ".copilot/agents", ".copilot/skills",
    ".claude/settings.json", ".claude/settings.local.json", ".claude/claude.json", ".claude/CLAUDE.md",
    ".claude/agents", ".claude/commands",
    ".bashrc", ".profile", ".bash_profile", ".zshrc",
]
# ~/.claude.json is rewritten by every claude run of the user: only the keys that grant something.
JSON_KEYS = {".claude.json": ("projects", "customApiKeyResponses", "mcpServers", "bypassPermissionsModeAccepted")}
# Installs and entry links.
INSTALL_LINKS = [".local/bin/codex", ".local/bin/claude", ".local/bin/copilot", ".codex/packages/standalone/current"]
INSTALL_DIRS = [".local/share/claude/versions"]
# Every pane: claude must not update the user's install (it ignores the isolated autoUpdates=false).
NO_SELF_UPDATE = {"DISABLE_AUTOUPDATER": "1"}
# Where an escape is looked for by name and content after every lab (relative to the home directory),
# plus the system temporary directory.
LEAK_AREAS = [".codex", ".copilot", ".claude", ".config", ".cache", ".local/state", ".local/share"]
LEAK_DEPTH = 5
LEAK_MAX_BYTES = 8 << 20
# The library's pane preparation command line ends with this sentinel (``_launch.prep_command``): its count
# in the user's shell history must not grow during a lab (D17, phase-5 final review).
PREP_SENTINEL = b"__pa_env_ok_$((1+1))"
HISTORY_FILES = (".bash_history", ".sh_history", ".zsh_history", ".histfile", ".local/share/fish/fish_history")
# Variables the lab's server (and so every pane and agent) inherits from the caller; nothing else
# (review C7): no provider or CLI variables of the shell pytest runs in.
SERVER_ENV_KEEP = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "COLORTERM", "TZ",
                   "XDG_RUNTIME_DIR", "HERDR_CONFIG_PATH", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
                   "REQUESTS_CA_BUNDLE", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "NO_PROXY", "all_proxy", "ALL_PROXY"}


# ---- digests -----------------------------------------------------------------------------------


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _tree(path: Path) -> dict[str, Any]:
    """Content digest of every file under ``path``; symbolic links are recorded as links (never followed)."""
    out: dict[str, Any] = {}
    for dirpath, dirs, files in os.walk(path):
        for n in dirs + files:
            p = Path(dirpath) / n
            if p.is_symlink():
                out[str(p)] = ("link", os.readlink(p))
            elif n in files:
                with contextlib.suppress(OSError):
                    out[str(p)] = _sha(p)
    return out


def _stat_tree(path: Path) -> list[tuple[str, int, int]]:
    """Cheap manifest of an install tree: (relative path, size, mtime) of every entry."""
    out = []
    for dirpath, dirs, files in os.walk(path):
        for n in dirs + files:
            p = Path(dirpath) / n
            with contextlib.suppress(OSError):
                st = os.lstat(p)
                # Directory times change with any restore below them; files keep size and time.
                out.append((os.path.relpath(p, path), 0, 0) if n in dirs else
                           (os.path.relpath(p, path), st.st_size, st.st_mtime_ns))
    return sorted(out)


def user_config_digest(home: Path = HOME) -> dict[str, Any]:
    """Digest of the user's agent configuration, installs and entry links (never their content)."""
    out: dict[str, Any] = {}
    for rel in STRICT:
        p = home / rel
        if p.is_dir():
            out.update({os.path.relpath(k, home): v for k, v in _tree(p).items()})
        elif p.is_file():
            out[rel] = _sha(p)
        else:
            out[rel] = None
    for rel, keys in JSON_KEYS.items():
        p = home / rel
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            data = {}
        for k in keys:
            out[f"{rel}#{k}"] = hashlib.sha256(json.dumps(data.get(k), sort_keys=True).encode()).hexdigest()
    for rel in INSTALL_LINKS:  # entry link and the content of what it resolves to
        p = home / rel
        try:
            target = os.path.realpath(p)
            content = _sha(Path(target)) if os.path.isfile(target) else _stat_tree(Path(target))
            out[rel] = (os.readlink(p) if p.is_symlink() else None, target, content)
        except OSError:
            out[rel] = None
    for rel in INSTALL_DIRS:  # installed versions: names, sizes and times of every file
        p = home / rel
        out[rel] = _stat_tree(p) if p.is_dir() else None
    return out


def digest_diff(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def _norm(text: str) -> str:
    """Names derived from a path replace its punctuation (claude: `/tmp/pa-it-x_y` -> `-tmp-pa-it-x-y`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", text)


def find_escapes(root: Path, since: float, home: Path = HOME, tmp: Path | None = None,
                 uid: int | None = None) -> list[str]:
    """Entries outside ``root`` that belong to it: a name containing the root's name (also with its
    punctuation replaced), or a file changed since ``since`` whose content contains the root path or
    name. Searched in the user's configuration directories, ``~/.cache``, the temporary directory
    (``LEAK_DEPTH`` levels each) and the regular files at the top of the home directory. Only entries
    of ``uid`` (default: the current user) count."""
    uid = os.getuid() if uid is None else uid
    token = _norm(root.name)
    needles = {str(root).encode(), root.name.encode()}
    found = []

    def check(p: Path, is_file: bool) -> None:
        try:
            st = os.lstat(p)
        except OSError:
            return
        if st.st_uid != uid:
            return
        if token in _norm(p.name):
            found.append(str(p))
            return
        if not is_file or st.st_mtime < since or st.st_size > LEAK_MAX_BYTES or not stat.S_ISREG(st.st_mode):
            return
        try:
            data = p.read_bytes()
        except OSError:
            return
        if any(x in data for x in needles):
            found.append(str(p))

    for area in [home / a for a in LEAK_AREAS] + [tmp or Path(tempfile.gettempdir())]:
        if not area.is_dir():
            continue
        base = len(area.parts)
        for dirpath, dirs, files in os.walk(area):
            here = Path(dirpath)
            if here == root or root in here.parents:
                dirs[:] = []
                continue
            if len(here.parts) - base >= LEAK_DEPTH:
                dirs[:] = []
            for n in dirs:
                if here / n != root:
                    check(here / n, False)
            for n in files:
                check(here / n, True)
    with contextlib.suppress(OSError):
        for entry in os.scandir(home):  # the home directory's own top-level regular files
            if entry.is_file(follow_symlinks=False):
                check(Path(entry.path), True)
    return sorted(set(found))


def history_sentinels(home: Path = HOME) -> dict[str, int]:
    """How often the library's preparation sentinel occurs in each of the user's shell history files."""
    paths = {home / rel for rel in HISTORY_FILES}
    if os.environ.get("HISTFILE"):
        paths.add(Path(os.environ["HISTFILE"]))
    out = {}
    for p in sorted(paths):
        with contextlib.suppress(OSError):
            out[str(p)] = p.read_bytes().count(PREP_SENTINEL)
    return out


def history_growth(before: dict[str, int], after: dict[str, int]) -> list[str]:
    return [f"{p} ({before.get(p, 0)} -> {n})" for p, n in after.items() if n > before.get(p, 0)]


def _uid_of(p: Path) -> int | None:
    try:
        return os.stat(p).st_uid
    except OSError:
        return None


def preflight(real_herdr: str, tmp: Path | None = None) -> list[str]:
    """Leftovers of an earlier run that was killed before its teardown (review C3). Nothing is removed
    here: they may hold copied credentials. Only the current user's processes and roots count (another
    user's lab is not ours to report)."""
    problems = []
    lab_procs = []
    uid = os.getuid()
    for d in Path("/proc").iterdir():
        if (d.name.isdigit() and int(d.name) != os.getpid() and _uid_of(d) == uid
                and f"{NONCE_VAR}=".encode() in _environ(int(d.name))):
            lab_procs.append(d.name)
    if lab_procs:
        problems.append(f"processes started by an earlier lab are still running (pids {', '.join(lab_procs)}): "
                        f"check `tr '\\0' '\\n' < /proc/<pid>/environ | grep {NONCE_VAR}` and stop them")
    roots = sorted(str(p) for p in (tmp or Path(tempfile.gettempdir())).glob("pa-it-*") if _uid_of(p) == uid)
    if roots:
        problems.append(f"lab roots of an earlier run are left (they hold copied credentials): {roots}; "
                        "stop their processes first, then delete them")
    sessions = sorted(n for n in session_names(real_herdr) if n.startswith("polyagent-test-"))
    if sessions:
        problems.append(f"herdr sessions of an earlier run are left: {sessions}; "
                        "`herdr session stop <name>` then `herdr session delete <name>`")
    return problems


# ---- login state copied into isolated directories ----------------------------------------------------


def _private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def claude_env() -> dict[str, str] | None:
    """The ``env`` block of the repository-root ``claude`` file, or ``None`` when there is none."""
    try:
        text = CLAUDE_FILE.read_text().strip()
    except OSError:
        return None
    if not text.startswith("{"):
        text = "{" + text.rstrip().rstrip(",") + "}"
    env = json.loads(text).get("env") or {}
    return {k: str(v) for k, v in env.items()} if env.get("ANTHROPIC_BASE_URL") else None


def claude_secrets() -> list[str]:
    """Values that must never show up in output, files or argv (token, endpoint and its host)."""
    env = claude_env() or {}
    out = []
    for k, v in env.items():
        if "TOKEN" in k or "KEY" in k or k == "ANTHROPIC_BASE_URL":
            out.append(v)
        if k == "ANTHROPIC_BASE_URL" and "://" in v:
            out.append(v.split("/")[2])
    return [v for v in out if len(v) >= 6]


def claude_provider(**extra: Any) -> ClaudeProvider:
    env = claude_env()
    if env is None:
        pytest.skip("claude: no local `claude` settings file with an endpoint at the repository root")
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    key = env.get("ANTHROPIC_API_KEY")
    auth = {"auth_token": token} if token else {"api_key": key}
    return ClaudeProvider.compatible(env["ANTHROPIC_BASE_URL"], model=env.get("ANTHROPIC_MODEL"),
                                     small_fast_model=env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"), **auth, **extra)


def write_copilot_home(d: Path) -> None:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = (HOME / ".copilot/config.json").read_text()
    src = json.loads("\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")))
    keep = {k: src[k] for k in ("firstLaunchAt", "loggedInUsers", "lastLoggedInUser", "authTokens") if k in src}
    _private(d / "config.json", json.dumps(keep))
    # without it every test session is published to the account's GitHub agents list
    _private(d / "settings.json", json.dumps({"remoteSessions": False, "remoteExport": False}))


def _toml(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    raise TypeError(type(v))


_TOML_TO_JSON = "import json,sys,tomllib; sys.stdout.write(json.dumps(tomllib.load(open(sys.argv[1],'rb'))))"


def _load_toml(path: Path) -> dict[str, Any]:
    """``tomllib`` (3.11+); on 3.10 a newer interpreter on PATH converts the file to JSON through a
    pipe (nothing is printed or written)."""
    try:
        import tomllib
    except ModuleNotFoundError:
        pass
    else:
        return tomllib.loads(path.read_text())
    for exe in ("python3.13", "python3.12", "python3.11", "python3"):
        found = shutil.which(exe)
        if not found:
            continue
        out = subprocess.run([found, "-I", "-c", _TOML_TO_JSON, str(path)], capture_output=True, text=True, timeout=30,
                             stdin=subprocess.DEVNULL)
        if out.returncode == 0:
            return json.loads(out.stdout)
    pytest.skip("reading the codex config needs tomllib (Python 3.11+, here or on PATH)")


def write_codex_home(d: Path) -> None:
    """Only the model provider (which may carry its own key header) and the model."""
    src = _load_toml(HOME / ".codex/config.toml")
    name = src["model_provider"]
    prov = src["model_providers"][name]
    lines = [f"model_provider = {_toml(name)}", f"model = {_toml(src['model'])}", "[tui]",
             "screen_reader_detection_done = true", f"[model_providers.{name}]"]
    tables = {}
    for k, v in prov.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            lines.append(f"{k} = {_toml(v)}")
    for k, t in tables.items():
        lines.append(f"[model_providers.{name}.{json.dumps(k)}]")
        lines += [f"{json.dumps(kk)} = {_toml(vv)}" for kk, vv in t.items()]
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(d / "config.toml", "\n".join(lines) + "\n")


def write_claude_home(d: Path) -> None:
    """No credentials: claude gets them from an explicit provider. Onboarding flags only."""
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        g = json.loads((HOME / ".claude.json").read_text())
    except (OSError, ValueError):
        g = {}
    keep = {k: g[k] for k in ("hasCompletedOnboarding", "lastOnboardingVersion", "installMethod", "theme") if k in g}
    keep.setdefault("hasCompletedOnboarding", True)
    keep["autoUpdates"] = False
    _private(d / ".claude.json", json.dumps(keep))
    _private(d / "settings.json", json.dumps({}))


LOGIN_SOURCES = {
    "copilot": [".copilot/config.json"],
    "codex": [".codex/config.toml"],
    "claude": [],
}


def require_kind(kind: str, policy: str | None = None, *, login: bool = True) -> None:
    """Skip (with the reason in the output) when the kind cannot run here. ``login=False``: the test
    brings its own backend (claude against the local stub) and needs no login state."""
    a = K.adapter(kind)
    if shutil.which(a.executable) is None:
        pytest.skip(f"{kind}: `{a.executable}` is not installed")
    missing = [p for p in LOGIN_SOURCES[kind] if not (HOME / p).exists()]
    if login and missing:
        pytest.skip(f"{kind}: no login state to copy ({', '.join('~/' + m for m in missing)})")
    if login and kind == "claude" and claude_env() is None:
        pytest.skip("claude: no local `claude` settings file with an endpoint at the repository root")
    if policy == "read_only":
        cap = a.capability(K.READ_ONLY)
        if cap.state != K.VERIFIED:
            pytest.skip(f"{kind}: read_only is {cap.state}" + (f" (task {cap.task})" if cap.task else ""))


# ---- processes ------------------------------------------------------------------------------------


NONCE_VAR = "POLYAGENT_LAB_NONCE"


def _environ(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return b""


def owned(nonce: str) -> list[tuple[int, str]]:
    """Processes that inherited this lab's nonce from the lab's server (pane shells, agents, helpers
    they detached). Nothing else carries it: an editor or `tail` looking at lab files does not."""
    mark = f"{NONCE_VAR}={nonce}".encode()
    found = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == os.getpid():
            continue
        if mark in _environ(int(d.name)).split(b"\0"):
            try:
                cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ")[:80].decode(errors="replace")
            except OSError:
                continue
            found.append((int(d.name), cmd))
    return found


def reap(nonce: str) -> list[str]:
    """Kill the processes ``owned`` by the lab; ownership is re-checked right before each signal."""
    mark = f"{NONCE_VAR}={nonce}".encode()
    killed: list[str] = []
    for sig in (signal.SIGTERM, signal.SIGTERM, signal.SIGKILL):
        left = owned(nonce)
        if not left:
            break
        for pid, cmd in left:
            if mark in _environ(pid).split(b"\0"):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
                    killed.append(cmd)
        time.sleep(1.5)
    return killed


def server_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in SERVER_ENV_KEEP or k.startswith("LC_")}
    env.update(extra)
    return env


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if (not k.startswith("HERDR_") or k == "HERDR_CONFIG_PATH") and k != "XDG_CONFIG_HOME"}


def session_names(real: str) -> set[str]:
    out = subprocess.run([real, "session", "list", "--json"], capture_output=True, text=True, timeout=30,
                         env=clean_env(), stdin=subprocess.DEVNULL)
    return {s["name"] for s in json.loads(out.stdout)["sessions"]}


# ---- the lab ----------------------------------------------------------------------------------------


@dataclass
class AgentLab:
    """One temporary session with isolated agent homes; see the module docstring."""

    real_herdr: str
    server_env: dict[str, str] = field(default_factory=dict)  # extra variables for the server (8.5)
    tag: str = "lab"
    # Where the escape and shell-history checks look; self-tests use temporary ones (D17, task 8.10).
    home: Path = HOME
    tmpdir: Path | None = None
    name: str = field(init=False)  # always generated here, never given by a caller
    root: Path = field(init=False)
    pa: PolyAgent = field(init=False)
    agents: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _cleanups: list[Callable[[], None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = f"polyagent-test-{secrets.token_hex(4)}"
        self.nonce = secrets.token_hex(16)
        self.owns_session = False

    # -- setup
    def __enter__(self) -> AgentLab:
        self.started_at = time.time() - 1
        self.digest_before = user_config_digest()
        self.history_before = history_sentinels(self.home)
        self.sessions_before = session_names(self.real_herdr)
        if self.name in self.sessions_before:  # never adopt an existing session
            raise RuntimeError(f"session {self.name} already exists")
        self.root = Path(tempfile.mkdtemp(prefix=f"pa-it-{self.tag}-"))
        self.root.chmod(0o700)
        try:
            self._setup()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _setup(self) -> None:
        cfg = self.root / "cfg"
        self.log, self.refused = self.root / "herdr-argv.log", self.root / "herdr-refused.log"
        # The variables name every kind's isolated directory from the start (the server's environment is
        # fixed when it starts); a directory is filled with login state only when a test starts that kind.
        homes: dict[str, Path] = {"CODEX_HOME": cfg / "codex", "CLAUDE_CONFIG_DIR": cfg / "claude",
                                  "COPILOT_HOME": cfg / "copilot"}
        self._writers = {
            "codex": lambda: write_codex_home(cfg / "codex"),
            "claude": lambda: write_claude_home(cfg / "claude"),
            "copilot": lambda: write_copilot_home(cfg / "copilot"),
        }
        self._prepared: set[str] = set()
        cfg.mkdir(mode=0o700)
        for d in ("tmp", "cache"):
            (self.root / d).mkdir(mode=0o700)
        env = server_env(self.server_env)
        env["PATH"] = f"{self.root / 'shim'}{os.pathsep}{env.get('PATH', '')}"
        env.update({k: str(v) for k, v in homes.items()})
        # claude writes logs, sockets and caches there (phase-5 review C1); herdr ignores both.
        env["TMPDIR"] = str(self.root / "tmp")
        env["XDG_CACHE_HOME"] = str(self.root / "cache")
        # Pane shells write the typed command lines here when they exit, not to the user's history (D17).
        self.histfile = self.root / "shell_history"
        env["HISTFILE"] = str(self.histfile)
        env[NONCE_VAR] = self.nonce
        # claude started its self-update from a lab pane (phase 8: a partial version file appeared in the
        # user's install directory) although the isolated configuration says autoUpdates=false.
        env.update(NO_SELF_UPDATE)
        # Any server launch (also a restart by the library) must carry the isolated homes and the nonce.
        self.shim = build_shim(self.root / "shim", self.real_herdr, self.name, self.log, self.refused,
                               server_env={**{k: str(v) for k, v in homes.items()}, NONCE_VAR: self.nonce,
                                           "HISTFILE": str(self.histfile), **NO_SELF_UPDATE})
        self.homes = homes
        self.env = env
        self.owns_session = True
        self._server = subprocess.Popen([str(self.shim), "--session", self.name, "server"], env=env, cwd=str(self.root),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
        # Wait with a plain probe (it never starts a server) before PolyAgent, whose constructor would
        # start one itself from the caller's environment if ours were not up yet (review C4).
        probe = HerdrClient(self.name, str(self.shim))
        deadline = time.monotonic() + 30
        while not probe.is_running():
            if self._server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the lab's herdr server did not start")
            time.sleep(0.1)
        self.pa = PolyAgent(session=self.name, herdr_path=str(self.shim), server_start_timeout=30)

    # -- helpers for tests
    def work(self, name: str = "work", git: bool = False, files: dict[str, str] | None = None) -> Path:
        d = self.root / "w" / name
        d.mkdir(parents=True)
        if git:
            subprocess.run(["git", "init", "-q", str(d)], check=True)
        for rel, text in (files or {}).items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        return d

    def spec(self, kind: str, policy: str | None = None, **kw: Any) -> AgentSpec:
        """A spec with this lab's per-kind defaults: copilot's model, claude's provider."""
        if kind == "copilot":
            kw.setdefault("model", COPILOT_MODEL)
        if kind == "claude" and "provider" not in kw:
            kw["provider"] = claude_provider()
        return AgentSpec(kind=kind, policy=policy, **kw)

    def prepare(self, kind: str) -> None:
        """Fill the kind's isolated directory with the copied login state (once); skip when that is not
        possible here (e.g. codex logged in without a model provider), without affecting other kinds."""
        if kind in self._prepared:
            return
        missing = [p for p in LOGIN_SOURCES[kind] if not (HOME / p).exists()]
        if missing:
            pytest.skip(f"{kind}: no login state to copy ({', '.join('~/' + m for m in missing)})")
        try:
            self._writers[kind]()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
            pytest.skip(f"{kind}: its login state could not be copied ({type(e).__name__})")
        self._prepared.add(kind)

    def start(self, kind: str, cwd: Path, policy: str | None = None, *, name: str | None = None,
              start_timeout: float = 90, **kw: Any):
        require_kind(kind, policy, login=not (kind == "claude" and "provider" in kw))
        shell = os.path.basename(os.environ.get("SHELL", ""))
        if _launch.needs_prep(self.spec(kind, policy, **kw), K.adapter(kind)) and shell not in _launch.POSIX_SHELLS:
            pytest.skip(f"{kind}: the pane shell {shell!r} is refused for the environment script (task 13.17)")
        self.prepare(kind)
        spec = self.spec(kind, policy, **kw)
        agent = self.pa.start(spec, cwd, name=name, start_timeout=start_timeout)
        self.agents.append(agent)
        return agent

    def on_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def calls(self) -> list[list[str]]:
        return read_records(self.log)

    def herdr(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run([self.real_herdr, "--session", self.name, *args], capture_output=True, text=True,
                              timeout=timeout, env=clean_env(), stdin=subprocess.DEVNULL)

    # -- teardown
    def __exit__(self, *exc: object) -> None:
        """Every stage runs whatever the earlier ones did; failures are collected in ``problems``."""
        problems: list[str] = []

        def stage(what: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 - keep tearing down, report afterwards
                problems.append(f"{what} failed: {e!r}")

        for agent in reversed(self.agents):
            stage(f"closing {agent.name}", agent.close)
        if self.owns_session:
            stage("stopping the session", lambda: problems.extend(self._stop_session()))

        def reap_owned() -> None:
            killed = reap(self.nonce)
            if killed:
                self.notes.append(f"killed {len(killed)} detached process(es): {sorted(set(killed))}")
            left = owned(self.nonce)
            if left:
                problems.append(f"processes of the lab survived SIGKILL: {left}")

        stage("reaping", reap_owned)

        for fn in reversed(self._cleanups):
            stage("cleanup", fn)
        def remove_locks() -> None:
            lockdir = locks.user_home() / ".local" / "state" / "polyagent" / self.name
            shutil.rmtree(lockdir, ignore_errors=True)
            with contextlib.suppress(OSError):
                lockdir.parent.rmdir()  # only when empty

        stage("removing the lock directory", remove_locks)
        root = getattr(self, "root", None)
        if root is not None:
            stage("removing the lab root", lambda: shutil.rmtree(root, ignore_errors=True))
            if root.exists():
                problems.append(f"lab root {root} not removed")

            def check_escapes() -> None:
                escaped = find_escapes(root, self.started_at, home=self.home, tmp=self.tmpdir)
                if escaped:
                    problems.append(f"entries outside the lab root carry its name: {escaped}")

            stage("looking for escapes", check_escapes)

        def check_history() -> None:
            grown = history_growth(self.history_before, history_sentinels(self.home))
            if grown:
                problems.append(f"the library's preparation command line was added to the user's shell history: {grown}")

        stage("checking the user's shell history", check_history)

        def check_sessions() -> None:
            after = session_names(self.real_herdr)
            if after != self.sessions_before:
                problems.append(f"session list changed: {sorted(after ^ self.sessions_before)}")

        def check_digest() -> None:
            changed = digest_diff(self.digest_before, user_config_digest())
            if changed:
                problems.append(f"user agent configuration changed: {changed}")

        stage("checking the session list", check_sessions)
        stage("checking the user configuration", check_digest)
        self.problems = problems

    def _stop_session(self) -> list[str]:
        """Stop this lab's own session, make sure its server process is gone, then delete it."""
        problems = []
        with contextlib.suppress(Exception):
            self.herdr("session", "stop", self.name, "--json", timeout=60)
        server = getattr(self, "_server", None)
        if server is not None:
            for sig in (None, signal.SIGTERM, signal.SIGKILL):
                if sig is not None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(server.pid, sig)
                try:
                    server.wait(15)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if server.poll() is None:
                problems.append(f"the lab's herdr server (pid {server.pid}) did not exit")
        for _ in range(3):
            if self.name not in session_names(self.real_herdr):
                break
            self.herdr("session", "delete", self.name, "--json", timeout=60)
            time.sleep(1)
        else:
            problems.append(f"session {self.name} could not be deleted")
        return problems

    def owned(self) -> list[tuple[int, str]]:
        return owned(self.nonce)

    def audit(self) -> list[str]:
        """Calls that escaped the temp session, and calls the shim refused (read before teardown)."""
        bad = [" ".join(c[:4]) for c in self.calls() if not is_scoped(c, self.name)]
        return bad + [" ".join(c[:4]) for c in read_records(self.refused)]
