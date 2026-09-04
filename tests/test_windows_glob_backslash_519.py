"""Regression tests for #519: 2 more Windows backslash-blindness sites that
survived #517's sweep -- bootstrap-dirs.sh's in-project `.gitignore` write
and 50-git-restore.sh's legacy-mode guard. Same class as #487/#517: a bash
`case`/parameter-expansion pattern match recognises only '/' as a path
separator, never a backslash, while REMEMBER_DIR/PROJECT_DIR arrive
backslash-separated on msys/cygwin (resolve-paths.sh's own
_remember_normalize_win_path).

Uses tests/_glob_backslash_517.py's shared extraction/run mechanism -- see
that module's own docstring for why the extracted-block technique is
needed at all (a real end-to-end run cannot reproduce the shape on a POSIX
CI runner).

Site 1 (bootstrap-dirs.sh) needs a REAL directory for its `-d` guard and
its actual `.gitignore` write to be observable at all -- a fully synthetic,
non-existent REMEMBER_DIR would make the write fail structurally (ENOENT)
regardless of whether the case pattern matched, indistinguishable from the
bug itself. The trick (same spirit as #487's own test, which keeps the
GLOB argument synthetic while the real files stay on real forward-slash
paths): only the single path component immediately after $_mem_proj is
given a literal backslash in its own name -- POSIX mkdir/open treat
backslash as an ordinary filename character, so
`tmp_path / "proj\\store"` is a perfectly legal, real, on-disk directory,
distinct from `tmp_path / "proj"`. `_mem_proj` never needs to exist as a
directory itself; it is only ever a string operand in the case pattern.
That reproduces exactly the shape a real Windows path has at this
boundary (a backslash where the pattern expects '/') while still letting
the surrounding `-d` guard and the real `.gitignore` write succeed for
real, so the test can assert the actual file lands on disk, not just that
some code path was reached.

Site 2 (50-git-restore.sh) is a pure string computation (REPO_ROOT/SLUG
via parameter-expansion split) with no filesystem interaction of its own,
so it is tested the same way lib-case-divergence.sh's own #517 split is:
a synthetic backslash-laden REMEMBER_DIR, no real directory needed.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from _glob_backslash_517 import extract_lines, run_block

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found (checked PATH, then Git-for-Windows install locations)",
)

REPO_ROOT_DIR = Path(__file__).resolve().parent.parent

# The shared helper itself, needed in `setup` for the bootstrap-dirs.sh
# site (which calls it directly) -- extracted the same way
# tests/test_windows_glob_backslash_517.py does, never retyped.
_FORWARD_SLASH_FN = (
    (REPO_ROOT_DIR / "scripts" / "resolve-paths.sh")
    .read_text()
    .split("_remember_forward_slash() {", 1)[1]
)
_FORWARD_SLASH_FN = "_remember_forward_slash() {" + _FORWARD_SLASH_FN.split("\n}\n", 1)[0] + "\n}"


class TestBootstrapDirsGitignoreWrite:
    """Site 1: bootstrap-dirs.sh:279-296 -- the in-project `.gitignore`
    write's `case "$REMEMBER_DIR" in "$_mem_proj"/*)` match."""

    _BLOCK = extract_lines(
        "scripts/bootstrap-dirs.sh",
        'if [ -d "$REMEMBER_DIR" ]; then',
        "unset _mem_proj",
        after_marker="Gitignore: only write when REMEMBER_DIR is inside the project tree",
    )

    def _run(self, remember_dir: str, mem_proj: str, ostype: str):
        setup = (
            _FORWARD_SLASH_FN
            + f"\nREMEMBER_DIR={shlex.quote(remember_dir)}"
            + f"\n_mem_proj={shlex.quote(mem_proj)}"
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup)

    def test_must_fire_gitignore_written_for_in_project_store_under_backslash_paths(
        self, tmp_path
    ):
        mem_proj = str(tmp_path / "proj")
        store_dir = tmp_path / "proj\\store"  # one real dir, literal backslash in its name
        store_dir.mkdir()
        remember_dir = str(store_dir)

        result = self._run(remember_dir, mem_proj, "msys")

        assert result.returncode == 0, result.stderr
        gitignore = store_dir / ".gitignore"
        assert gitignore.exists(), (
            "the in-project store's .gitignore must still be written when "
            "REMEMBER_DIR is backslash-separated at the $_mem_proj boundary "
            f"-- stderr={result.stderr}"
        )
        assert gitignore.read_text() == "*\n"

    def test_must_not_fire_control_external_store_gets_no_gitignore(self, tmp_path):
        """Positive control: a store genuinely OUTSIDE _mem_proj must still
        get no .gitignore, backslash-laden or not -- the fix must not
        start matching everything."""
        mem_proj = str(tmp_path / "proj")
        store_dir = tmp_path / "elsewhere\\store"
        store_dir.mkdir()
        remember_dir = str(store_dir)

        result = self._run(remember_dir, mem_proj, "msys")

        assert result.returncode == 0, result.stderr
        assert not (store_dir / ".gitignore").exists(), (
            "an external store must not get a .gitignore written just "
            f"because both paths happen to be backslash-laden -- stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_paths_still_work(self, tmp_path):
        """Positive control: an ordinary POSIX in-project REMEMBER_DIR is
        unaffected by the fix."""
        mem_proj = str(tmp_path / "proj")
        store_dir = tmp_path / "proj" / "store"
        store_dir.mkdir(parents=True)
        remember_dir = str(store_dir)

        result = self._run(remember_dir, mem_proj, "")

        assert result.returncode == 0, result.stderr
        assert (store_dir / ".gitignore").exists(), (
            f"an ordinary forward-slash in-project store must still get "
            f"its .gitignore -- stderr={result.stderr}"
        )


class TestGitRestoreLegacyGuardSplit:
    """Site 2: hooks.d/before_session_start/50-git-restore.sh's own
    REPO_ROOT/SLUG split (`${REMEMBER_DIR%/*}` / `${REMEMBER_DIR##*/}`).

    Unlike every other #517-class site, this one duplicates the
    `_remember_forward_slash` GATE inline (`case "${OSTYPE:-}" in
    msys|cygwin) ...`) rather than calling the shared function: this hook
    is exec'd by session-start-hook.sh's dispatch() as its own process
    (see scripts/log.sh's dispatch()), never sourced, so a function
    defined by resolve-paths.sh in the PARENT process is not in scope
    here, and the file deliberately never sources resolve-paths.sh itself
    to keep its own documented "cheap guards first" cost promise for the
    legacy-mode majority. This test therefore does NOT prepend
    `_FORWARD_SLASH_FN` to `setup` -- the extracted block is
    self-contained.
    """

    _BLOCK = extract_lines(
        "hooks.d/before_session_start/50-git-restore.sh",
        "# #519: normalize before the parameter-expansion split",
        "unset _gr_normalized_dir",
    )
    _AFTER = 'printf "REPO_ROOT=%s SLUG=%s" "$REPO_ROOT" "$SLUG"'

    def _run(self, remember_dir: str, ostype: str):
        setup = f"REMEMBER_DIR={shlex.quote(remember_dir)}"
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after=self._AFTER)

    def test_must_fire_split_finds_the_real_repo_root_under_backslash_paths(self):
        remember_dir = r"C:\Users\x\.claude\remember\proj-slug"
        result = self._run(remember_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "REPO_ROOT=C:/Users/x/.claude/remember SLUG=proj-slug", (
            "a backslash-separated REMEMBER_DIR must still split into the "
            f"real repo root and slug -- stdout={result.stdout!r} "
            f"stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_paths_still_split_correctly(self):
        """Positive control: an ordinary POSIX REMEMBER_DIR is unaffected
        -- this passes both before and after the fix, since `%/*`/`##*/`
        already handle a real '/' correctly on their own."""
        remember_dir = "/home/x/.claude/remember/proj-slug"
        result = self._run(remember_dir, "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "REPO_ROOT=/home/x/.claude/remember SLUG=proj-slug"
