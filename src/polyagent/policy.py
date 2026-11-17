"""Tool-policy mapping, capability checks, extra-arg/env checks and repo-level config checks (design D8).

Everything here runs before any herdr resource is created (D9 step 1). ``read_only`` restrictions are
applied only under ``read_only``; ``full_auto`` gets the CLI's full-auto arguments and nothing else
(apart from the checks that are not about the tool policy).
"""

from __future__ import annotations

import dataclasses
import json
import os
import pwd
import re
import stat
from pathlib import Path
from typing import Iterable

from . import kinds as K
from .errors import InvalidConfigError, PolicyNotSupportedError
from .spec import _CONTROL_RE, MCP_TOOL_SEP, AgentSpec, ToolPolicy

__all__ = [
    "build_args", "check_static", "require", "check_reserved_args", "check_extra_args", "check_env_names",
    "check_repo_config", "check_skills_dir",
]

READ_ONLY = ToolPolicy.READ_ONLY.value
FULL_AUTO = ToolPolicy.FULL_AUTO.value
# Kinds whose MCP allow-list can name single tools (verified together with their MCP_ALLOW state).
TOOL_LEVEL_MCP = frozenset({"claude"})
# D18: a skills directory must not equal, lie in or contain these (relative to the user's home), nor be or
# contain the home itself: the three CLIs' user configuration, ~/.agents, ~/.config and polyagent's state.
SKILLS_FORBIDDEN = (".claude", ".copilot", ".codex", ".agents", ".config", ".local/state/polyagent")


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
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
        # Short options: attached value (`-mgpt-5`) or a cluster (`-sr` = `-s -r`). Any reserved short
        # letter anywhere in the element counts; values must then be given as a separate element (review N2).
        for opt in reserved:
            if len(opt) == 2 and opt[0] == "-" and opt[1] != "-" and opt[1] in arg[1:].split("=", 1)[0]:
                return opt
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
    kind's configuration/permission families."""
    if policy != READ_ONLY:
        return
    for name in names:
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
    # realpath of the path as given: abspath would drop `link/..` before the link is resolved (review N1).
    real = Path(os.path.realpath(cwd))
    dirs = dict.fromkeys(_repo_chain(literal) + _repo_chain(real))
    for d in dirs:
        for rel in adapter.repo_config_paths:
            p = d / rel
            if os.path.lexists(p):
                raise PolicyNotSupportedError(
                    f"{p} is repository-level configuration that {adapter.kind} would load (hooks, MCP servers, "
                    "plugins or permissions); read_only cannot exclude it"
                )


def passwd_home() -> Path:
    """The passwd home, as ``locks.user_home`` (policy does not depend on locks, see the module table)."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        raise InvalidConfigError("the current user has no passwd entry; its home cannot be told apart") from None


def _config_locations(adapter: K.KindAdapter, spec: AgentSpec, cwd: str | os.PathLike[str]) -> tuple[Path, list[Path]]:
    """D18: the user's home (refused when equal to or inside the skills directory) and the configuration
    locations a skills directory must not equal, lie in or contain: the default ones under the passwd home,
    plus the directory ``env`` names with the kind's configuration variable (the agent's real one then)."""
    home = Path(os.path.realpath(passwd_home()))
    places = [Path(os.path.realpath(home / rel)) for rel in SKILLS_FORBIDDEN]
    value = spec.env.get(adapter.config_dir_var, "")
    if value:  # exported verbatim, so a relative value is taken from the pane's working directory
        places.append(Path(os.path.realpath(os.path.join(cwd, value))))
        if value == "~" or value.startswith("~/"):  # in case the CLI expands it itself
            places.append(Path(os.path.realpath(home / value[2:])))
    return home, places


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_skills_dir(adapter: K.KindAdapter, spec: AgentSpec, cwd: str | os.PathLike[str]) -> str:
    """D18 static checks of ``skills_dir`` (before any herdr call): the real path is a readable directory
    holding at least one skill, away from the user's configuration; its kind-specific variables are not
    given in ``env`` too; then the kind's capability and its own requirements. Returns the real path, which
    is what the agent is given. The library writes nothing for it."""
    real = Path(os.path.realpath(spec.skills_dir))  # type: ignore[arg-type]
    shown = f"skills_dir {spec.skills_dir!r}" + ("" if str(real) == spec.skills_dir else f" (real path {str(real)!r})")
    try:  # the real path is what the CLI gets: same characters as at construction, and it goes into a script
        str(real).encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidConfigError(f"{shown} is not valid UTF-8; it is passed in a UTF-8 script or argument") from None
    if _CONTROL_RE.search(str(real)):
        raise InvalidConfigError(f"{shown} contains control characters that cannot be passed verbatim")
    try:
        st = os.stat(real)
    except OSError as e:
        raise InvalidConfigError(f"{shown} cannot be used: {e.strerror or e}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise InvalidConfigError(f"{shown} is not a directory")
    if not os.access(real, os.R_OK | os.X_OK):
        raise InvalidConfigError(f"{shown} cannot be read and entered by the current user")
    home, forbidden = _config_locations(adapter, spec, cwd)
    if _within(home, real) or any(_within(real, f) or _within(f, real) for f in forbidden):
        raise InvalidConfigError(
            f"{shown} is, contains or lies in the user's home or agent configuration directories (~/.claude, "
            f"~/.copilot, ~/.codex, ~/.agents, ~/.config, polyagent's state, {adapter.config_dir_var} given in "
            "env): a full_auto agent can rewrite what it loads, and those skills are loaded anyway"
        )
    if not _has_skill(real):
        raise InvalidConfigError(f"{shown} has no subdirectory with a SKILL.md file; each skill is a directory "
                                 "holding SKILL.md")
    clash = sorted(n for n in spec.env if n in adapter.skills_env_vars)
    if clash:
        raise InvalidConfigError(f"env sets {clash} while skills_dir is given; the library sets them for a skills "
                                 "directory")
    for arg in spec.extra_args:
        hit = _reserved_hit(arg, adapter.skills_reserved_options)
        if hit is not None:
            raise InvalidConfigError(f"extra argument {arg!r} would set {hit}, which polyagent sets for "
                                     f"{adapter.kind} when skills_dir is given")
    require(adapter, K.SKILLS_DIR, "a skills directory")
    if adapter.skills_plugin_dir_option:
        _check_plugin_layout(adapter, real, shown)
    if adapter.skills_export and ("," in str(real) or str(real)[-1:].isspace()):
        # a comma-separated list; whether its items are stripped of surrounding white space is not verified
        raise PolicyNotSupportedError(f"{shown}: {adapter.kind} takes skills directories as a comma-separated "
                                      f"list ({adapter.skills_export}), so the path must not contain a comma or "
                                      "end with white space")
    return str(real)


PLUGIN_ROOT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # names starting with '.' or '-' are unverified


def _check_plugin_layout(adapter: K.KindAdapter, real: Path, shown: str) -> None:
    """D18 / task 14.3: the parent is given as a plugin root, whose every component would be loaded, so
    the skills directory must be named `skills` and be its only entry; the parent's name prefixes every
    skill name (`<root>:<skill>`), and a name with a space cannot be called (claude 2.1.322)."""
    root = real.parent
    layout = (f"{adapter.kind} loads skills from a plugin root: put the skills in a directory named 'skills' "
              "that is the only entry of its parent, whose name starts with a letter or digit and uses only "
              "letters, digits, '.', '_' and '-'")
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        raise PolicyNotSupportedError(f"{shown}: its parent cannot be listed ({e.strerror or e}); {layout}") from None
    if real.name != "skills" or entries != ["skills"] or not PLUGIN_ROOT_NAME_RE.fullmatch(root.name):
        raise PolicyNotSupportedError(f"{shown}: {layout}")


def _has_skill(d: Path) -> bool:
    try:
        entries = list(os.scandir(d))
    except OSError:
        return False
    for e in entries:
        try:
            if e.is_dir() and stat.S_ISREG(os.stat(os.path.join(e.path, "SKILL.md")).st_mode):
                return True
        except OSError:
            continue
    return False


def check_static(spec: AgentSpec, cwd: str | os.PathLike[str], default_policy: str = FULL_AUTO) -> AgentSpec:
    """D9 step 1 for one agent: fix the policy (``default_policy`` when unset) and run every
    policy-related check. Returns the spec with its policy fixed."""
    spec = spec.resolve(default_policy)
    adapter = K.adapter(spec.kind)
    policy = spec.policy
    check_reserved_args(adapter, spec.extra_args, policy)  # both policies, before the read_only whitelist
    check_env_names(adapter, spec.env, policy)  # type: ignore[arg-type]
    if spec.skills_dir is not None:  # only full_auto gets here (AgentSpec refuses it under read_only)
        spec = dataclasses.replace(spec, skills_dir=check_skills_dir(adapter, spec, cwd))
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
    if ro and spec.env:
        names = sorted(spec.env)
        if spec.kind == "codex":
            args += ["-c", "shell_environment_policy.exclude=[" + ",".join(json.dumps(n) for n in names) + "]"]
        elif adapter.secret_env_flag:
            args += [f"{adapter.secret_env_flag}={','.join(names)}"]
    if ro and spec.mcp_allow and spec.mcp_config is not None:
        args += ["--mcp-config", spec.mcp_config]
    undeny: tuple[str, ...] = ()
    if spec.skills_dir is not None and not ro:  # D18; check_static lets only full_auto and verified kinds here
        require(adapter, K.SKILLS_DIR, "a skills directory")
        if adapter.skills_plugin_dir_option:
            args += [adapter.skills_plugin_dir_option, os.path.dirname(spec.skills_dir)]
        args += list(adapter.skills_args)
        undeny = adapter.skills_allow_tools
    args += adapter.native_args(spec.policy, model=spec.model, session_id=session_id, allow_extra=allow_extra,
                                undeny=undeny)
    return args
