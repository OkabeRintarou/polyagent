"""Per-kind adapter table (design D7): native arguments per tool policy, capability states,
environment rules, repo-level configuration, start dialogs, prompt guards, keys and log binding.

Adding a kind only touches this module. Values come from the verification tasks 3.1–3.4 and 13.x;
evidence lives under ``tests/fixtures/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

from ._rules import KINDS

# ---- capability states (D7) ------------------------------------------------------

VERIFIED = "verified"
UNSUPPORTED = "unsupported"
UNVERIFIED = "unverified"

READ_ONLY = "read_only"
MCP_ALLOW = "mcp_allow"
LOG_ADAPTER = "log_adapter"
INTERRUPT_KEY = "interrupt_key"
# Screen capture (D12): the footer markers and echo rules are backed by screen samples of this kind.
SCREEN_CAPTURE = "screen_capture"
# Turn attribution (D8): tools that keep producing output after the turn's end marker are disabled.
# Stated per policy because copilot differs between the two (task 13.1).
TURN_ATTRIBUTION_READ_ONLY = "turn_attribution.read_only"
TURN_ATTRIBUTION_FULL_AUTO = "turn_attribution.full_auto"

IGNORE_USER_CONFIG = "ignore_user_config"


def turn_attribution(policy: str) -> str:
    """The capability key for the turn-attribution tool limits under ``policy``."""
    return TURN_ATTRIBUTION_READ_ONLY if policy == READ_ONLY else TURN_ATTRIBUTION_FULL_AUTO


@dataclass(frozen=True)
class Capability:
    state: str
    task: str | None = None  # verification task that would change an unverified state
    note: str = ""

    def __post_init__(self) -> None:
        if self.state not in (VERIFIED, UNSUPPORTED, UNVERIFIED):
            raise ValueError(f"bad capability state {self.state!r}")
        if self.state == UNVERIFIED and not self.task:
            raise ValueError("an unverified capability must name its verification task")


# ---- start dialogs (D9) -----------------------------------------------------------

TRUST = "trust"
PROVIDER_CREDENTIALS = "provider_credentials"
FULL_AUTO_CONFIRM = "full_auto_confirm"


@dataclass(frozen=True)
class Dialog:
    name: str
    category: str  # TRUST | PROVIDER_CREDENTIALS | FULL_AUTO_CONFIRM
    pattern: re.Pattern[str]  # anchored on the dialog's own text, several phrases in order
    keys: tuple[str, ...] | None  # named keys that accept it; None: never answered (AgentBlockedError)
    persisted: bool  # answering it writes a lasting decision


def _dialog(name: str, category: str, *phrases: str, keys: tuple[str, ...] | None, persisted: bool) -> Dialog:
    pattern = re.compile(r"[\s\S]*?".join(re.escape(p).replace(r"\ ", r"\s+") for p in phrases))
    return Dialog(name, category, pattern, keys, persisted)


# ---- prompt guards (D10) -----------------------------------------------------------

GUIDE_LINE_TEXT = "Message from the user follows:"
GUARDED_PREFIXES = frozenset("/!#@$?-")
# copilot 1.0.112 keeps a paste of more than 20 KiB (UTF-8) out of the message: it is saved to a file
# and the message is `<pasted_content file=… />` (task 13.14, tests/fixtures/logs/copilot).
COPILOT_PASTE_LIMIT = 20 * 1024


@dataclass(frozen=True)
class PromptGuard:
    """D10: a prompt whose first effective character is in ``applies_to`` gets the fixed guide line
    (``GUIDE_LINE_TEXT``) in front of it. Task 13.8: the guide line works on all three v1 kinds; codex's
    earlier leading space and its refused prefixes are gone."""

    applies_to: frozenset[str]  # first non-blank characters that get the guard
    # First non-blank characters refused outright -> the task that could lift the refusal, or None
    # when no guard works (verified).
    rejected: Mapping[str, str | None]
    # Largest prompt (UTF-8 bytes, after the guard) the kind takes as message text; None: only the
    # library-wide limit. Task 13.14: copilot saves a longer paste to a file and the message becomes a
    # reference to it, so the model never sees the text.
    max_bytes: int | None = None


# ---- screen footer (D12 "内容区和页脚") ----------------------------------------------------


@dataclass(frozen=True)
class FooterBlock:
    """A bottom footer block (D12): it starts at the last line of the screen read matching ``input_line``
    (the input line), which must not match ``input_exclude``; it is followed by ``min_after`` to
    ``max_after`` non-blank lines, and when ``status_line`` is given every one of them satisfies it. A
    screen without such a block is not in a recognizable input state."""

    input_line: re.Pattern[str]
    max_after: int = 4
    min_after: int = 0
    # The input line must not match this (codex: a numbered option of a selection dialog, D12).
    input_exclude: re.Pattern[str] | None = None
    # A check of each line after the input line by its fields (codex's status line, D12 rule 4).
    status_line: Callable[[str], bool] | None = None


_KEY_HINT_RE = re.compile(r"(?i)\b(?:enter|esc)\b")


def codex_status_line(line: str) -> bool:
    """D12 rule 4 (phase 8): ` · ` separated fields, at least two; the first (model and effort) without a
    standalone `enter`/`esc`; the second a directory: `/` not followed by a space, `~` or `~/…`. The
    working directory and the session title are free text and are not searched for key names."""
    fields = line.strip().split(" · ")
    if len(fields) < 2 or _KEY_HINT_RE.search(fields[0]):
        return False
    d = fields[1]
    return (d.startswith("/") and not d.startswith("/ ")) or d == "~" or d.startswith("~/")


# ---- read_only extra-argument whitelist (D8) ------------------------------------------


@dataclass(frozen=True)
class SafeOption:
    """An option allowed in ``extra_args`` under ``read_only``; its value must match ``value``."""

    option: str
    value: re.Pattern[str]


def _opt(option: str, value: str) -> SafeOption:
    return SafeOption(option, re.compile(value))


# ---- adapter -----------------------------------------------------------------------

GENERIC_FORBIDDEN_ENV_NAMES = frozenset(
    {"HOME", "PATH", "SHELL", "BASH_ENV", "ENV", "NODE_OPTIONS", "XDG_CONFIG_HOME",
     # Task 13.11: the program the CLI runs to edit a file (copilot's environment help; claude's editor
     # shortcut), and module resolution of the node-based CLIs (it changes the code they run).
     "EDITOR", "VISUAL", "NODE_PATH"}
)
GENERIC_FORBIDDEN_ENV_PREFIXES = ("LD_", "HERDR_")


@dataclass(frozen=True)
class KindAdapter:
    kind: str
    herdr_kind: str
    executable: str
    model_flag: str
    # Options the caller may not give in extra_args under either policy (D8 "库保留的启动选项"):
    # every option the library generates under full_auto, session-identity options and run-mode
    # options, with their aliases (checked against each CLI's --help).
    reserved_options: tuple[str, ...]
    read_only_args: tuple[str, ...]  # empty when read_only is not supported
    full_auto_args: tuple[str, ...]
    # Variadic list options appended last so that nothing after them is swallowed: (option, values).
    read_only_lists: tuple[tuple[str, tuple[str, ...]], ...]
    full_auto_lists: tuple[tuple[str, tuple[str, ...]], ...]
    session_id_flag: str | None
    capabilities: Mapping[str, Capability]
    config_dir_var: str
    default_config_dir: str  # relative to the passwd home
    config_env_prefixes: tuple[str, ...]
    config_env_names: tuple[str, ...]
    permission_env_vars: tuple[str, ...]  # unset in the pane under read_only (D6)
    read_only_safe_options: tuple[SafeOption, ...]
    repo_config_paths: tuple[str, ...]  # checked from cwd up to the repo root under read_only
    excludes_project_config: bool  # read_only args already exclude project-level config
    dialogs: tuple[Dialog, ...]
    prompt_guard: PromptGuard
    status_trusted: bool  # herdr's working/idle report can be relied on (D11)
    deny_key: str | None
    interrupt_key: str
    log_binding: str | None  # "session_id" | "rollout_fd" | None (D12)
    # Screen-capture footer (D12): line markers (the first matching line after the baseline ends the
    # content), or a bottom footer block, which replaces the markers when given.
    footer: tuple[re.Pattern[str], ...] = ()
    footer_block: FooterBlock | None = None
    # The visible screen of an agent ready for input. Where herdr's status is not reliable (D11) the
    # start-up dialog handling waits for it before taking `idle` as ready (task 13.20).
    ready_screen: re.Pattern[str] | None = None
    # Fixed, non-credential variables given to the pane's shell when its workspace is created (herdr
    # `--env`), and exported again by the preparation script before the caller's variables, so a caller
    # value of the same name wins (D6: claude's self-update, phase 8). They never make a start prepare.
    pane_env: Mapping[str, str] = field(default_factory=dict)
    secret_env_flag: str | None = None  # hides injected variable names from tools under read_only
    notes: tuple[str, ...] = field(default=())

    def capability(self, name: str) -> Capability:
        return self.capabilities[name]

    def native_args(
        self, policy: str, *, model: str | None = None, session_id: str | None = None, allow_extra: tuple[str, ...] = ()
    ) -> list[str]:
        """Base native arguments for ``policy`` (D8 table); ``allow_extra`` is appended to the read_only
        allow list (claude's ``mcp__…`` entries). Callers add MCP config, trust, secret-env and extra
        arguments (``policy.build_args``); this does not check capability states."""
        if policy == READ_ONLY:
            if not self.read_only_args:
                raise ValueError(f"{self.kind} has no read_only arguments")
            args, lists = list(self.read_only_args), self.read_only_lists
        elif policy == "full_auto":
            args, lists = list(self.full_auto_args), self.full_auto_lists
        else:
            raise ValueError(f"unknown policy {policy!r}")
        if model is not None:
            args += [self.model_flag, model]
        if session_id is not None and self.session_id_flag:
            args += [self.session_id_flag, session_id]
        if allow_extra and not any(o == ALLOW_LIST_OPTION for o, _ in lists):
            raise ValueError(f"{self.kind} has no allow list for {allow_extra}")
        for option, values in lists:
            args += [option, *values, *(allow_extra if option == ALLOW_LIST_OPTION else ())]
        return args


ALLOW_LIST_OPTION = "--allowedTools"

# claude tools that keep producing output after the turn's end marker (D8 "回合归属"); names seen
# in claude 2.1.322's tool lists (task 3.4 / phase-2 stub). Final list: task 13.2.
CLAUDE_ASYNC_TOOLS = (
    "Agent", "ListAgents", "Workflow", "CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "Monitor",
    "SendMessage", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskStop", "EnterWorktree", "ExitWorktree",
    "PushNotification",
    # Task 13.2 (claude 2.1.322, local stub): offered under full_auto; "a skill that runs in the background
    # returns only the agent's name — its result arrives later".
    "Skill",
)
# Forms that wait for the user (D8 "提问表单"), both policies. Task 13.2: the plan-mode pair is loadable through
# claude's deferred-tool search in both policies, and ExitPlanMode waits for the user's approval.
CLAUDE_FORM_TOOLS = ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode")
# DesignSync (task 13.2): reads and updates the user's claude.ai design projects over the network; loadable
# through the deferred-tool search unless denied.
CLAUDE_READ_ONLY_DENY = ("Edit", "Write", "NotebookEdit", "Bash", "WebFetch", "WebSearch", "DesignSync",
                         "ReportFindings", *CLAUDE_FORM_TOOLS)
CLAUDE_READ_ONLY_ALLOW = ("Read", "Grep", "Glob")
# copilot tools that start background work reported in later, prompt-less turns (task 13.1).
# `bash` with mode=async has the same effect but cannot be excluded on its own (phase-3 finding P3-1).
COPILOT_ASYNC_TOOLS = ("task", "read_agent", "write_agent", "list_agents", "run_dynamic_workflow", "dynamic_workflows_manage")
COPILOT_READ_ONLY_TOOLS = ("view", "grep", "glob")

ADAPTERS: dict[str, KindAdapter] = {
    "claude": KindAdapter(
        kind="claude",
        herdr_kind="claude",
        executable="claude",
        model_flag="--model",
        reserved_options=(
            "--model", "--session-id", "--dangerously-skip-permissions", "--disallowedTools", "--disallowed-tools",
            # session identity
            "--resume", "-r", "--continue", "-c", "--fork-session", "--from-pr", "--teleport", "--cloud",
            "--no-session-persistence",  # no session file: the log binding would fail
            # run mode
            "--print", "-p", "--bg", "--background", "--desktop",
            # move the session (worktree: the log path follows the cwd) or let others type into it (review N3)
            "--worktree", "-w", "--remote-control",
        ),
        read_only_args=(
            "--permission-mode", "dontAsk", "--restricted", "--disable-slash-commands", "--strict-mcp-config",
        ),
        full_auto_args=("--dangerously-skip-permissions",),
        read_only_lists=(
            # Task 13.33: only these built-in tools exist for the model; a tool a new CLI version adds is
            # not available by default. The deny list below stays as the second layer (D8).
            ("--tools", (",".join(CLAUDE_READ_ONLY_ALLOW),)),
            ("--allowedTools", CLAUDE_READ_ONLY_ALLOW),
            ("--disallowedTools", CLAUDE_READ_ONLY_DENY + CLAUDE_ASYNC_TOOLS),
        ),
        full_auto_lists=(("--disallowedTools", CLAUDE_FORM_TOOLS + CLAUDE_ASYNC_TOOLS),),
        session_id_flag="--session-id",
        capabilities={
            READ_ONLY: Capability(VERIFIED, note="needs an explicit non-official provider (--restricted drops settings login)"),
            MCP_ALLOW: Capability(VERIFIED, note="server definitions come from AgentSpec.mcp_config"),
            LOG_ADAPTER: Capability(VERIFIED),
            INTERRUPT_KEY: Capability(VERIFIED),
            SCREEN_CAPTURE: Capability(UNSUPPORTED, note="samples show the echo with the input line's prefix; no footer rule (D12)"),
            # Task 13.2 (local stub): the denied tools are neither offered nor found by the deferred-tool search.
            TURN_ATTRIBUTION_READ_ONLY: Capability(VERIFIED, note="async tools denied, also for the deferred-tool search"),
            TURN_ATTRIBUTION_FULL_AUTO: Capability(VERIFIED, note="async tools denied, also for the deferred-tool search"),
            IGNORE_USER_CONFIG: Capability(VERIFIED, note="--restricted"),
        },
        config_dir_var="CLAUDE_CONFIG_DIR",
        default_config_dir=".claude",
        # Task 13.11 (claude's environment-variable documentation): the whole CLAUDE_ prefix stays (it holds
        # restricted/safe mode, sandbox and permission switches), plus unprefixed names that change the
        # executable, the visible tools, where content is sent, or the installed code.
        config_env_prefixes=("CLAUDE_",),
        config_env_names=("USE_BUILTIN_RIPGREP", "ENABLE_TOOL_SEARCH", "ENABLE_CLAUDEAI_MCP_SERVERS",
                          "BETA_TRACING_ENDPOINT", "ENABLE_BETA_TRACING_DETAILED", "FORCE_AUTOUPDATE_PLUGINS"),
        permission_env_vars=(),
        pane_env={"DISABLE_AUTOUPDATER": "1"},  # claude ignores autoUpdates=false in its config (P8-5)
        read_only_safe_options=(_opt("--effort", r"low|medium|high|xhigh|max"),),
        repo_config_paths=(),
        excludes_project_config=True,
        dialogs=(
            _dialog("trust", TRUST, "Quick safety check: Is this a project you created or one you trust?",
                    "No, exit", "Yes, I trust this folder", keys=("down", "enter"), persisted=True),
            _dialog("api_key", PROVIDER_CREDENTIALS, "Detected a custom API key in your environment",
                    "Do you want to use this API key?", "Yes", "No (recommended)", keys=("up", "enter"), persisted=False),
            _dialog("bypass_permissions", FULL_AUTO_CONFIRM, "running in Bypass Permissions mode",
                    "No, exit", "Yes, I accept", keys=("down", "enter"), persisted=True),
        ),
        prompt_guard=PromptGuard(GUARDED_PREFIXES, {}),
        status_trusted=True,
        deny_key="esc",
        interrupt_key="esc",
        log_binding="session_id",
    ),
    "copilot": KindAdapter(
        kind="copilot",
        herdr_kind="copilot",
        executable="copilot",
        model_flag="--model",
        reserved_options=(
            "--model", "--session-id", "--yolo", "--no-ask-user", "--no-auto-update", "--excluded-tools",
            "--available-tools", "--resume", "-r", "--continue", "--prompt", "-p", "--interactive", "-i", "--acp",
        ),
        read_only_args=(
            "--deny-tool", "shell", "--deny-tool", "write", "--deny-tool", "memory", "--deny-tool", "url",
            "--disable-builtin-mcps", "--available-tools=" + ",".join(COPILOT_READ_ONLY_TOOLS),
            "--no-ask-user", "--no-auto-update",
        ),
        full_auto_args=(
            "--yolo", "--no-ask-user", "--no-auto-update", "--excluded-tools=" + ",".join(COPILOT_ASYNC_TOOLS),
        ),
        read_only_lists=(),
        full_auto_lists=(),
        session_id_flag="--session-id",
        capabilities={
            READ_ONLY: Capability(VERIFIED, note="tool allow-list view,grep,glob plus deny rules (task 13.1)"),
            MCP_ALLOW: Capability(UNVERIFIED, task="13.3", note="third-party MCP disabled by the account's org policy"),
            LOG_ADAPTER: Capability(VERIFIED),
            # Task 13.3 (copilot 1.0.112): ctrl+c ends the running turn (log: `abort`, then the checkpoint); Esc
            # does not. On an idle prompt the first ctrl+c only arms the exit confirmation, a second one quits,
            # so interrupt() sends it only while the log shows an open turn.
            INTERRUPT_KEY: Capability(VERIFIED, note="ctrl+c"),
            SCREEN_CAPTURE: Capability(UNSUPPORTED, note="samples show the echo with the input line's prefix; no footer rule (D12)"),
            TURN_ATTRIBUTION_READ_ONLY: Capability(VERIFIED, note="the tool allow-list has no background tool (task 13.1)"),
            TURN_ATTRIBUTION_FULL_AUTO: Capability(
                VERIFIED, task="13.10",
                note="sub-agent and workflow tools excluded (task 13.1); bash mode=async cannot be excluded and is a "
                     "known residual: its result arrives in a later notification turn (timing samples: task 13.10)",
            ),
            IGNORE_USER_CONFIG: Capability(UNSUPPORTED, note="no start-up option; user hooks are off only through the "
                                                             "disableAllHooks setting (task 13.5)"),
        },
        config_dir_var="COPILOT_HOME",
        default_config_dir=".copilot",
        config_env_prefixes=("COPILOT_",),
        # `copilot help environment`: the search programs, and (task 13.11) the GitHub host it authenticates
        # against. GH_TOKEN/GITHUB_TOKEN stay allowed: they are the ordinary way to give copilot a credential
        # (hidden from its tools under read_only, D8); phase-7 finding P7-6.
        config_env_names=("USE_BUILTIN_RIPGREP", "USE_TGREP", "GH_HOST"),
        permission_env_vars=("COPILOT_ALLOW_ALL", "COPILOT_ASSISTED_APPROVAL"),
        read_only_safe_options=(_opt("--reasoning-effort", r"none|minimal|low|medium|high|xhigh|max"),),
        repo_config_paths=(
            ".github/hooks", ".github/copilot/settings.json", ".mcp.json", ".vscode/mcp.json",
            ".claude/settings.json", ".claude/settings.local.json",
        ),
        excludes_project_config=False,
        dialogs=(
            _dialog("trust", TRUST, "Confirm folder trust", "Do you trust the files in this folder?", "1. Yes",
                    "2. Yes, and remember this folder", "3. No", keys=("enter",), persisted=False),
        ),
        prompt_guard=PromptGuard(GUARDED_PREFIXES, {}, max_bytes=COPILOT_PASTE_LIMIT),
        status_trusted=False,
        # Its input box: a `❯` line between two rules, all at column 0 (dialog boxes are drawn with `│`).
        ready_screen=re.compile(r"(?m)^─{20,}[ \t]*\n❯[^\n]*\n─{20,}[ \t]*$"),
        deny_key="esc",
        interrupt_key="ctrl+c",
        log_binding="session_id",
        secret_env_flag="--secret-env-vars",
    ),
    "codex": KindAdapter(
        kind="codex",
        herdr_kind="codex",
        executable="codex",
        model_flag="--model",
        # Resume / fork / exec are subcommands, not options; `-c` stays available (D8).
        # `-C`/`--cd` move the agent away from the checked cwd, `--remote` attaches to a remote server (review N3).
        reserved_options=("--model", "-m", "--no-daemon", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox",
                          "--cd", "-C", "--remote"),
        # Final web-search/feature switches: task 13.4 (read_only is unverified until then).
        read_only_args=(
            "--no-daemon", "--no-alt-screen", "--sandbox", "read-only", "--ask-for-approval", "never",
            "-c", "mcp_servers={}",
            # Task 13.5 (codex 0.167): no hook runs, whatever the user's hooks.json, inline [hooks] tables or
            # `[features] hooks = true` say. Other user settings still load (D8 trust boundary).
            "--disable", "hooks",
        ),
        full_auto_args=("--no-daemon", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox"),
        read_only_lists=(),
        full_auto_lists=(),
        session_id_flag=None,
        capabilities={
            READ_ONLY: Capability(UNVERIFIED, task="13.4", note="needs the OS sandbox (unprivileged user namespaces)"),
            MCP_ALLOW: Capability(UNSUPPORTED, note="not supported in v1"),
            LOG_ADAPTER: Capability(VERIFIED),
            INTERRUPT_KEY: Capability(VERIFIED),
            SCREEN_CAPTURE: Capability(VERIFIED),
            TURN_ATTRIBUTION_READ_ONLY: Capability(VERIFIED, note="no asynchronous tools"),
            TURN_ATTRIBUTION_FULL_AUTO: Capability(VERIFIED, note="no asynchronous tools"),
            IGNORE_USER_CONFIG: Capability(VERIFIED, note="--disable hooks: user hooks only (task 13.5)"),
        },
        config_dir_var="CODEX_HOME",
        default_config_dir=".codex",
        config_env_prefixes=("CODEX_",),
        config_env_names=(),
        permission_env_vars=(),
        read_only_safe_options=(_opt("-c", r"model_reasoning_effort=(minimal|low|medium|high|xhigh)"),),
        repo_config_paths=(".codex",),
        excludes_project_config=False,
        dialogs=(
            _dialog("trust", TRUST, "Trust this folder?", "Your trust decision will be saved",
                    "1. Trust and continue", keys=None, persisted=True),
        ),
        # Task 13.8 (codex 0.167): the guide line keeps `/`, `!`, `@`, `-`, `#`, `$` and `?` on its second line
        # part of one message to the model; nothing runs locally (tests/fixtures/screens/codex).
        prompt_guard=PromptGuard(GUARDED_PREFIXES, {}),
        status_trusted=True,
        deny_key=None,
        interrupt_key="esc",
        log_binding="rollout_fd",
        # codex shows submitted prompts in the transcript as `› <prompt>` too (phase-2 screen, phase-7
        # finding), and its selection dialogs mark the chosen option with `›`. D12 (tasks 13.31, 13.37): the
        # last line starting with `›`, not a numbered option, followed by exactly one non-blank line, the
        # status line, judged by its fields (`codex_status_line`): at least two ` · ` separated fields, the
        # first without a standalone `enter`/`esc`, the second a directory.
        footer_block=FooterBlock(re.compile(r"^›(?: |\s*$)"), max_after=1,
                                 min_after=1, input_exclude=re.compile(r"^›\s*\d+\."),
                                 status_line=codex_status_line),
    ),
}

assert tuple(sorted(ADAPTERS)) == tuple(sorted(KINDS))


def adapter(kind: str) -> KindAdapter:
    return ADAPTERS[kind]


# Configuration / permission variable families per kind (D8): used by the session-level
# `server_env_passthrough` check (D3) and by the read_only env checks (policy).
CONFIG_ENV_PREFIXES: dict[str, tuple[str, ...]] = {k: a.config_env_prefixes for k, a in ADAPTERS.items()}
CONFIG_ENV_NAMES: dict[str, tuple[str, ...]] = {k: a.config_env_names for k, a in ADAPTERS.items()}


def is_config_env(name: str) -> bool:
    """True if ``name`` belongs to any kind's configuration or permission variable family."""
    return any(name.startswith(p) for ps in CONFIG_ENV_PREFIXES.values() for p in ps) or any(
        name in ns for ns in CONFIG_ENV_NAMES.values()
    )
