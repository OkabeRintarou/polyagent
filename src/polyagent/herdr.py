"""Thin, thread-safe wrapper around the ``herdr`` CLI (design D1, D2).

Every call runs ``herdr --session <S> ...`` as a child process with a bounded wait.
The session is validated on construction and again for every argv, so this module can
never target herdr's default session.
"""

from __future__ import annotations

import json
import re
import os
import shlex
import shutil
import subprocess
import time
from typing import Any, Iterable, Protocol

from ._rules import (  # re-exported: the herdr module is the single access point for error codes
    BAD_RESPONSE,
    DEFAULT_SESSION_REFUSED,
    LAUNCH_FAILED,
    SERVER_LOCK_TIMEOUT,
    SERVER_START_FAILED,
    SERVER_START_TIMEOUT,
    UNKNOWN,
    UNRESPONSIVE,
    UNSUPPORTED_SHELL,
    UNSUPPORTED_VERSION,
    USAGE,
    validate_session_name,
)
from .errors import (
    AskCancelledError,
    HerdrError,
    HerdrNotFoundError,
    HerdrUnavailableError,
    InvalidConfigError,
    InvalidPromptError,
)

# ---- herdr's own error-code literals the library depends on (mapping: design D16) ----
SERVER_NOT_RUNNING = "server_not_running"
PROTOCOL_MISMATCH = "protocol_mismatch"
AGENT_NOT_FOUND = "agent_not_found"
AGENT_NAME_TAKEN = "agent_name_taken"
AGENT_NOT_READY = "agent_not_ready"
AGENT_BLOCKED = "agent_blocked"
AGENT_PROMPT_STALLED = "agent_prompt_stalled"
WORKSPACE_NOT_FOUND = "workspace_not_found"
TIMEOUT = "timeout"

UNAVAILABLE_CODES = frozenset({SERVER_NOT_RUNNING, PROTOCOL_MISMATCH, UNRESPONSIVE, LAUNCH_FAILED})

__all__ = [
    "AGENT_BLOCKED", "AGENT_NAME_TAKEN", "AGENT_NOT_FOUND", "AGENT_NOT_READY", "AGENT_PROMPT_STALLED", "BAD_RESPONSE",
    "DEFAULT_SESSION_REFUSED", "LAUNCH_FAILED", "PROTOCOL_MISMATCH", "SERVER_LOCK_TIMEOUT",
    "SERVER_NOT_RUNNING", "SERVER_START_FAILED", "SERVER_START_TIMEOUT", "TIMEOUT", "UNAVAILABLE_CODES",
    "UNKNOWN", "UNRESPONSIVE", "UNSUPPORTED_SHELL", "UNSUPPORTED_VERSION", "USAGE", "WORKSPACE_NOT_FOUND",
    "HerdrClient", "check_version", "find_herdr", "herdr_env", "parse_version", "validate_session_name",
]

MIN_VERSION = (0, 9, 5)
READ_SOURCES = ("visible", "recent", "recent-unwrapped", "detection")
WAIT_OUTPUT_SOURCES = ("visible", "recent", "recent-unwrapped")

CALL_LIMIT = 30.0  # seconds, ordinary commands
PROBE_LIMIT = 10.0  # seconds, liveness probe
WAIT_SLACK = 10.0  # seconds added to herdr's own --timeout for waiting commands
POLL_INTERVAL = 0.1
KILL_GRACE = 1.0  # seconds to collect a killed child's output before closing its pipes
AGENT_START_TIMEOUT_RANGE_MS = (3000, 300_000)  # (exclusive, inclusive]

_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class Cancel(Protocol):
    def is_set(self) -> bool: ...


# Moves herdr's session directory: one session name must mean one server for all callers (D1, D3).
SESSION_DIR_VARS = frozenset({"XDG_CONFIG_HOME"})


def herdr_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for herdr CLI calls: drop inherited pane context (``HERDR_*``, except
    ``HERDR_CONFIG_PATH``) and ``XDG_CONFIG_HOME``, which would relocate the session directory."""
    src = os.environ if base is None else base
    return {
        k: v
        for k, v in src.items()
        if (not k.startswith("HERDR_") or k == "HERDR_CONFIG_PATH") and k not in SESSION_DIR_VARS
    }


def find_herdr(path: str | None = None) -> str:
    """Absolute path of the herdr binary (``path`` or the one on ``PATH``)."""
    found = path if path else shutil.which("herdr")
    if not found or not os.path.isfile(found) or not os.access(found, os.X_OK):
        raise HerdrNotFoundError(f"herdr binary not found ({path or 'PATH'})")
    return os.path.abspath(found)


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION_PATTERN.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def check_version(binary: str, timeout: float = CALL_LIMIT) -> tuple[int, int, int]:
    """Run ``herdr --version``; raise ``HerdrError(unsupported_version)`` below 0.9.5 or when unparsable."""
    argv = [binary, "--version"]
    proc = _communicate(argv, timeout, None, env=herdr_env())
    detected = (proc.stdout or proc.stderr or "").strip()
    version = parse_version(detected) if proc.returncode == 0 else None
    need = ".".join(map(str, MIN_VERSION))
    if version is None:
        raise HerdrError(UNSUPPORTED_VERSION, f"cannot determine herdr version from {detected[:200]!r}; need >= {need}", argv)
    if version < MIN_VERSION:
        got = ".".join(map(str, version))
        raise HerdrError(UNSUPPORTED_VERSION, f"herdr {got} is too old; need >= {need}", argv)
    return version


class HerdrClient:
    """Run herdr CLI commands against one named, non-default session."""

    def __init__(self, session: str = "polyagent", binary: str | None = None):
        self._session = validate_session_name(session)
        self._binary = find_herdr(binary)

    @property
    def session(self) -> str:
        return self._session

    @property
    def binary(self) -> str:
        return self._binary

    # ---- plumbing -------------------------------------------------------

    def argv(self, args: Iterable[str]) -> list[str]:
        return [self._binary, "--session", validate_session_name(self._session), *args]

    def _run(self, args: Iterable[str], limit: float, cancel: Cancel | None = None) -> subprocess.CompletedProcess[str]:
        argv = self.argv(args)
        proc = _communicate(argv, limit, cancel, env=herdr_env())
        if proc.returncode != 0:
            raise parse_error(proc, argv)
        return proc

    def call(self, *args: str, limit: float = CALL_LIMIT, cancel: Cancel | None = None) -> dict[str, Any]:
        """Run a JSON-returning command and return its ``result`` object (or the whole payload if unwrapped)."""
        proc = self._run(args, limit, cancel)
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise _bad_response(proc.stdout, self.argv(args)) from e
        if not isinstance(payload, dict):
            raise _bad_response(proc.stdout, self.argv(args))
        if "result" not in payload and isinstance(payload.get("error"), dict):
            err = payload["error"]
            raise make_error(str(err.get("code") or UNKNOWN), str(err.get("message") or ""), self.argv(args))
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            raise _bad_response(proc.stdout, self.argv(args))
        return result

    def text(self, *args: str, limit: float = CALL_LIMIT, cancel: Cancel | None = None) -> str:
        """Run a command whose stdout is plain text (``pane read`` / ``agent read``); content is never interpreted."""
        return self._run(args, limit, cancel).stdout

    def _get(self, result: dict[str, Any], key: str, kind: type, args: Iterable[str]) -> Any:
        value = result.get(key)
        if not isinstance(value, kind):
            raise _bad_response(json.dumps(result), self.argv(args), f"missing or malformed {key!r}")
        return value

    # ---- server / session ------------------------------------------------

    def is_running(self, limit: float = PROBE_LIMIT) -> bool:
        try:
            self.call("workspace", "list", limit=limit)
            return True
        except HerdrError as e:
            if e.code == SERVER_NOT_RUNNING:
                return False
            raise

    def session_list(self, limit: float = CALL_LIMIT) -> list[dict[str, Any]]:
        args = ("session", "list", "--json")
        return _dicts(self._get(self.call(*args, limit=limit), "sessions", list, args), self.argv(args))

    def session_stop(self) -> dict[str, Any]:
        return self.call("session", "stop", self._session, "--json")

    # ---- workspaces / panes --------------------------------------------

    def workspace_create(self, cwd: str, label: str) -> dict[str, Any]:
        return self.call("workspace", "create", "--cwd", cwd, "--no-focus", "--label", label)

    def workspace_get(self, workspace_id: str) -> dict[str, Any]:
        args = ("workspace", "get", workspace_id)
        return self._get(self.call(*args), "workspace", dict, args)

    def workspace_close(self, workspace_id: str) -> None:
        self._run(("workspace", "close", workspace_id), CALL_LIMIT)

    def workspace_list(self) -> list[dict[str, Any]]:
        args = ("workspace", "list")
        return _dicts(self._get(self.call(*args), "workspaces", list, args), self.argv(args))

    def pane_run(self, pane_id: str, command: str) -> None:
        self._run(("pane", "run", pane_id, command), CALL_LIMIT)

    def pane_process_info(self, pane_id: str) -> dict[str, Any]:
        return self.call("pane", "process-info", "--pane", pane_id)

    def pane_wait_output(
        self,
        pane_id: str,
        timeout_ms: int,
        match: str | None = None,
        regex: str | None = None,
        source: str | None = None,
        cancel: Cancel | None = None,
    ) -> dict[str, Any]:
        if (match is None) == (regex is None):
            raise ValueError("exactly one of match/regex is required")
        args = ["pane", "wait-output", pane_id]
        args += ["--match", match] if match is not None else ["--regex", regex]  # type: ignore[list-item]
        if source is not None:
            if source not in WAIT_OUTPUT_SOURCES:
                raise ValueError(f"source must be one of {WAIT_OUTPUT_SOURCES}")
            args += ["--source", source]
        args += ["--timeout", str(_positive_ms(timeout_ms))]
        return self.call(*args, limit=_wait_limit(timeout_ms), cancel=cancel)

    def pane_read(self, pane_id: str, source: str = "recent-unwrapped", lines: int | None = None) -> str:
        return self.text(*_read_args(["pane", "read", pane_id], source, lines))

    # ---- agents ---------------------------------------------------------

    def agent_list(self) -> list[dict[str, Any]]:
        args = ("agent", "list")
        return _dicts(self._get(self.call(*args), "agents", list, args), self.argv(args))

    def agent_get(self, target: str) -> dict[str, Any]:
        args = ("agent", "get", target)
        return self._get(self.call(*args), "agent", dict, args)

    def agent_explain(self, target: str) -> dict[str, Any]:
        return self.call("agent", "explain", target)

    def agent_start(
        self,
        name: str,
        kind: str,
        pane_id: str,
        timeout_ms: int,
        args: list[str] | None = None,
        cancel: Cancel | None = None,
    ) -> dict[str, Any]:
        lo, hi = AGENT_START_TIMEOUT_RANGE_MS
        if not (lo < timeout_ms <= hi):
            raise InvalidConfigError(f"agent start timeout must be in ({lo}, {hi}] ms")
        argv = ["agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", str(timeout_ms)]
        if args:
            argv += ["--", *args]
        return self._get(self.call(*argv, limit=_wait_limit(timeout_ms), cancel=cancel), "agent", dict, argv)

    def agent_prompt(
        self, target: str, text: str, timeout_ms: int | None = None, wait: bool = True, cancel: Cancel | None = None
    ) -> dict[str, Any]:
        """Submit ``text``. With ``wait`` (default) herdr waits up to ``timeout_ms`` (required);
        without it nothing is waited for and ``timeout_ms`` must be omitted."""
        # `agent prompt` has no `--` terminator; callers must apply the D10 guard prefix first.
        if text.startswith("-"):
            raise InvalidPromptError("prompt text reaching herdr must not start with '-'")
        argv = ["agent", "prompt", target, text]
        if not wait:
            if timeout_ms is not None:
                raise ValueError("timeout_ms is only used with wait=True")
            # herdr rejects `--timeout` without `--wait` (usage error, task 13.1); an ordinary call bound applies.
            return self._get(self.call(*argv, limit=CALL_LIMIT, cancel=cancel), "agent", dict, argv)
        if timeout_ms is None:
            raise ValueError("timeout_ms is required with wait=True")
        argv += ["--wait", "--timeout", str(_positive_ms(timeout_ms))]
        return self._get(self.call(*argv, limit=_wait_limit(timeout_ms), cancel=cancel), "agent", dict, argv)

    def agent_wait(
        self, target: str, timeout_ms: int, until: Iterable[str] = (), cancel: Cancel | None = None
    ) -> dict[str, Any]:
        argv = ["agent", "wait", target]
        for state in until:
            argv += ["--until", state]
        argv += ["--timeout", str(_positive_ms(timeout_ms))]
        return self._get(self.call(*argv, limit=_wait_limit(timeout_ms), cancel=cancel), "agent", dict, argv)

    def agent_read(self, target: str, source: str = "recent-unwrapped", lines: int | None = None) -> str:
        return self.text(*_read_args(["agent", "read", target], source, lines))

    def agent_send_keys(self, target: str, *keys: str) -> None:
        self._run(("agent", "send-keys", target, *keys), CALL_LIMIT)


# ---- helpers ----------------------------------------------------------------


def _positive_ms(ms: int) -> int:
    if not isinstance(ms, int) or isinstance(ms, bool) or ms <= 0:
        raise ValueError("timeout_ms must be a positive int")
    return ms


def _wait_limit(timeout_ms: int) -> float:
    return _positive_ms(timeout_ms) / 1000 + WAIT_SLACK


def _read_args(base: list[str], source: str, lines: int | None) -> list[str]:
    if source not in READ_SOURCES:
        raise ValueError(f"source must be one of {READ_SOURCES}")
    args = base + ["--source", source, "--format", "text"]
    if lines is not None:
        args += ["--lines", str(lines)]
    return args


def _dicts(items: list[Any], argv: list[str]) -> list[dict[str, Any]]:
    if not all(isinstance(i, dict) for i in items):
        raise _bad_response(json.dumps(items), argv, "list items must be objects")
    return items


def _bad_response(raw: str, argv: list[str], why: str = "") -> HerdrError:
    detail = f"{why}: " if why else ""
    text = _redact_text(detail + (raw or "").strip(), argv)  # redact first, then truncate (D1)
    return HerdrError(BAD_RESPONSE, text[:MAX_RAW_IN_ERROR], redact_argv(argv))


# Text-bearing positional arguments (index into the args after `--session S`) that must not be
# kept on exception objects: the prompt of `agent prompt` and the command of `pane run`.
_SENSITIVE_ARGS = {("agent", "prompt"): 3, ("pane", "run"): 3}
MIN_REDACT_IN_TEXT = 8  # shorter values are not searched for inside messages (they would mangle them)
ECHO_FRAGMENT = 16  # opening characters of the prompt/command that must not survive in an echo-risk message
MAX_MESSAGE = 1000  # error messages are truncated to this length *after* redaction
MAX_RAW_IN_ERROR = 500
_WITHHELD = "herdr rejected the arguments (message withheld: it may echo the prompt or command)"


def _sensitive_index(argv: list[str]) -> int | None:
    """Absolute index in ``argv`` of the prompt/command argument, if any."""
    base = 3 if argv[1:2] == ["--session"] else 1
    args = argv[base:]
    idx = _SENSITIVE_ARGS.get(tuple(args[:2]))  # type: ignore[arg-type]
    return base + idx if idx is not None and len(args) > idx else None


def _placeholder(value: str) -> str:
    return f"<{len(value)} chars redacted>"


def redact_argv(argv: list[str]) -> list[str]:
    out = list(argv)
    i = _sensitive_index(out)
    if i is not None:
        out[i] = _placeholder(out[i])
    return out


def _echo_forms(value: str) -> set[str]:
    """Opening fragment of ``value`` as herdr or a shell might print it back: raw, or JSON-,
    repr-, shell- or backslash-escaped. Escape the whole value first, then cut, so that the
    fragment matches the start of the escaped echo whatever quoting style was chosen."""
    escaped = [
        value,
        json.dumps(value)[1:],
        repr(value)[1:],
        shlex.quote(value).lstrip("'"),
        value.encode("unicode_escape").decode("ascii"),
    ]
    return {e[:ECHO_FRAGMENT] for e in escaped if e[:ECHO_FRAGMENT]}


def _redact_text(text: str, argv: list[str], echo_risk: bool = True) -> str:
    """Hide the prompt/command inside ``text`` (D1). Whole occurrences of long values are replaced;
    the message is then withheld entirely if it still contains the value's opening fragment in any
    escaped form, which catches truncated, wrapped and escaped echoes under any error code (review C7).
    Values shorter than ``MIN_REDACT_IN_TEXT`` are only looked for in messages that may echo arguments
    (``echo_risk``: usage/unknown errors, raw output), since a short word also occurs by chance (an
    agent named like the prompt). Callers truncate only afterwards."""
    i = _sensitive_index(argv)
    if i is None or not argv[i]:
        return text
    value = argv[i]
    if len(value) >= MIN_REDACT_IN_TEXT:
        text = text.replace(value, _placeholder(value))
    stripped = value.strip()
    if stripped and (echo_risk or len(stripped) >= MIN_REDACT_IN_TEXT):
        if any(f in text for f in _echo_forms(stripped)):
            return _WITHHELD
    return text


def _kill(proc: subprocess.Popen[str]) -> None:
    """Kill and reap the child; close its pipes even if a grandchild still holds them open."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=KILL_GRACE)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass


def launch_error(e: OSError, argv: list[str]) -> Exception:
    binary = argv[0]
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        return HerdrNotFoundError(f"herdr binary {binary} cannot be executed: {e}")
    return HerdrUnavailableError(LAUNCH_FAILED, f"could not run herdr: {e}", redact_argv(argv))


def _communicate(
    argv: list[str], limit: float, cancel: Cancel | None, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with an upper bound on wall time; terminate it on overrun, cancellation or any exception."""
    if cancel is not None and cancel.is_set():
        raise AskCancelledError("cancelled before calling herdr")
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as e:
        raise launch_error(e, argv) from e
    finished = False
    try:
        deadline = time.monotonic() + limit
        while True:
            if cancel is not None and cancel.is_set():
                raise AskCancelledError("herdr wait cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrUnavailableError(
                    UNRESPONSIVE, f"herdr CLI did not return within {limit:g}s", redact_argv(argv)
                )
            step = remaining if cancel is None else min(remaining, POLL_INTERVAL)
            try:
                out, err = proc.communicate(timeout=step)
            except subprocess.TimeoutExpired:
                continue
            finished = True
            return subprocess.CompletedProcess(argv, proc.returncode, out or "", err or "")
    finally:
        if not finished:
            _kill(proc)


def make_error(code: str, message: str, argv: list[str]) -> HerdrError:
    if code == PROTOCOL_MISMATCH:
        message = (
            f"{message} (the herdr client and the running server speak different protocols; "
            "stop this session's server and let polyagent start it again)"
        )
    cls = HerdrUnavailableError if code in UNAVAILABLE_CODES else HerdrError
    text = _redact_text(message, argv, echo_risk=code in (USAGE, UNKNOWN))[:MAX_MESSAGE]
    return cls(code, text, redact_argv(argv))


def parse_error(proc: subprocess.CompletedProcess[str], argv: list[str]) -> HerdrError:
    for stream in (proc.stderr, proc.stdout):
        try:
            err = json.loads(stream).get("error")
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        if isinstance(err, dict):
            return make_error(str(err.get("code") or UNKNOWN), str(err.get("message") or ""), argv)
    code = USAGE if proc.returncode == 2 else UNKNOWN
    msg = (proc.stderr or proc.stdout or "").strip()  # make_error redacts, then truncates
    return make_error(code, msg or f"herdr exited with {proc.returncode}", argv)
