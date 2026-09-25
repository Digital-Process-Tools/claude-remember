"""Two follow-up defects in the shared injection guard (lib-memory-context.sh):

  - a directory BETWEEN the memory file and the repository root (most
    commonly `.remember` itself) committed as a symlink defeats both the
    per-file symlink check (it only ever sees the file already resolved,
    at the far end of the link) and the tracked-file check (`git
    ls-files` records a symlinked directory as one blob, with nothing
    "under" it ever listed).
  - the repository-root walk (_remember_repo_root_walk_into, driven from
    _remember_file_tracked_state_into) strips only `/`-delimited path
    components. On msys/cygwin, _remember_normalize_win_path hands it a
    backslash-separated directory, so the walk climbs at most one level
    (wherever the one literal `/` this codebase's own string
    concatenation still inserts happens to sit) and then stops --
    silently never reaching a `.git` further up.

Both are unit-level here, not the full session-start-hook integration
(tests/test_injection_guard_754_755_756.py is win32-skipped wholesale for
being a POSIX-subprocess test): this drives the guard's own shipped
functions directly under a real bash (tests/_bash_runner.py's
resolve_bash()), so both run on every platform in the CI matrix rather
than only where the hook itself is portable. The backslash case sets
OSTYPE=msys for the child bash process rather than requiring an actual
Windows host -- OSTYPE is an ordinary (non-readonly) bash variable, and
_remember_forward_slash_into's own gate reads nothing else, so setting it
is what actually exercises the msys/cygwin code path on any host.

Every case here is red before the fix and green after -- see the branch's
own report for the paired subprocess.run output.

DIAGNOSTIC (temporary, kept until the windows-latest leg is confirmed
green -- see PR #767 review): the shipped function's own `git -C ... `
call redirects git's stderr to /dev/null (matching production's own
#760-established "unavailable" behaviour, which must never leak a git
error message into injected session context), so a CI job log carries
only the opaque final state, never git's own reason for failing. `_probe`
below independently replicates the walk + pathspec computation ALONGSIDE
the real call (not instead of it -- the assertions still check the real,
unmodified `_remember_file_tracked_state_into` output), with git's stderr
and exit code captured rather than discarded, purely so a CI failure's
own output carries the exact string that a local darwin/linux run cannot
reproduce.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
BASH = resolve_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash on this host (#432)")

LIB = (REPO_ROOT / "scripts" / "lib-memory-context.sh").as_posix()
RESOLVE_PATHS = (REPO_ROOT / "scripts" / "resolve-paths.sh").as_posix()

# The one function this needs from resolve-paths.sh, lifted out exactly as
# tests/test_dirname_without_a_fork_660.py lifts _remember_root_of out of
# lib-memory-context.sh -- the real shipped code runs, not a copy of it.
#
# The trailing DIAG block is diagnostic-only (see module docstring): it
# does not feed back into $_state, which is still exactly what the real,
# unmodified _remember_file_tracked_state_into produced.
HARNESS = r'''
_body=$(sed -n '/^_remember_forward_slash_into()/,/^}/p' "@RESOLVE_PATHS@")
eval "$_body"
source "@LIB@"
_remember_file_tracked_state_into _state "$1"
printf 'STATE=%s\n' "$_state"

case "$1" in
    (*/*) _diag_dir="${1%/*}" ;;
    (*)   _diag_dir="." ;;
esac
_remember_forward_slash_into _diag_dir_fs "$_diag_dir"
_remember_repo_root_walk_into _diag_root "$_diag_dir_fs"
printf 'DIAG_OSTYPE=%s\n' "${OSTYPE:-<unset>}"
printf 'DIAG_BASH_VERSION=%s\n' "${BASH_VERSION:-<unset>}"
printf 'DIAG_INPUT=%s\n' "$1"
printf 'DIAG_DIR_FS=%s\n' "$_diag_dir_fs"
printf 'DIAG_ROOT=%s\n' "$_diag_root"
if [ -n "$_diag_root" ]; then
    printf 'DIAG_GIT_WHICH=%s\n' "$(command -v git 2>&1)"
    printf 'DIAG_GIT_VERSION=%s\n' "$(git --version 2>&1)"
    _diag_out=$(git -C "$_diag_root" ls-files 2>&1)
    _diag_rc=$?
    printf 'DIAG_GIT_RC=%s\n' "$_diag_rc"
    printf 'DIAG_GIT_OUT=%s\n' "$_diag_out"
    _diag_top=$(git -C "$_diag_root" rev-parse --show-toplevel 2>&1)
    printf 'DIAG_GIT_TOPLEVEL_RC=%s\n' "$?"
    printf 'DIAG_GIT_TOPLEVEL=%s\n' "$_diag_top"
fi
'''.replace("@RESOLVE_PATHS@", RESOLVE_PATHS).replace("@LIB@", LIB)


def _tracked_state(file_path: str, ostype: str | None = None) -> tuple[str, str]:
    """Returns (state, diagnostic_text). diagnostic_text is empty on a
    healthy 'tracked'/'not-tracked' state and always populated otherwise,
    for inclusion in an assertion failure message -- see module docstring."""
    env = None
    if ostype is not None:
        import os

        env = {**os.environ, "OSTYPE": ostype}
    done = subprocess.run(
        [BASH, "-c", HARNESS, "bash", file_path],
        capture_output=True, text=True, timeout=60, check=False, env=env,
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    state = ""
    diag_lines = []
    for line in lines:
        if line.startswith("STATE="):
            state = line[len("STATE="):]
        else:
            diag_lines.append(line)
    diag = "\n".join(diag_lines)
    return state, diag


class TestBackslashPathWalksPastTheProjectRoot:
    """_remember_repo_root_walk_into (driven from
    _remember_file_tracked_state_into) must climb a backslash-separated
    directory exactly as far as a forward-slash one -- not stop at the
    first literal `/` this codebase's own string concatenation happens to
    leave in the path."""

    def test_backslash_path_finds_git_two_levels_up(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        # proj sits a level BELOW the repository root -- the walk has to
        # climb past it to find .git, the same shape #754 fixed for the
        # forward-slash case.
        proj = repo / "pkg"
        remember_dir = proj / ".remember"
        remember_dir.mkdir(parents=True)
        now_md = remember_dir / "now.md"
        now_md.write_text("Working on the parser fix.\n")

        # Mirrors real construction: _remember_normalize_win_path
        # backslash-izes PROJECT_DIR wholesale, and REMEMBER_DIR/MFILE are
        # then built by plain "/"-concatenation on top of that
        # (lib-memory-dir.sh's `"${proj}/.remember"`, this file's own
        # `"$REMEMBER_DIR/now.md"`) -- so the real shape is backslash
        # throughout PROJECT_DIR, with exactly the two literal "/"s that
        # concatenation itself adds.
        proj_backslash = str(proj).replace("/", "\\")
        file_arg = f"{proj_backslash}/.remember/now.md"

        state, diag = _tracked_state(file_arg, ostype="msys")

        assert state == "not-tracked", (
            f"backslash-separated path did not climb past the project "
            f"root to find the repository two levels up -- got {state!r} "
            f"(expected 'not-tracked': a repository was found, and this "
            f"untracked file is genuinely not in it)\n--- diagnostic ---\n{diag}"
        )

    def test_forward_slash_path_still_finds_the_same_repo(self, tmp_path):
        """Positive control: the ordinary, never-backslashed path (no
        OSTYPE override at all) must keep finding the same repository --
        this fix must not become 'always forward-slash, even on POSIX',
        which happens to work here only because no path on this host ever
        legitimately contains a backslash."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        proj = repo / "pkg"
        remember_dir = proj / ".remember"
        remember_dir.mkdir(parents=True)
        now_md = remember_dir / "now.md"
        now_md.write_text("Working on the parser fix.\n")

        state, diag = _tracked_state(str(now_md))

        assert state == "not-tracked", (
            f"positive control did not reach 'not-tracked' -- got {state!r}\n"
            f"--- diagnostic ---\n{diag}"
        )
