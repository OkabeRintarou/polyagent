"""Guard: no hard-coded LLM credentials or real LLM endpoints in repository files.

Scans every git-tracked file plus untracked files that are not ignored. Provider configuration must
come from caller arguments or environment variables; samples use neutral placeholders such as
``EXAMPLE_ENDPOINT``, ``example.com`` or ``127.0.0.1``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]

SECRET_PATTERNS = [
    ("sk-style API key", re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{19,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
]

# `<NAME>=<url>` / `"<NAME>": "<url>"` / `base_url = "<url>"` where NAME looks like an LLM endpoint setting.
ENDPOINT_ASSIGNMENT = re.compile(
    r"""(?P<name>\b[A-Z][A-Z0-9_]*(?:BASE_URL|API_BASE|API_URL|ENDPOINT)\b|\bbase_url\b|\bapi_base\b)"""
    r"""["']?\s*[:=]\s*["']?(?P<url>https?://[^\s"'<>,}]+)""",
)

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
ALLOWED_SUFFIXES = (".example", ".invalid", ".test", ".localhost")


def _allowed_host(host: str) -> bool:
    host = host.lower().strip("[]")
    if host in ALLOWED_HOSTS or host.startswith("127."):
        return True
    if host in ("example.com", "example.org", "example.net") or host.endswith(
        (".example.com", ".example.org", ".example.net")
    ):
        return True
    return host.endswith(ALLOWED_SUFFIXES) or "EXAMPLE" in host.upper()


def find_violations(text: str) -> list[str]:
    """Return human-readable descriptions of hard-coded LLM credentials/endpoints in ``text``."""
    found = []
    for label, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            found.append(f"{label}: {m.group(0)[:8]}…")
    for m in ENDPOINT_ASSIGNMENT.finditer(text):
        host = urlsplit(m.group("url")).hostname or ""
        if not _allowed_host(host):
            found.append(f"endpoint {m.group('name')} -> {host}")
    return found


def repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True,
    ).stdout
    return [REPO / p for p in out.decode().split("\0") if p and (REPO / p).is_file()]


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
def test_repository_has_no_hardcoded_llm_credentials_or_endpoints():
    problems = {}
    for path in repo_files():
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue  # binary
        hits = find_violations(data.decode("utf-8", "replace"))
        if hits:
            problems[str(path.relative_to(REPO))] = hits
    assert problems == {}, f"hard-coded LLM credentials/endpoints found: {problems}"


def test_scanner_catches_violations():
    key = "sk-" + "ant-api03-" + "A1b2C3d4" * 4
    gateway = "https://" + "llm-gw" + ".acme-corp" + ".net/v1"
    for sample in (
        f'export ANTHROPIC_API_KEY="{key}"',
        f"ANTHROPIC_BASE_URL={gateway}",
        f'{{"env": {{"OPENAI_BASE_URL": "{gateway}"}}}}',
        f'[model_providers.x]\nbase_url = "{gateway}"',
        "Authorization: Bearer " + "x9Y8z7W6v5U4t3S2r1Q0p9O8n7M6",
    ):
        assert find_violations(sample), sample


def test_scanner_allows_placeholders():
    for sample in (
        "ANTHROPIC_BASE_URL=http://127.0.0.1:8080",
        'OPENAI_BASE_URL="https://api.example.com/v1"',
        "base_url = \"http://localhost:9/v1\"",
        "ANTHROPIC_BASE_URL=https://EXAMPLE_ENDPOINT/v1",
        "ANTHROPIC_API_KEY=sk-ant-...-redacted",
        "see https://docs.anthropic.com/en/docs for details",
        '"model_provider_id": "example_gateway"',
    ):
        assert find_violations(sample) == [], sample
