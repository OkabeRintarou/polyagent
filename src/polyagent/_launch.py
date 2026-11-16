"""Agent start helpers (design D6, D9): pane environment script, start-dialog handling.

Used by ``PolyAgent.start``; kept apart from ``manager`` so that each part stays readable.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import tempfile
import time
from typing import Any

from . import herdr as H
from . import kinds as K
from .errors import AgentBlockedError, AgentTimeoutError, HerdrError, PolicyNotSupportedError
from .spec import AgentSpec, ClaudeProvider

log = logging.getLogger("polyagent")

# Shells whose execution of the preparation script was verified (D6 step 1). zsh and ksh failed task
# 13.17: zsh's `command` skips builtins (exports fail silently, `set +xv` never runs) and ksh93
# echoes the whole sourced script under `set -v` before it can switch tracing off.
POSIX_SHELLS = frozenset({"bash", "sh", "dash"})
PREP_TIMEOUT_MS = 15_000
SENTINEL_RE = "__pa_env_(ok|fail)_2"
REASON_RE = re.compile(r"__pa_reason_([a-z_]+)_2")
MAX_DIALOGS = 3
DIALOG_WAIT_MS = 15_000
DIALOG_GONE_WAIT = 15.0  # seconds for an answered dialog's text to leave the screen (D9)
DIALOG_POLL = 0.2
READY = ("idle", "done")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# read_only start-up self-checks (D6, D8) per kind: shell snippets that print a reason line and
# `return 1` from the sourced script. codex's are candidates until task 13.4 (its read_only is
# unverified, so they never run in practice yet).
SELF_CHECKS: dict[str, list[tuple[str, str]]] = {
    "codex": [
        ("user_rules", '\\command ls -A "${CODEX_HOME:-$HOME/.codex}/rules" 2>/dev/null | \\command grep -q .'),
        ("sandbox_unavailable", "! \\command codex sandbox true >/dev/null 2>&1"),
    ],
}
# Reasons that are failures of the policy's preconditions (PolicyNotSupportedError); any other reason
# (`provider_env`, `export_failed`), or none, is HerdrError(code="prep_failed") (D6 step 3).
POLICY_REASONS = frozenset({"user_rules", "sandbox_unavailable", "executable_shadowed", "permission_env"})
REASONS = {
    "provider_env": "inherited provider variables could not be cleared (read-only variables?)",
    "export_failed": "an exported variable does not have the given value (read-only variable?)",
    "executable_shadowed": "the agent's executable still resolves to an alias, a shell function or nothing "
                           "after they were removed",
    "permission_env": "a permission switch variable of the agent is still set after it was unset (read-only variable?)",
    "user_rules": "the user's codex rules directory contains rules, which can let commands run outside the sandbox",
    "sandbox_unavailable": "the host does not allow codex's sandbox (unprivileged user namespaces are required)",
}


def needs_prep(spec: AgentSpec, adapter: K.KindAdapter) -> bool:
    """D6: always under read_only; under full_auto only with variables, a provider or pre-start variables."""
    return spec.policy == "read_only" or bool(spec.env) or spec.provider is not None or bool(adapter.prestart_env)


def check_shell(client: H.HerdrClient, pane_id: str) -> None:
    info = client.pane_process_info(pane_id).get("process_info") or {}
    pid = info.get("shell_pid")
    name = next((p.get("name") for p in info.get("foreground_processes") or []
                 if isinstance(p, dict) and p.get("pid") == pid), None)
    if not isinstance(name, str) and isinstance(pid, int):
        try:
            with open(f"/proc/{pid}/comm") as f:
                name = f.read().strip()
        except OSError:
            name = None
    shell = os.path.basename(str(name or "")).lstrip("-")
    if shell not in POSIX_SHELLS:
        raise HerdrError(H.UNSUPPORTED_SHELL, f"the pane's shell {shell or '?'!r} is not one of the shells the "
                                              f"environment script is verified in ({', '.join(sorted(POSIX_SHELLS))})")


def _quote_names(names: list[str]) -> str:
    return " ".join(n for n in names if _NAME_RE.fullmatch(n))


def prep_script(spec: AgentSpec, adapter: K.KindAdapter) -> str:
    """The script sourced in the pane before `agent start` (D6 step 2). Values only appear here,
    in a 0600 file, never on a command line.

    The pane shell is interactive, so aliases and functions apply inside the sourced script: every
    command is written as ``\\command <name>`` (the backslash stops alias expansion, ``command``
    skips functions). Tracing is switched off first so that ``set -x``/``set -v`` cannot echo the
    exported values to the screen; after the exports the provider families are checked again (C4)."""
    c = "\\command"
    lines = ["# polyagent pane preparation", f"{{ {c} set +xv; }} 2>/dev/null"]
    ro = spec.policy == "read_only"
    if ro:
        exe = shlex.quote(adapter.executable)
        lines.append(f"{c} unalias {exe} 2>/dev/null; {c} unset -f {exe} 2>/dev/null")
        if adapter.permission_env_vars:
            lines.append(f"{c} unset {_quote_names(list(adapter.permission_env_vars))} 2>/dev/null")
    exports: dict[str, str] = dict(adapter.prestart_env)
    exports.update(spec.env)
    family = ""
    if spec.provider is not None:
        prefixes, names = ClaudeProvider.clear_rules()
        family = ("^(" + "|".join(re.escape(p) for p in prefixes) + ")|^(" + "|".join(re.escape(n) for n in names) + ")$")
        listing = (f"{c} env | {c} sed -n 's/^\\([A-Za-z_][A-Za-z0-9_]*\\)=.*/\\1/p' | "
                   f"{c} grep -E '{family}'")
        lines.append(f'for __pa_v in $({listing}); do {c} unset "$__pa_v" 2>/dev/null; done')
        exports.update(spec.provider.env())
    for name, value in exports.items():
        lines.append(f"{c} export {name}={shlex.quote(value)}")
    # Re-checks (D6): whatever made a step above fail silently, the script fails with a reason code.
    if spec.provider is not None:
        allowed = " ".join(n for n in exports if re.search(family, n))
        lines.append(f'for __pa_v in $({listing}); do case " {allowed} " in *" $__pa_v "*) ;; '
                     f'*) {c} echo __pa_reason_provider_env_$((1+1)); return 1 ;; esac; done')
        lines.append(f"{c} unset __pa_v")
    # Every export took effect with its value: a read-only variable keeps its old value although its
    # name is allowed (D6 re-check `export_failed`).
    for name, value in exports.items():
        lines.append(f'if {c} test "${{{name}+x}}" != x || {c} test "${name}" != {shlex.quote(value)}; then '
                     f"{c} echo __pa_reason_export_failed_$((1+1)); return 1; fi")
    if ro:
        lines.append(f'case "$({c} -v {exe} 2>/dev/null)" in /*) ;; '
                     f'*) {c} echo __pa_reason_executable_shadowed_$((1+1)); return 1 ;; esac')
        for var in adapter.permission_env_vars:
            if _NAME_RE.fullmatch(var):
                lines.append(f'if {c} test -n "${{{var}+x}}"; then {c} echo __pa_reason_permission_env_$((1+1)); '
                             "return 1; fi")
    if ro:
        for reason, cond in SELF_CHECKS.get(adapter.kind, []):
            # The reason line is built with arithmetic expansion, like the sentinels.
            lines.append(f"if {cond}; then {c} echo __pa_reason_{reason}_$((1+1)); return 1; fi")
    lines.append("return 0")
    return "\n".join(lines) + "\n"


def run_prep(client: H.HerdrClient, pane_id: str, spec: AgentSpec, adapter: K.KindAdapter) -> None:
    """D6 steps 1–4. Raises PolicyNotSupportedError for a failed self-check, AgentTimeoutError when the
    script does not finish in time, HerdrError otherwise (including variables that could not be cleared).
    The temporary file is removed by the pane on success and here in every case."""
    check_shell(client, pane_id)
    base = os.environ.get("XDG_RUNTIME_DIR")
    fd, path = tempfile.mkstemp(prefix="polyagent-", suffix=".sh", dir=base if base and os.path.isdir(base) else None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prep_script(spec, adapter))
        q = shlex.quote(path)
        client.pane_run(pane_id, f". {q} && rm -f {q} && echo __pa_env_ok_$((1+1)) || echo __pa_env_fail_$((1+1))")
        try:
            client.pane_wait_output(pane_id, PREP_TIMEOUT_MS, regex=SENTINEL_RE, source="recent")
        except HerdrError as e:
            if e.code == H.TIMEOUT:  # D16: a `pane wait-output` timeout at start is AgentTimeoutError (C9)
                raise AgentTimeoutError("the pane did not finish the environment script in time") from None
            raise
        screen = client.pane_read(pane_id, "recent", 60)
        if "__pa_env_ok_2" in screen and "__pa_env_fail_2" not in screen:
            return
        m = REASON_RE.search(screen)
        if m and m.group(1) in POLICY_REASONS:
            raise PolicyNotSupportedError(f"{adapter.kind} read_only cannot be enforced in this pane: "
                                          f"{REASONS[m.group(1)]}")
        why = REASONS.get(m.group(1), m.group(1)) if m else "the script failed"
        raise HerdrError("prep_failed", f"preparing the pane environment failed: {why}")
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _precondition(dialog: K.Dialog, spec: AgentSpec) -> bool:
    if dialog.keys is None:
        return False
    if dialog.category == K.TRUST:
        return spec.auto_trust
    if dialog.category == K.PROVIDER_CREDENTIALS:
        return spec.provider is not None and "ANTHROPIC_API_KEY" in spec.provider.env()
    if dialog.category == K.FULL_AUTO_CONFIRM:
        return spec.policy == "full_auto"
    return False


def match_dialog(adapter: K.KindAdapter, screen: str) -> K.Dialog | None:
    return next((d for d in adapter.dialogs if d.pattern.search(screen)), None)


def _wait_gone(client: H.HerdrClient, pane_id: str, dialog: K.Dialog) -> str | None:
    """Poll the screen until ``dialog`` is gone; the last screen if it is still there."""
    deadline = time.monotonic() + DIALOG_GONE_WAIT
    while True:
        screen = client.pane_read(pane_id, "visible")
        if not dialog.pattern.search(screen):
            return None
        if time.monotonic() >= deadline:
            return screen
        time.sleep(DIALOG_POLL)


def _wait_settled(client: H.HerdrClient, name: str) -> None:
    """`agent wait` without `--until` (idle, done or blocked, D9); its timeout is not an error here."""
    try:
        client.agent_wait(name, DIALOG_WAIT_MS)
    except HerdrError as e:
        if e.code != H.TIMEOUT:
            raise


def handle_dialogs(client: H.HerdrClient, name: str, pane_id: str, spec: AgentSpec, adapter: K.KindAdapter) -> None:
    """D9 step 6: whatever `agent start` reported, look at the screen; answer only known dialogs whose
    precondition holds, each one once, at most three. After an answer its text must leave the screen
    before anything else; seeing it again is AgentBlockedError. In the end the agent must be idle or
    done (AgentTimeoutError otherwise): having answered a dialog does not replace that check."""
    answered: set[str] = set()
    waited = False
    while True:
        screen = client.pane_read(pane_id, "visible")
        dialog = match_dialog(adapter, screen)
        if dialog is None:
            state = client.agent_get(name).get("agent_status")
            if state == "blocked":
                raise AgentBlockedError(f"agent {name} stopped at an unknown start-up dialog", screen, name)
            if state in READY:
                return
            if waited:
                raise AgentTimeoutError(f"agent {name} did not become ready", status=state)
            waited = True
            _wait_settled(client, name)
            continue
        if dialog.name in answered:
            raise AgentBlockedError(f"the {adapter.kind} {dialog.name} dialog of agent {name} came back after it "
                                    "was answered; it is not answered twice", screen, name)
        if len(answered) >= MAX_DIALOGS or not _precondition(dialog, spec):
            raise AgentBlockedError(f"agent {name} stopped at the {adapter.kind} {dialog.name} dialog, which "
                                    "polyagent may not answer here", screen, name)
        log.info("answering the %s %s dialog of agent %s", adapter.kind, dialog.name, name)
        client.agent_send_keys(name, *dialog.keys)  # type: ignore[misc]
        answered.add(dialog.name)
        still = _wait_gone(client, pane_id, dialog)
        if still is not None:
            raise AgentBlockedError(f"the {adapter.kind} {dialog.name} dialog of agent {name} did not go away "
                                    "after it was answered", still, name)
        _wait_settled(client, name)
        waited = True


def start_failed_cleanup(client: H.HerdrClient, workspace_id: str) -> None:
    try:
        client.workspace_close(workspace_id)
    except HerdrError as e:
        if e.code != H.WORKSPACE_NOT_FOUND:
            log.warning("closing workspace %s after a failed start failed: %s", workspace_id, e)


def agent_start(client: H.HerdrClient, name: str, kind: str, pane_id: str, timeout_ms: int,
                args: list[str]) -> dict[str, Any] | None:
    """`agent start`; returns the agent info, or None when herdr reported `agent_not_ready`."""
    try:
        return client.agent_start(name, kind, pane_id, timeout_ms, args)
    except HerdrError as e:
        if e.code == H.AGENT_NOT_READY:
            return None
        raise
