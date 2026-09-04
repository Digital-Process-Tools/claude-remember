"""Shared harness for #517's extracted-block tests.

Each of the 7 sites #517 fixes is a bash glob or parameter-expansion
pattern match that silently misses when its operand is backslash-separated
(the real shape REMEMBER_DIR/PROJECT_DIR arrive in on msys/cygwin, per
resolve-paths.sh's own _remember_normalize_win_path). Driving each site
through its full owning script end to end -- the way most of this repo's
hook tests do -- cannot reproduce that shape on a POSIX CI runner: the
normalizer that puts backslashes into PROJECT_DIR only fires for a
drive-letter-shaped input (`/c/...`, `C:/...`), and a macOS/Linux tmp_path
never looks like that, so forcing $OSTYPE=msys around a real subprocess
run leaves REMEMBER_DIR exactly as forward-slash as it always was there.

tests/test_autonomous_log_retention_487.py's own
_TestAutonomousLogHousekeepingGlobBackslash class solved the identical
problem for #487 by extracting the real fix's own code, verbatim, out of
its owning script and running it standalone in a bash subprocess with
REMEMBER_DIR handed a SYNTHETIC backslash-laden string directly -- no
resolve-paths.sh derivation chain involved, so no drive-letter-shaped
input is needed to reach it. This module generalizes that technique so
each of #517's other 6 sites (plus the shared _remember_forward_slash
helper itself) can reuse one extraction/run mechanism instead of 7 copies
of it.

`local OSTYPE=...` inside a wrapping function, NOT an environment variable
override -- #487's own test docstring for `_run_extracted_block` found
this is the one part of the technique that is NOT optional: a real
windows-latest runner's Git Bash resets $OSTYPE to its own compiled
default regardless of what the parent process's environment carries, so
`env["OSTYPE"] = ...` silently has no effect there, while a `local`
shadow inside a function is unaffected (confirmed on PR #499's own CI,
jobs 100892436094 and 100895310415).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def extract_lines(
    rel_path: str,
    start_marker: str,
    end_marker: str,
    *,
    after_marker: str | None = None,
) -> str:
    """The real lines from `rel_path`, from the first line containing
    `start_marker` through the first later line containing `end_marker`,
    inclusive -- the actual fix, not a paraphrase of it.

    `after_marker`, when given, is a line found first; the search for
    `start_marker` then begins strictly after it -- needed where the same
    literal text appears at more than one call site in the file (doctor.sh
    carries the identical storage-mode conditional twice, #517) and the
    first, textual, match is not the occurrence under test.
    """
    script = REPO_ROOT / rel_path
    lines = script.read_text().splitlines()
    search_from = 0
    if after_marker is not None:
        anchor = next(i for i, line in enumerate(lines) if after_marker in line)
        search_from = anchor + 1
    start = next(
        i for i, line in enumerate(lines)
        if start_marker in line and i >= search_from
    )
    end = next(
        i for i, line in enumerate(lines) if end_marker in line and i >= start
    )
    assert end >= start, (
        f"{rel_path}: end marker {end_marker!r} found before start marker "
        f"{start_marker!r} -- markers moved; fix this extraction"
    )
    return "\n".join(lines[start : end + 1])


def extract_function(rel_path: str, func_name: str) -> str:
    """The real body of a top-level `name() { ... }` bash function, found
    by its own opening line and the first `\\n}\\n` after it -- good enough
    for the flat (no nested function definitions) shape every function
    this module extracts actually has."""
    script = REPO_ROOT / rel_path
    text = script.read_text()
    marker = f"{func_name}() {{"
    start = text.index(marker)
    end = text.index("\n}\n", start) + len("\n}")
    return text[start:end]


def run_block(
    block: str,
    *,
    ostype: str,
    setup: str = "",
    after: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Runs `block` (real code extracted from the repo) inside a wrapping
    function that shadows $OSTYPE with a `local`, after `setup` (stub
    function definitions, variable assignments) has run in the SAME
    function scope -- so `setup` can freely set locals the block reads.

    `after`, appended in that same scope once `block` returns, is test-only
    observation code (an `echo` of a variable `block` left behind) -- never
    part of the claim under test itself. If `block` contains its own early
    `return` (lib-case-divergence.sh's own guards do), `after` is skipped
    exactly as it would be for a real caller, which is itself part of what
    a test of that guard needs to observe.
    """
    from _bash_runner import resolve_bash

    bash = resolve_bash()
    assert bash, "no usable bash found -- caller must skip before this runs"
    ostype_shadow = f"local OSTYPE={shlex.quote(ostype)}" if ostype else ":"
    script = f"""
set -u
_run_extracted_block() {{
    {ostype_shadow}
{setup}
{block}
{after}
}}
_run_extracted_block
"""
    import os

    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [bash, "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
