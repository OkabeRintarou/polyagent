"""``AgentSpec``, ``ClaudeProvider``, ``ToolPolicy`` and their static validation (design D7, D8, D16).

Everything here is checked before any herdr resource is created. Context-dependent checks (the
effective tool policy of a spec whose ``policy`` is unset) happen in :meth:`AgentSpec.resolve`.
"""

from __future__ import annotations

import enum
import os
import re
import stat
import urllib.parse
from dataclasses import InitVar, dataclass, field
from typing import Any, Iterator, Mapping

from ._rules import CURSOR_UNSUPPORTED, KINDS, UNSUPPORTED_KINDS
from .errors import InvalidConfigError

__all__ = ["AgentSpec", "ClaudeProvider", "ToolPolicy", "ENV_NAME_RE", "MODEL_RE", "MCP_NAME_RE"]

ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]=,+-]*")  # first character alphanumeric (D8)
MCP_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
MCP_TOOL_SEP = "/"  # `server` or `server/tool`; `/` is outside the MCP name character set
# Values travel as one shell input line (herdr types the start command, D8) or through an
# exported script (D6); control characters cannot be guaranteed to arrive verbatim.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class ToolPolicy(str, enum.Enum):
    """Tool policy of one agent (D8). Unset in :class:`AgentSpec` means "decided by context"."""

    FULL_AUTO = "full_auto"
    READ_ONLY = "read_only"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


def _policy(value: object) -> str | None:
    if value is None:
        return None
    try:
        return ToolPolicy(value).value
    except ValueError:
        raise InvalidConfigError(f"unknown tool policy {value!r}: use 'full_auto' or 'read_only'") from None


def _plain(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidConfigError(f"{what} must be a non-empty string")
    if _CONTROL_RE.search(value):
        raise InvalidConfigError(f"{what} contains control characters that cannot be passed verbatim")
    return value


class FrozenEnv(Mapping[str, str]):
    """Read-only, picklable mapping of environment variables."""

    __slots__ = ("_d",)

    def __init__(self, data: Mapping[str, str] = ()) -> None:  # type: ignore[assignment]
        self._d = dict(data)

    def __getitem__(self, key: str) -> str:
        return self._d[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self._d) == dict(other)

    def __hash__(self) -> int:
        return hash(frozenset(self._d.items()))

    def __getstate__(self):
        return self._d

    def __setstate__(self, state) -> None:
        self._d = dict(state)

    def __repr__(self) -> str:  # names only: values may be credentials
        return f"<{len(self._d)} variables: {sorted(self._d)}>"


def _env_value(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidConfigError(f"value of environment variable {name} must be a string")
    if "\x00" in value:
        raise InvalidConfigError(f"value of environment variable {name} contains a NUL character")
    return value


# ---- claude providers (D7) ------------------------------------------------------

# Variable families a provider owns; all are unset in the pane before an explicit provider's
# variables are exported (D6), including inherited custom request headers.
PROVIDER_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_")
PROVIDER_ENV_NAMES = ("CLOUD_ML_REGION",)

_MODEL_VARS = frozenset({"ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"})
CUSTOM_HEADERS_VAR = "ANTHROPIC_CUSTOM_HEADERS"
HEADER_NAME_RE = re.compile(r"[A-Za-z0-9-]+")
# Per type: the only variables it may export, and those that must be present (name -> required
# value, or None for any non-empty value). Applies to direct construction too (review C2).
_PROVIDER_SCHEMA: dict[str, tuple[frozenset[str], dict[str, str | None]]] = {
    "official": (frozenset(), {}),
    "compatible": (
        frozenset({"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", CUSTOM_HEADERS_VAR}) | _MODEL_VARS,
        {"ANTHROPIC_BASE_URL": None},
    ),
    "bedrock": (
        frozenset({"CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                   "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"}) | _MODEL_VARS,
        {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": None},
    ),
    "vertex": (
        frozenset({"CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID",
                   "GOOGLE_APPLICATION_CREDENTIALS"}) | _MODEL_VARS,
        {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": None, "ANTHROPIC_VERTEX_PROJECT_ID": None},
    ),
}
_PROVIDER_SECRET_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                         "AWS_BEARER_TOKEN_BEDROCK", CUSTOM_HEADERS_VAR)


def _header_lines(headers: Mapping[str, str]) -> str:
    """``custom_headers`` as ``ANTHROPIC_CUSTOM_HEADERS``: one ``Name: value`` per line (D7). Several
    headers, and values with colons and spaces, arrive verbatim (task 13.12, claude 2.1.322, local stub)."""
    if not isinstance(headers, Mapping):
        raise InvalidConfigError("custom_headers must be a mapping of header names to values")
    lines = []
    for name, value in headers.items():
        if not isinstance(name, str) or not HEADER_NAME_RE.fullmatch(name):
            raise InvalidConfigError(f"invalid custom header name {name!r}: must match [A-Za-z0-9-]+")
        if not isinstance(value, str) or not value.strip():
            raise InvalidConfigError(f"custom header {name} needs a non-empty value")
        if _CONTROL_RE.search(value):
            raise InvalidConfigError(f"value of custom header {name} contains control characters")
        lines.append(f"{name}: {value}")
    if not lines:
        raise InvalidConfigError("custom_headers is empty; omit it instead")
    return "\n".join(lines)


def _parse_header_lines(text: str) -> dict[str, str]:
    """Inverse of :func:`_header_lines`, used to validate directly constructed providers."""
    if not isinstance(text, str):
        raise InvalidConfigError(f"{CUSTOM_HEADERS_VAR} must be a string")
    headers: dict[str, str] = {}
    for line in text.split("\n"):
        name, sep, value = line.partition(": ")
        if not sep:
            raise InvalidConfigError(f"{CUSTOM_HEADERS_VAR} lines must read 'Name: value'")
        headers[name] = value
    return headers


def _check_base_url(url: str) -> None:
    """http(s) URL without embedded credentials: user info, a query string or a fragment would carry
    secrets outside ``secret_values()`` (review N5); credentials go through auth_token / api_key /
    custom_headers. The URL itself is never echoed in the error."""
    if not re.match(r"https?://[^\s/]+", url):
        raise InvalidConfigError("base_url must be an http(s) URL")
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise InvalidConfigError("base_url is not a valid URL") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidConfigError("base_url must not contain user info; pass credentials as auth_token or api_key")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise InvalidConfigError(
            "base_url must not contain a query string or fragment; pass credentials as auth_token, api_key "
            "or custom_headers"
        )


def _check_provider_env(ptype: str, env: Mapping[str, str]) -> None:
    allowed, required = _PROVIDER_SCHEMA[ptype]
    extra = sorted(set(env) - allowed)
    if extra:
        raise InvalidConfigError(f"a {ptype} claude provider cannot set {extra}")
    if CUSTOM_HEADERS_VAR in env:  # newline separates headers; every value is checked on its own
        _header_lines(_parse_header_lines(env[CUSTOM_HEADERS_VAR]))
    for name, value in env.items():
        if name != CUSTOM_HEADERS_VAR:
            _plain(value, name)
    for name, want in required.items():
        if name not in env or (want is not None and env[name] != want):
            raise InvalidConfigError(f"a {ptype} claude provider needs {name}" + (f"={want}" if want else ""))
    for name in _MODEL_VARS & set(env):
        _model(env[name])
    if ptype == "compatible":
        _check_base_url(env["ANTHROPIC_BASE_URL"])
        if ("ANTHROPIC_AUTH_TOKEN" in env) == ("ANTHROPIC_API_KEY" in env):
            raise InvalidConfigError("a compatible provider needs exactly one of auth_token or api_key")
    if ptype == "bedrock":
        if ("AWS_ACCESS_KEY_ID" in env) != ("AWS_SECRET_ACCESS_KEY" in env):
            raise InvalidConfigError("access_key_id and secret_access_key go together")
        if "AWS_SESSION_TOKEN" in env and "AWS_ACCESS_KEY_ID" not in env:
            raise InvalidConfigError("session_token needs access_key_id and secret_access_key")


@dataclass(frozen=True, eq=False)
class ClaudeProvider:
    """Backend of one claude instance. Build with the class methods; direct construction gets the
    same validation. The variables are not a dataclass field, so ``repr``, :meth:`to_dict` and
    ``dataclasses.asdict`` all carry the type name only (D7, review N4); pickling keeps everything."""

    type: str
    # Defaults to None only so that `dataclasses.replace()` reaches the validation below and fails
    # with InvalidConfigError instead of silently dropping or keeping the variables.
    env_vars: InitVar[Mapping[str, str] | None] = None

    def __post_init__(self, env_vars: Mapping[str, str] | None) -> None:
        if self.type not in _PROVIDER_SCHEMA:
            raise InvalidConfigError(f"unknown claude provider type {self.type!r}")
        if not isinstance(env_vars, Mapping):
            raise InvalidConfigError("provider variables must be a mapping (build providers with the class methods)")
        _check_provider_env(self.type, env_vars)
        object.__setattr__(self, "_env", FrozenEnv(env_vars))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClaudeProvider):
            return NotImplemented
        return self.type == other.type and self._env == other._env

    def __hash__(self) -> int:
        return hash((self.type, self._env))

    # -- constructors --
    @classmethod
    def official(cls) -> "ClaudeProvider":
        """Anthropic's own service with claude's own login; exports nothing."""
        return cls("official", {})

    @classmethod
    def compatible(
        cls,
        base_url: str,
        *,
        auth_token: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        small_fast_model: str | None = None,
        custom_headers: Mapping[str, str] | None = None,
    ) -> "ClaudeProvider":
        """An Anthropic-compatible endpoint. Exactly one of ``auth_token`` / ``api_key``.
        ``custom_headers`` (one ``name -> value`` for now, task 13.12) is sent with every request;
        its values are treated as credentials."""
        env = {"ANTHROPIC_BASE_URL": _plain(base_url, "base_url")}
        if auth_token is not None:
            env["ANTHROPIC_AUTH_TOKEN"] = _plain(auth_token, "auth_token")
        if api_key is not None:
            env["ANTHROPIC_API_KEY"] = _plain(api_key, "api_key")
        env.update(_models(model, small_fast_model))
        if custom_headers is not None:
            env[CUSTOM_HEADERS_VAR] = _header_lines(custom_headers)
        return cls("compatible", env)

    @classmethod
    def bedrock(
        cls,
        region: str,
        *,
        profile: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        bearer_token: str | None = None,
        model: str | None = None,
        small_fast_model: str | None = None,
    ) -> "ClaudeProvider":
        """AWS Bedrock. Inherited ``AWS_*`` variables are cleared, so credentials come from here
        (keys, a bearer token, or a named profile from the AWS config files)."""
        env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": _plain(region, "region")}
        for name, value in (("AWS_PROFILE", profile), ("AWS_ACCESS_KEY_ID", access_key_id),
                            ("AWS_SECRET_ACCESS_KEY", secret_access_key), ("AWS_SESSION_TOKEN", session_token),
                            ("AWS_BEARER_TOKEN_BEDROCK", bearer_token)):
            if value is not None:
                env[name] = _plain(value, name)
        env.update(_models(model, small_fast_model))
        return cls("bedrock", env)

    @classmethod
    def vertex(
        cls,
        region: str,
        project_id: str,
        *,
        credentials_file: str | os.PathLike[str] | None = None,
        model: str | None = None,
        small_fast_model: str | None = None,
    ) -> "ClaudeProvider":
        """Google Vertex AI. ``credentials_file`` becomes ``GOOGLE_APPLICATION_CREDENTIALS``."""
        env = {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLOUD_ML_REGION": _plain(region, "region"),
            "ANTHROPIC_VERTEX_PROJECT_ID": _plain(project_id, "project_id"),
        }
        if credentials_file is not None:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = _plain(os.fspath(credentials_file), "credentials_file")
        env.update(_models(model, small_fast_model))
        return cls("vertex", env)

    # -- use --
    @property
    def is_official(self) -> bool:
        return self.type == "official"

    def env(self) -> dict[str, str]:
        """Variables to export in the pane (after :meth:`clear_rules` have been applied)."""
        return dict(self._env)

    @staticmethod
    def clear_rules() -> tuple[tuple[str, ...], tuple[str, ...]]:
        """``(prefixes, names)`` of every provider-related variable to unset first (D6)."""
        return PROVIDER_ENV_PREFIXES, PROVIDER_ENV_NAMES

    def secret_values(self) -> tuple[str, ...]:
        """Credential values, for redaction of records and events (D6)."""
        out = [self._env[n] for n in _PROVIDER_SECRET_VARS if n in self._env]
        if CUSTOM_HEADERS_VAR in self._env:  # the bare header values too, as they may be echoed alone
            out += list(_parse_header_lines(self._env[CUSTOM_HEADERS_VAR]).values())
        return tuple(dict.fromkeys(out))

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form: the type name only, never endpoints or credentials."""
        return {"type": self.type}


def _models(model: str | None, small_fast_model: str | None) -> dict[str, str]:
    out = {}
    if model is not None:
        out["ANTHROPIC_MODEL"] = _model(model)
    if small_fast_model is not None:
        out["ANTHROPIC_SMALL_FAST_MODEL"] = _model(small_fast_model)
    return out


def _model(value: object) -> str:
    if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
        raise InvalidConfigError(
            f"invalid model name {value!r}: must match [A-Za-z0-9][A-Za-z0-9._:/@\\[\\]=,+-]*"
        )
    return value


def is_provider_env(name: str) -> bool:
    return name.startswith(PROVIDER_ENV_PREFIXES) or name in PROVIDER_ENV_NAMES


# ---- AgentSpec ---------------------------------------------------------------------


def _mcp_entry(entry: object) -> str:
    if not isinstance(entry, str):
        raise InvalidConfigError(f"MCP allow-list entries must be strings, got {entry!r}")
    parts = entry.split(MCP_TOOL_SEP)
    # `__` is claude's own separator in `mcp__<server>__<tool>`: `a__b` and `a/b` would collide (review C5).
    # Edge underscores join the separator too: server `a_` + tool `b` and server `a` + tool `_b` (D8).
    if len(parts) > 2 or not all(
        MCP_NAME_RE.fullmatch(p) and "__" not in p and not p.startswith("_") and not p.endswith("_") for p in parts
    ):
        raise InvalidConfigError(
            f"invalid MCP allow-list entry {entry!r}: use 'server' or 'server/tool' with names "
            "matching [A-Za-z0-9._-]+, without '__' and not starting or ending with '_'"
        )
    return entry


def _readable_regular_file(path: str) -> None:
    try:
        st = os.stat(path)
    except OSError as e:
        raise InvalidConfigError(f"mcp_config {path!r} cannot be read: {e.strerror or e}") from None
    if not stat.S_ISREG(st.st_mode):
        raise InvalidConfigError(f"mcp_config {path!r} is not a regular file")
    if not os.access(path, os.R_OK):
        raise InvalidConfigError(f"mcp_config {path!r} is not readable by the current user")


@dataclass(frozen=True)
class AgentSpec:
    """What to start. ``policy=None`` means: ``full_auto`` for :meth:`PolyAgent.start`,
    ``read_only`` for discussion members (D8)."""

    kind: str
    model: str | None = None
    extra_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    provider: ClaudeProvider | None = None
    policy: ToolPolicy | str | None = None
    mcp_allow: tuple[str, ...] = ()
    mcp_config: str | None = None
    auto_trust: bool = True

    def __post_init__(self) -> None:
        if self.kind in UNSUPPORTED_KINDS:
            raise InvalidConfigError(CURSOR_UNSUPPORTED)
        if self.kind not in KINDS:
            raise InvalidConfigError(f"unsupported agent kind {self.kind!r}: use one of {', '.join(KINDS)}")
        if self.model is not None:
            _model(self.model)
        if isinstance(self.extra_args, str):
            raise InvalidConfigError("extra_args must be a sequence of arguments, not a string")
        object.__setattr__(self, "extra_args", tuple(_plain(a, "extra argument") for a in self.extra_args))
        if not isinstance(self.env, Mapping):
            raise InvalidConfigError("env must be a mapping of variable names to values")
        env = {}
        for name, value in self.env.items():
            if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
                raise InvalidConfigError(f"invalid environment variable name {name!r}")
            env[name] = _env_value(name, value)
        object.__setattr__(self, "env", FrozenEnv(env))
        if self.provider is not None:
            if not isinstance(self.provider, ClaudeProvider):
                raise InvalidConfigError("provider must be a ClaudeProvider")
            if self.kind != "claude":
                raise InvalidConfigError("a provider can only be given for claude")
            clash = sorted(n for n in env if is_provider_env(n))
            if clash:
                raise InvalidConfigError(
                    f"env sets provider variables {clash} while a provider is given; put them in the provider"
                )
        object.__setattr__(self, "policy", _policy(self.policy))
        if isinstance(self.mcp_allow, str):
            raise InvalidConfigError("mcp_allow must be a sequence of entries, not a string")
        object.__setattr__(self, "mcp_allow", tuple(dict.fromkeys(_mcp_entry(e) for e in self.mcp_allow)))
        if self.mcp_config is not None:
            if self.kind != "claude":
                raise InvalidConfigError("mcp_config is only used by read_only claude")
            path = os.path.abspath(_plain(os.fspath(self.mcp_config), "mcp_config"))
            object.__setattr__(self, "mcp_config", path)
        if not isinstance(self.auto_trust, bool):
            raise InvalidConfigError("auto_trust must be a bool")
        if self.policy is not None:
            self._check_policy_fields(self.policy)

    def _check_policy_fields(self, policy: str) -> None:
        if policy == ToolPolicy.FULL_AUTO.value:
            if self.mcp_allow:
                raise InvalidConfigError(
                    "mcp_allow restricts nothing under full_auto (every MCP tool is allowed); "
                    "drop it or use read_only"
                )
            if self.mcp_config is not None:
                raise InvalidConfigError("mcp_config is only used by read_only claude")
        elif self.mcp_config is not None:
            if not self.mcp_allow:
                raise InvalidConfigError(
                    "mcp_config without mcp_allow would be ignored (read_only claude starts no MCP server "
                    "outside the allow-list); give the allow-list or drop mcp_config"
                )
            _readable_regular_file(self.mcp_config)

    def effective_policy(self, default: ToolPolicy | str) -> str:
        """The policy this spec runs with when the context's default is ``default``."""
        return self.policy if self.policy is not None else _policy(default)  # type: ignore[return-value]

    def resolve(self, default: ToolPolicy | str) -> "AgentSpec":
        """A copy with the policy fixed (``default`` if unset), re-validated for that policy."""
        policy = self.effective_policy(default)
        self._check_policy_fields(policy)
        if policy == self.policy:
            return self
        return AgentSpec(
            kind=self.kind, model=self.model, extra_args=self.extra_args, env=dict(self.env),
            provider=self.provider, policy=policy, mcp_allow=self.mcp_allow, mcp_config=self.mcp_config,
            auto_trust=self.auto_trust,
        )

    def secret_values(self) -> tuple[str, ...]:
        """Values to redact from records and events (D6): env values and provider credentials."""
        vals = list(self.env.values())
        if self.provider is not None:
            vals += list(self.provider.secret_values())
        return tuple(dict.fromkeys(vals))

    def __repr__(self) -> str:
        return (
            f"AgentSpec(kind={self.kind!r}, model={self.model!r}, extra_args={self.extra_args!r}, "
            f"env={self.env!r}, provider={self.provider!r}, "
            f"policy={self.policy!r}, mcp_allow={self.mcp_allow!r}, mcp_config={self.mcp_config!r}, "
            f"auto_trust={self.auto_trust!r})"
        )
