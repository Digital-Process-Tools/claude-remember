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
    write's `case "$REMEMBER_DIR" in "$_mem_proj"/*)` match.

    The real store directory this class creates has to be built
    differently per HOST platform (CI job 100931242415/100931242372,
    windows-latest 3.9/3.12, self-review-round-2 finding): on a POSIX host
    (macOS/Linux CI, local dev), backslash is an ordinary filename
    character, never a separator, so faking a Windows-native boundary
    needs one real directory whose own on-disk NAME literally contains a
    backslash (`tmp_path / "proj\\store"`, ONE component, no
    parents=True). On a native-Windows host (windows-latest), the
    OPPOSITE is true: backslash IS the real separator, both for Python's
    own `Path.mkdir()` and for Git Bash's own MSYS-translated file I/O --
    `tmp_path / "proj\\store"` there is TWO components, and `mkdir()`
    (no `parents=True`) fails with `WinError 3: The system cannot find
    the path specified` because "proj" was never created, which is
    exactly what CI hit. On that host an ORDINARY nested `mkdir` (two
    real components, `parents=True`) already produces exactly the fully
    backslash-separated path this test needs, with no special
    construction at all -- `str(store_dir)` on native Windows is already
    all-backslash by construction.
    """

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

    @staticmethod
    def _make_store_dir(tmp_path, container_name: str, leaf_name: str):
        """Real on-disk directory named <container_name>BOUNDARY<leaf_name>
        under tmp_path, where BOUNDARY is a literal backslash -- see the
        class docstring for why its construction has to differ by host
        platform. `container_name` never needs to exist as its own
        directory; only the composed store_dir is ever `-d`/write tested."""
        if os.name == "nt":
            store_dir = tmp_path / container_name / leaf_name
            store_dir.mkdir(parents=True)
        else:
            store_dir = tmp_path / f"{container_name}\\{leaf_name}"
            store_dir.mkdir()
        return store_dir

    def test_must_fire_gitignore_written_for_in_project_store_under_backslash_paths(
        self, tmp_path
    ):
        mem_proj = str(tmp_path / "proj")
        store_dir = self._make_store_dir(tmp_path, "proj", "store")
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
        store_dir = self._make_store_dir(tmp_path, "elsewhere", "store")
        remember_dir = str(store_dir)

        result = self._run(remember_dir, mem_proj, "msys")

        assert result.returncode == 0, result.stderr
        assert not (store_dir / ".gitignore").exists(), (
            "an external store must not get a .gitignore written just "
            f"because both paths happen to be backslash-laden -- stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_paths_still_work(self, tmp_path):
        """Positive control: an ordinary POSIX in-project REMEMBER_DIR is
        unaffected by the fix. The real directory is created with an
        ORDINARY nested mkdir (portable everywhere), and both strings
        handed to bash are forced to forward slashes -- a no-op on POSIX,
        and on native Windows a spelling Git Bash's own MSYS runtime
        accepts equally well as the native backslash form, so the real
        directory this creates is still reachable through it."""
        mem_proj_dir = tmp_path / "proj"
        store_dir = mem_proj_dir / "store"
        store_dir.mkdir(parents=True)
        mem_proj = str(mem_proj_dir).replace("\\", "/")
        remember_dir = str(store_dir).replace("\\", "/")

        result = self._run(remember_dir, mem_proj, "")

        assert result.returncode == 0, result.stderr
        assert (store_dir / ".gitignore").exists(), (
            f"an ordinary forward-slash in-project store must still get "
            f"its .gitignore -- stderr={result.stderr}"
        )


class TestGitRestoreLegacyGuardSplit:
    """Site 2: hooks.d/before_session_start/50-git-restore.sh's own
    REPO_ROOT/SLUG split (`${REMEMBER_DIR%/*}` / `${REMEMBER_DIR##*/}`)
    PLUS the legacy-mode short-circuit that immediately follows it
    (`[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit 0`) -- both included in one
    extraction, because a self-review finding (oss:auditor) caught that
    normalizing only REMEMBER_DIR and comparing the result against a still
    -backslash PROJECT_DIR breaks that exact guard for a genuine legacy
    install: _remember_normalize_win_path (scripts/resolve-paths.sh)
    rewrites PROJECT_DIR to BACKSLASH form on msys/cygwin, the opposite
    direction, so PROJECT_DIR needs normalizing too before the comparison
    -- the fix now does this, and TestLegacyModeShortCircuit below is the
    regression test for it.

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
        "unset _gr_normalized_project",
    )
    _AFTER = 'printf "REPO_ROOT=%s SLUG=%s" "$REPO_ROOT" "$SLUG"'

    def _run(self, remember_dir: str, project_dir: str, ostype: str):
        setup = (
            f"REMEMBER_DIR={shlex.quote(remember_dir)}"
            + f"\nPROJECT_DIR={shlex.quote(project_dir)}"
        )
        return run_block(self._BLOCK, ostype=ostype, setup=setup, after=self._AFTER)

    def test_must_fire_split_finds_the_real_repo_root_under_backslash_paths(self):
        remember_dir = r"C:\Users\x\.claude\remember\proj-slug"
        project_dir = r"C:\Users\x\somewhere-else"  # NOT the store's parent
        result = self._run(remember_dir, project_dir, "msys")

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
        project_dir = "/home/x/somewhere-else"
        result = self._run(remember_dir, project_dir, "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "REPO_ROOT=/home/x/.claude/remember SLUG=proj-slug"


class TestLegacyModeShortCircuit:
    """Regression test for the oss:auditor self-review finding on this
    diff: the legacy-mode guard (`[ "$REPO_ROOT" = "$PROJECT_DIR" ] && exit
    0`) must still fire -- cheaply, without ever reaching `git -C` -- for a
    genuine legacy-mode install (REMEMBER_DIR is PROJECT_DIR + "/.remember")
    even when both arrive backslash-separated on msys/cygwin. Reuses
    TestGitRestoreLegacyGuardSplit's own block/`_run` (same class, not
    duplicated) via direct construction.
    """

    def _run(self, remember_dir: str, project_dir: str, ostype: str):
        return TestGitRestoreLegacyGuardSplit()._run(remember_dir, project_dir, ostype)

    def test_must_fire_legacy_install_short_circuits_under_backslash_paths(self):
        """The bug this guards against: PROJECT_DIR is normalized in the
        OPPOSITE direction from REMEMBER_DIR by resolve-paths.sh on
        msys/cygwin (backslash, not forward-slash) -- comparing a
        forward-slashed REPO_ROOT against a still-backslash PROJECT_DIR
        would never match here, and the hook would fall through past its
        own cheap-guards-first short-circuit into sourcing log.sh and
        parsing config for every legacy-mode Windows install."""
        project_dir = r"C:\Users\x\proj"
        remember_dir = project_dir + r"\.remember"
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", (
            "a genuine legacy-mode install (REMEMBER_DIR is PROJECT_DIR's "
            "own .remember subdir) must still short-circuit via `exit 0` "
            "-- reaching git -C at all here means the guard silently "
            f"stopped firing -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_not_fire_control_external_install_does_not_short_circuit(self):
        """Positive control: a genuinely external store (REPO_ROOT is NOT
        PROJECT_DIR) must NOT be caught by this guard, backslash-laden or
        not -- the fix must not start matching everything."""
        project_dir = r"C:\Users\x\proj"
        remember_dir = r"C:\Users\x\.claude\remember\proj-slug"
        result = self._run(remember_dir, project_dir, "msys")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "REPO_ROOT=C:/Users/x/.claude/remember SLUG=proj-slug", (
            "an external store must still fall through past the legacy "
            f"guard -- stdout={result.stdout!r} stderr={result.stderr}"
        )

    def test_must_fire_forward_slash_legacy_install_still_short_circuits(self):
        """Positive control: an ordinary POSIX legacy install is
        unaffected -- this already passed before the fix too, since
        neither side needed normalizing there."""
        project_dir = "/home/x/proj"
        remember_dir = project_dir + "/.remember"
        result = self._run(remember_dir, project_dir, "")

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
