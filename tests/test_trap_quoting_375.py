"""Regression tests for #375: bootstrap-dirs.sh built its EXIT trap by string
concatenation around a user-controlled path (the raw, non-slugified legacy
project directory), interpolated into a single-quoted span inside a string
that `trap` re-parses at exit. An apostrophe in the project path terminates
that span early and the remainder of the path is evaluated as shell source --
the benign symptom being the merged-config temp file surviving at exit
(reintroducing #362's leak for exactly this slice of users), the general one
being that a project directory name is an input to the shell's parser at
every hook exit.

`scripts/lib-memory-dir.sh` uses the identical string-built-trap idiom on
`$SYS_TMPDIR/remember-config-$$.json`, which is safe because no user names
that path. `bootstrap-dirs.sh` relocates the same file under
`$REMEMBER_DIR/tmp`, which in legacy mode IS the raw project path -- the
idiom was safe, the relocation was safe, the composition was not.

Deliberately does NOT construct a project-directory name that would execute
something if the defect were present -- an apostrophe is sufficient to pin
it, and a weaponised fixture name in this repo is a liability CI would run
on three platforms. We assert on cleanup behaviour (the file is gone after
the shell exits), never by proving something executed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Reuses the bash-discovery logic from the #362 regression suite rather than
# duplicating it. Deliberately does NOT reuse that module's
# `_source_bootstrap` for this file: that helper interpolates
# CLAUDE_PROJECT_DIR as literal text inside a double-quoted shell string, so
# a project path containing a backtick or a double quote breaks the
# harness's OWN setup script before the hook chain -- and therefore the trap
# -- is ever reached. This is a harness-quoting bug, not the #375 defect
# under test, and it is why every case below passes the project path through
# the subprocess `env` dict instead of splicing it into script text: an
# environment variable set via `env=` needs no shell quoting at all, because
# execve() never re-parses it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stale_config_sweep_362 import _BASH, _bash_path, _norm_sep

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS_SH = REPO_ROOT / "scripts" / "resolve-paths.sh"
BOOTSTRAP_DIRS_SH = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def _make_named_project(tmp_path, name: str) -> Path:
    """Same shape as test_stale_config_sweep_362._make_project, but with a
    caller-chosen, non-identifier project directory name -- the load-bearing
    difference. `proj` (used throughout #362's suite) is exactly why nothing
    caught this: every character in it is a shell-safe identifier byte, and
    the defect only reaches the parser when the path is not."""
    project = tmp_path / name
    (project / ".claude" / "remember").mkdir(parents=True)
    (project / ".remember").mkdir(parents=True)
    return project


def _remember_tmp(project) -> Path:
    return project / ".remember" / "tmp"


def _source_bootstrap_named(project, isolated_tmp, inner_commands: str = ""):
    """Like test_stale_config_sweep_362._source_bootstrap, but passes the
    project directory through the subprocess environment rather than
    splicing it into the script text -- see the module docstring above for
    why that distinction matters for THIS suite specifically."""
    script = f"""
set -e
export PIPELINE_DIR="{_bash_path(REPO_ROOT)}"
source "{_bash_path(RESOLVE_PATHS_SH)}"
export TMPDIR="{_bash_path(isolated_tmp)}"
source "{_bash_path(BOOTSTRAP_DIRS_SH)}"
{inner_commands}
"""
    env = {
        **os.environ,
        "HOME": _bash_path(project.parent),
        "CLAUDE_PROJECT_DIR": _bash_path(project),
    }
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# A double quote is illegal in a Windows filename outright (the forbidden
# set is: less-than, greater-than, colon, double-quote, slash, backslash,
# pipe, question-mark, asterisk), so a fixture using it cannot exist there --
# skip loudly per-case rather than silently narrowing what every platform is
# checked against. Apostrophe, dollar-sign and backtick are all legal
# project-directory bytes on every platform this runs on (Windows, macOS,
# Linux) and are exercised everywhere.
_CASES = [
    pytest.param("proj-no-metachar", id="positive-control-no-metachar"),
    pytest.param("Bob's Project", id="apostrophe"),
    pytest.param("Weird $ Project", id="dollar-sign"),
    pytest.param("Weird ` Project", id="backtick"),
    pytest.param(
        'Weird " Project',
        id="double-quote",
        marks=pytest.mark.skipif(
            sys.platform == "win32",
            reason="a double quote is not a legal character in a Windows filename",
        ),
    ),
]


@pytest.mark.parametrize("case", _CASES)
def test_exit_trap_cleans_up_relocated_config_regardless_of_path_metacharacters(
    tmp_path, case
):
    """The core assertion, parametrized over the positive control (a plain
    identifier project name, which must ALSO clean up -- otherwise "the file
    is gone" would pass for the wrong reason, e.g. a broken harness) and
    several path-hostile characters legal on all three platforms this suite
    runs on.

    Runs the real hook chain (resolve-paths.sh -> bootstrap-dirs.sh, which
    sources lib-memory-dir.sh) as a subprocess so the shell's own EXIT trap
    genuinely fires, exactly as it does for a real hook invocation. Before
    the fix, only the apostrophe case actually leaves the relocated
    remember-config-$$.json behind -- the vulnerable trap wraps the path in
    a SINGLE-quoted span, and `$`, a backtick and a double quote are all
    inert inside single quotes, so those three cases pass before the fix
    too. They stay in this matrix as regression coverage for the fix
    itself (confirming `printf %q` round-trips them as well as the
    apostrophe it was written for), not as pre-fix reproductions. The
    plain-identifier control passes either way, which is what makes it a
    control rather than a duplicate."""
    project = _make_named_project(tmp_path, case)
    isolated_tmp = tmp_path / "systmp"
    isolated_tmp.mkdir()

    result = _source_bootstrap_named(
        project,
        isolated_tmp,
        'echo "MERGED=$REMEMBER_CONFIG"',
    )
    assert result.returncode == 0, (
        f"hook chain exited non-zero for project name {case!r}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    merged_line = [l for l in result.stdout.splitlines() if l.startswith("MERGED=")]
    assert merged_line, (
        f"no MERGED= line in stdout for project name {case!r}: "
        f"{result.stdout!r}"
    )
    merged_path_str = merged_line[0][len("MERGED="):]

    remember_tmp = _remember_tmp(project)
    assert _norm_sep(str(remember_tmp)) in _norm_sep(merged_path_str), (
        f"expected the merged config under {remember_tmp} for project name "
        f"{case!r}, got {merged_path_str!r} -- setup broke before the trap "
        "was ever exercised"
    )

    merged_path = Path(merged_path_str)
    assert not merged_path.exists(), (
        f"the relocated merged-config file survived shell exit for project "
        f"name {case!r} -- this is #375: a metacharacter in the (legacy, "
        "unslugified) project path terminated the trap's single-quoted "
        "span early, so the shell's own EXIT trap never removed the file "
        "it names"
    )
