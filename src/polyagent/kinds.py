"""Per-kind adapter table: native args, dialogs, prompt guards, log adapters (design D7).

Only the parts needed by the runtime layer exist so far; the full table is task 4.3.
"""

from __future__ import annotations

KINDS = ("codex", "copilot", "cursor", "claude")

# Configuration / permission variable families per kind (design D8). Used by the
# session-level `server_env_passthrough` check (D3) and, later, by read_only env checks.
CONFIG_ENV_PREFIXES: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_",),
    "copilot": ("COPILOT_",),
    "claude": ("CLAUDE_CODE_",),
    "cursor": ("CURSOR_",),
}
CONFIG_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "codex": (),
    "copilot": (),
    "claude": ("CLAUDE_CONFIG_DIR",),
    "cursor": (),
}


def is_config_env(name: str) -> bool:
    """True if ``name`` belongs to any kind's configuration or permission variable family."""
    return any(name.startswith(p) for ps in CONFIG_ENV_PREFIXES.values() for p in ps) or any(
        name in ns for ns in CONFIG_ENV_NAMES.values()
    )
