"""Recording and guarding ``herdr`` shim for integration tests (design D17, review T1).

The shim is placed first on ``PATH`` and also passed as ``herdr_path``. It records every
invocation and refuses (without running the real herdr) any call that is not strictly scoped to
the temporary session:

* the first two arguments must be ``--session <temp>`` (only a lone ``--version`` is exempt);
* no further ``--session`` / ``--session=...`` before ``--``;
* ``session`` subcommands: only ``list``, or ``stop``/``delete`` whose target is ``<temp>``;
* no inherited ``HERDR_*`` variable other than ``HERDR_CONFIG_PATH`` in the environment.

Records are ``\\x1f``-separated arguments terminated by ``\\x1e`` so multi-line arguments survive.
"""

from __future__ import annotations

import shlex
from pathlib import Path

ARG_SEP = "\x1f"
REC_SEP = "\x1e"

_TEMPLATE = r"""#!/bin/bash
LOG=__LOG__
REFUSED=__REFUSED__
NAME=__NAME__
REAL=__REAL__
rec=$(printf '%s\037' "$@")
printf '%s\036' "$rec" >> "$LOG"
refuse() {
  printf '%s\037%s\036' "$1" "$rec" >> "$REFUSED"
  printf '{"error":{"code":"refused_by_test_shim","message":"%s"}}\n' "$1" >&2
  exit 1
}
for v in $(compgen -e); do
  case "$v" in HERDR_CONFIG_PATH) ;; HERDR_*) refuse "inherited $v" ;; esac
done
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then exec "$REAL" --version; fi
if [ "$1" != "--session" ] || [ "$2" != "$NAME" ]; then refuse "not scoped to the temp session"; fi
args=("${@:3}")
for a in "${args[@]}"; do
  [ "$a" = "--" ] && break
  case "$a" in --session|--session=*) refuse "repeated --session" ;; esac
done
if [ "${args[0]}" = "session" ]; then
  case "${args[1]}" in
    list) ;;
    stop|delete)
      [ "${args[2]}" = "$NAME" ] || refuse "session ${args[1]} target is not the temp session"
      [ "${#args[@]}" -le 4 ] || refuse "unexpected session arguments"
      if [ "${#args[@]}" -eq 4 ] && [ "${args[3]}" != "--json" ]; then refuse "unexpected session arguments"; fi
      ;;
    *) refuse "session subcommand ${args[1]}" ;;
  esac
fi
exec "$REAL" "$@"
"""


def build_shim(directory: Path, real: str, name: str, log: Path, refused: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "herdr"
    body = (
        _TEMPLATE.replace("__LOG__", shlex.quote(str(log)))
        .replace("__REFUSED__", shlex.quote(str(refused)))
        .replace("__NAME__", shlex.quote(name))
        .replace("__REAL__", shlex.quote(real))
    )
    shim.write_text(body)
    shim.chmod(0o755)
    return shim


def read_records(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [rec.split(ARG_SEP)[:-1] for rec in path.read_text().split(REC_SEP) if rec]


def is_scoped(argv: list[str], name: str) -> bool:
    """Python mirror of the shim's rule, used to audit the recorded calls."""
    if argv == ["--version"]:
        return True
    if argv[:2] != ["--session", name]:
        return False
    args = argv[2:]
    for a in args:
        if a == "--":
            break
        if a == "--session" or a.startswith("--session="):
            return False
    if args[:1] == ["session"]:
        sub = args[1] if len(args) > 1 else None
        if sub == "list":
            return True
        if sub in ("stop", "delete"):
            return len(args) >= 3 and args[2] == name and args[3:] in ([], ["--json"])
        return False
    return True
