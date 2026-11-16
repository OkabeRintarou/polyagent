"""Tool-policy mapping, capability checks, extra-arg/env checks and repo-level config checks (design D8).

Everything here runs before any herdr resource is created (D9 step 1). ``read_only`` restrictions are
applied only under ``read_only``; ``full_auto`` gets the CLI's full-auto arguments and nothing else
(apart from the checks that are not about the tool policy).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from . import kinds as K
from .errors import InvalidConfigError, PolicyNotSupportedError
from .spec import MCP_TOOL_SEP, AgentSpec, ToolPolicy

__all__ = [
    "build_args", "check_static", "require", "check_reserved_args", "check_extra_args", "check_env_names",
    "check_repo_config",
]

READ_ONLY = ToolPolicy.READ_ONLY.value
FULL_AUTO = ToolPolicy.FULL_AUTO.value
# Kinds whose MCP allow-list can name single tools (verified together with their MCP_ALLOW state).
TOOL_LEVEL_MCP = frozenset({"claude"})


def require(adapter: K.KindAdapter, capability: str, what: str) -> None:
    """Raise ``PolicyNotSupportedError`` unless ``capability`` is verified for this kind; an
    unverified capability is refused the same way, naming its verification task (D7)."""
    cap = adapter.capability(capability)
    if cap.state == K.VERIFIED:
        return
    note = f" ({cap.note})" if cap.note else ""
    if cap.state == K.UNVERIFIED:
        raise PolicyNotSupportedError(
            f"{what} is not yet verified for {adapter.kind} (verification task {cap.task}){note}; refusing to start"
        )
    raise PolicyNotSupportedError(f"{what} is not supported for {adapter.kind}{note}")


def _reserved_hit(arg: str, reserved: tuple[str, ...]) -> str | None:
    """The reserved option ``arg`` would set, by the four D8 matching rules, else None."""
    for opt in reserved:
        if arg == opt or arg.startswith(opt + "="):  # exact, or `--opt=value`
            return opt
        if len(opt) == 2 and not opt.startswith("--") and arg.startswith(opt) and not arg.startswith("--"):
            return opt  # short option with its value attached: `-mgpt-5`
    if arg.startswith("--") and len(arg) > 2:
        name = arg.split("=", 1)[0]
        for opt in reserved:
            if opt.startswith("--") and opt.startswith(name):  # an abbreviation a parser might accept
                return opt
    return None


def check_reserved_args(adapter: K.KindAdapter, extra_args: Iterable[str], policy: str = FULL_AUTO) -> None:
    """Both policies: ``extra_args`` must not repeat an option the library sets, change the session
    identity or run mode, or contain the ``--`` terminator (D8 "库保留的启动选项").

    The error is ``InvalidConfigError``; under ``read_only`` it is the subclass
    ``PolicyNotSupportedError``, because such an argument is also outside the read_only whitelist and
    the agent-tool-policy spec ("附加参数放宽权限", e.g. copilot ``--yolo``) requires that error there
    (phase-3 finding P3-6)."""
    error = PolicyNotSupportedError if policy == READ_ONLY else InvalidConfigError
    for arg in extra_args:
        if arg == "--":
            raise error(
                "extra_args must not contain '--': it would turn the library's own arguments into a prompt"
            )
        hit = _reserved_hit(arg, adapter.reserved_options)
        if hit is not None:
            raise error(
                f"extra argument {arg!r} would set {hit}, which polyagent reserves for {adapter.kind} "
                "(options it sets itself, session identity and run mode)"
            )


def check_extra_args(adapter: K.KindAdapter, extra_args: Iterable[str]) -> None:
    """read_only: every extra argument must be a whitelisted option with an allowed value (D8)."""
    allowed = {o.option: o for o in adapter.read_only_safe_options}
    args = list(extra_args)
    i = 0
    while i < len(args):
        arg = args[i]
        option, eq, value = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        safe = allowed.get(option)
        if safe is None:
            raise PolicyNotSupportedError(
                f"extra argument {arg!r} is not in the read_only whitelist for {adapter.kind} "
                f"(allowed: {sorted(allowed) or 'none'})"
            )
        if not eq:
            if i + 1 >= len(args):
                raise PolicyNotSupportedError(f"extra argument {option!r} needs a value")
            i += 1
            value = args[i]
        if not safe.value.fullmatch(value):
            raise PolicyNotSupportedError(f"value {value!r} of extra argument {option!r} is not allowed under read_only")
        i += 1


def check_env_names(adapter: K.KindAdapter, names: Iterable[str], policy: str) -> None:
    """Caller-provided variable names. read_only: no generic sensitive names and nothing from the
    kind's configuration/permission families. Both policies: the kind's always-forbidden families."""
    for name in names:
        if adapter.always_forbidden_env_prefixes and name.startswith(adapter.always_forbidden_env_prefixes):
            raise InvalidConfigError(f"{name} is set by polyagent itself for {adapter.kind} and cannot be provided")
        if policy != READ_ONLY:
            continue
        if (
            name in K.GENERIC_FORBIDDEN_ENV_NAMES
            or name.startswith(K.GENERIC_FORBIDDEN_ENV_PREFIXES)
            or name.startswith(adapter.config_env_prefixes)
            or name in adapter.config_env_names
        ):
            raise PolicyNotSupportedError(
                f"environment variable {name} could change {adapter.kind}'s permissions, sandbox or configuration "
                "location; it is not accepted under read_only"
            )


def _repo_chain(cwd: Path) -> list[Path]:
    """``cwd`` and its parents up to the enclosing git repository root (``cwd`` alone if none)."""
    chain = []
    for d in (cwd, *cwd.parents):
        chain.append(d)
        if os.path.lexists(d / ".git"):
            return chain
    return [cwd]


def check_repo_config(adapter: K.KindAdapter, cwd: str | os.PathLike[str]) -> None:
    """read_only: refuse when repo-level executable configuration exists between ``cwd`` and the
    repository root, for kinds that cannot exclude project-level configuration (D8).

    Both the literal path and the symlink-resolved path are walked: the agent runs in the real
    directory, so its repository is the one above the resolved path (review C1); the literal chain
    is kept in case a CLI looks upward along the path it was given."""
    if adapter.excludes_project_config or not adapter.repo_config_paths:
        return
    literal = Path(os.path.abspath(cwd))
    dirs = dict.fromkeys(_repo_chain(literal) + _repo_chain(Path(os.path.realpath(literal))))
    for d in dirs:
        for rel in adapter.repo_config_paths:
            p = d / rel
            if os.path.lexists(p):
                raise PolicyNotSupportedError(
                    f"{p} is repository-level configuration that {adapter.kind} would load (hooks, MCP servers, "
                    "plugins or permissions); read_only cannot exclude it"
                )


def check_static(spec: AgentSpec, cwd: str | os.PathLike[str], default_policy: str = FULL_AUTO) -> AgentSpec:
    """D9 step 1 for one agent: fix the policy (``default_policy`` when unset) and run every
    policy-related check. Returns the spec with its policy fixed."""
    spec = spec.resolve(default_policy)
    adapter = K.adapter(spec.kind)
    policy = spec.policy
    check_reserved_args(adapter, spec.extra_args, policy)  # both policies, before the read_only whitelist
    check_env_names(adapter, spec.env, policy)  # type: ignore[arg-type]
    if policy != READ_ONLY:
        return spec
    require(adapter, K.READ_ONLY, "the read_only tool policy")
    if spec.kind == "claude" and (spec.provider is None or spec.provider.is_official):
        raise PolicyNotSupportedError(
            "read_only claude runs with --restricted, which ignores the login stored in user settings; "
            "give an explicit compatible, Bedrock or Vertex provider (the official login is unverified, task 13.6)"
        )
    if spec.mcp_allow:
        require(adapter, K.MCP_ALLOW, "an MCP allow-list")
        if adapter.kind not in TOOL_LEVEL_MCP and any(MCP_TOOL_SEP in e for e in spec.mcp_allow):
            raise PolicyNotSupportedError(f"{adapter.kind} only supports server-level MCP allow-lists")
        if spec.kind == "claude" and spec.mcp_config is None:
            raise PolicyNotSupportedError(
                "read_only claude does not load existing MCP configuration; give the server definitions as mcp_config"
            )
    check_extra_args(adapter, spec.extra_args)
    check_repo_config(adapter, cwd)
    return spec


def _mcp_allow_entry(entry: str) -> str:
    server, _, tool = entry.partition(MCP_TOOL_SEP)
    return f"mcp__{server}__{tool}" if tool else f"mcp__{server}"


def build_args(spec: AgentSpec, cwd: str | os.PathLike[str], session_id: str | None = None) -> list[str]:
    """Native arguments for a spec returned by :func:`check_static` (its policy is fixed)."""
    if spec.policy is None:
        raise ValueError("build_args needs a resolved spec (see check_static)")
    adapter = K.adapter(spec.kind)
    ro = spec.policy == READ_ONLY
    check_reserved_args(adapter, spec.extra_args, spec.policy)
    if ro:  # never emit read_only arguments for a kind whose read_only is not verified (review C6)
        require(adapter, K.READ_ONLY, "the read_only tool policy")
        if spec.mcp_allow:
            require(adapter, K.MCP_ALLOW, "an MCP allow-list")
    allow_extra = tuple(_mcp_allow_entry(e) for e in spec.mcp_allow) if ro else ()
    args = list(spec.extra_args)
    if spec.auto_trust:
        if spec.kind == "codex":  # trusted for this run only, nothing written to config.toml (D9)
            args += ["-c", "projects={" + json.dumps(os.path.abspath(cwd)) + '={trust_level="trusted"}}']
        elif adapter.trust_by_flag:
            args += ["--trust"]
    if ro and spec.env:
        names = sorted(spec.env)
        if spec.kind == "codex":
            args += ["-c", "shell_environment_policy.exclude=[" + ",".join(json.dumps(n) for n in names) + "]"]
        elif adapter.secret_env_flag:
            args += [f"{adapter.secret_env_flag}={','.join(names)}"]
    if ro and spec.mcp_allow and spec.mcp_config is not None:
        args += ["--mcp-config", spec.mcp_config]
    args += adapter.native_args(spec.policy, model=spec.model, session_id=session_id, allow_extra=allow_extra)
    return args
