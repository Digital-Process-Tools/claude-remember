"""#898 round 12: `printf -v VAR ''` does not assign VAR on bash 3.2.

Round 7 of #898 replaced two getters' indirect `${!name-}` reads with
parallel-array scans, and wrote their "no entry" answer as
`printf -v "$outvar" ''`. On bash 4+ that sets the variable to the empty
string. On bash 3.2 -- stock macOS `/bin/bash`, this repo's documented floor
-- an empty FORMAT produces no output and the variable is left untouched: a
caller under `set -u` then dies on "unbound variable" (macOS CI,
test_unknown_size_695.py), and a caller without `set -u` silently keeps
whatever the variable held before.

`printf -v VAR '%s' ''` assigns on every bash. Two layers here:

- a static guard over shipped shell, which runs on every platform (the
  Linux and Windows legs have no bash 3.2 to show the bug on), and
- the behaviour itself on a floor bash when this machine has one, with a
  positive control proving that interpreter really has the defect -- a
  "the variable is set" assertion on an interpreter without it proves
  nothing.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from shell_parse import KIND_BASH, discover_interpreters

LIB = REPO_ROOT / "scripts" / "lib-memory-context.sh"

# `printf -v NAME` followed by an empty format and nothing else on the command.
_EMPTY_FORMAT = re.compile(r"""printf\s+-v\s+\S+\s+(?:''|"")\s*(?:$|[;&|)#])""")


def _shipped_shell() -> list:
    out = []
    for sub in ("scripts", "hooks", "hooks.d"):
        root = REPO_ROOT / sub
        if root.is_dir():
            out.extend(p for p in root.rglob("*.sh") if p.is_file())
    return sorted(out)


def test_the_guard_pattern_matches_the_defect_shape():
    """Positive control for the static guard below."""
    assert _EMPTY_FORMAT.search("    printf -v \"$_outvar\" ''")
    assert _EMPTY_FORMAT.search('printf -v GOT ""; echo')
    assert not _EMPTY_FORMAT.search("    printf -v \"$_outvar\" '%s' ''")
    assert not _EMPTY_FORMAT.search(r"    printf -v dq '\042'")


def test_no_shipped_shell_assigns_through_an_empty_printf_format():
    files = _shipped_shell()
    assert files, "sanity: no shipped shell scripts found"
    hits = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _EMPTY_FORMAT.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{n}: {line.strip()}")
    assert not hits, (
        "printf -v with an empty format leaves the variable UNSET on bash 3.2; "
        "write printf -v VAR '%s' '' instead:\n" + "\n".join(hits)
    )


def _floor_bash():
    for interp in discover_interpreters():
        if interp.kind == KIND_BASH and interp.is_floor:
            return interp.path
    return None


def _run(bash: str, body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    script = (
        "set -u\n"
        f"export PIPELINE_DIR='{REPO_ROOT}'\n"
        f"export PROJECT_DIR='{tmp_path}'\n"
        f"source '{LIB}' >/dev/null 2>&1\n"
        f"{body}\n"
    )
    return subprocess.run([bash, "-c", script], check=False, capture_output=True,
                          text=True, timeout=30)


@pytest.mark.parametrize("call", [
    '_remember_wc_size_get_into GOT "/no/such/file"',
    '_remember_repo_root_walk_into GOT "/" || true',
])
def test_a_no_entry_answer_assigns_empty_on_the_floor_bash(call, tmp_path):
    bash = _floor_bash()
    if bash is None:
        pytest.skip("no bash below 4.0 on this machine -- the static guard above "
                    "covers this leg")
    # Positive control: this interpreter really leaves the variable unset
    # for an empty format, so the assertion below is about the fix.
    control = subprocess.run(
        [bash, "-c", "set -u; printf -v X ''; printf '[%s]' \"${X-UNSET}\""],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert control.stdout == "[UNSET]", (bash, control.stdout, control.stderr)

    r = _run(bash, f'{call}; printf "[%s]" "$GOT"', tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "[]", (call, r.stdout, r.stderr)
