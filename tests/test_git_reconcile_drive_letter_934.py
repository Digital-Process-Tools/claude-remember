r"""_grc_common_dir's drive-letter guard never matches a backslash form (#934).

`hooks.d/after_save/60-git-reconcile.sh`'s `_grc_common_dir()` carries its own
copy of the "is this already absolute" guard that `50-git-backup.sh`'s
`_gb_common_dir()` and `hooks.d/before_session_start/50-git-restore.sh`'s
`_gr_common_dir()` also carry. The two siblings test a Windows drive-letter
path against the bracket expression ``[/\\]`` -- both a forward slash and a
backslash -- so ``C:/repo/.git`` and ``C:\repo\.git`` are both recognised as
already absolute. Reconcile's own copy carries ``[/\]`` instead (backslash
not doubled), which bash's glob-pattern bracket matching parses as an
unterminated class (the lone ``\`` escapes the following ``]`` rather than
closing the bracket) -- so the whole expression matches neither slash form,
and BOTH ``C:/repo/.git`` and ``C:\repo\.git`` fall through to being
misclassified as relative, not only the backslash form. Verified by
reverting only this guard back to ``[/\]`` and re-running this file's
`test_reconcile_guard_matches_siblings`: both the `forward-slash` and
`backslash` cases fail (only `relative-control` passes), confirming the
old guard was broken for both inputs rather than just the backslash one.

This drives the literal guard line straight out of the shipped hook file (so
the test cannot drift from what actually ships) and runs it standalone in a
real bash, rather than trying to coax a real `git rev-parse` into the
"legacy fallback" branch the issue says is effectively unreachable on tested
git versions. A positive control (an actually-relative path) is paired with
the two absolute-form checks so a broken harness that answers "relative" for
everything cannot pass by looking identical to a correctly-discriminating
guard. No blanket `skipif(win32)`: the `bash` fixture skips itself via
`resolve_bash()` when no real bash is available, which is the narrower route
(#432).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._bash_runner import decode_bash_output, resolve_bash, run_bash_file

REPO_ROOT = Path(__file__).resolve().parent.parent

RECONCILE = REPO_ROOT / "hooks.d" / "after_save" / "60-git-reconcile.sh"
BACKUP = REPO_ROOT / "hooks.d" / "after_save" / "50-git-backup.sh"
RESTORE = REPO_ROOT / "hooks.d" / "before_session_start" / "50-git-restore.sh"


def _guard_line(path: Path) -> str:
    """The one line in PATH that tests `_out` against the drive-letter form.

    Grepped out of the live file rather than copied by hand, so the test
    tracks whatever the shipped hook actually says instead of a snapshot of
    it taken when this test was written.
    """
    needle = "_out#[A-Za-z]:"
    for line in path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            return line.strip()
    raise AssertionError(f"drive-letter guard line not found in {path}")


def _classify(bash: str, guard_line: str, candidate: str) -> str:
    """Run GUARD_LINE standalone against CANDIDATE; "RELATIVE" or "ABSOLUTE".

    Mirrors the real function's branch: the guard's condition is true when
    neither strip attempt removed anything, i.e. the path is NOT already
    recognised as absolute and would be prefixed with the caller's directory.
    """
    script = (
        "_out=" + candidate + "\n"
        + guard_line + "\n"
        "    echo RELATIVE\n"
        "else\n"
        "    echo ABSOLUTE\n"
        "fi\n"
    )
    proc = run_bash_file(bash, script)
    assert proc.returncode == 0, decode_bash_output(proc.stderr)
    return decode_bash_output(proc.stdout).strip()


@pytest.fixture(scope="module")
def bash() -> str:
    found = resolve_bash()
    if not found:
        pytest.skip("no bash on PATH")
    return found


CASES = [
    ("'C:/repo/.git'", "ABSOLUTE"),
    (r"'C:\repo\.git'", "ABSOLUTE"),
    ("'.git'", "RELATIVE"),
]


@pytest.mark.parametrize("hook_path", [BACKUP, RESTORE], ids=["git-backup", "git-restore"])
@pytest.mark.parametrize("candidate,expected", CASES, ids=["forward-slash", "backslash", "relative-control"])
def test_sibling_guards_already_correct(bash, hook_path, candidate, expected):
    """Positive control: the two already-correct siblings classify as expected."""
    guard_line = _guard_line(hook_path)
    assert _classify(bash, guard_line, candidate) == expected


@pytest.mark.parametrize("candidate,expected", CASES, ids=["forward-slash", "backslash", "relative-control"])
def test_reconcile_guard_matches_siblings(bash, candidate, expected):
    """The bug: reconcile's own guard must classify identically to its siblings.

    Before the fix, the backslash candidate is misclassified as RELATIVE
    here while the siblings (above) correctly say ABSOLUTE -- this is the
    one assertion that would still pass if the code did nothing, which is
    why it is paired with the sibling test above rather than standing alone.
    """
    guard_line = _guard_line(RECONCILE)
    assert _classify(bash, guard_line, candidate) == expected
