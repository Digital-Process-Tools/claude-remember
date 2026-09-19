"""#725 (F12): `./supertool` at the repository root was excluded only via the
local, untracked `.git/info/exclude` -- never via the tracked `.gitignore` --
so a fresh clone or a contributor's PR checkout carried no protection against
an ordinary `git add`/`git add -A` tracking a file at that path. Adding
`/supertool` to the tracked `.gitignore` closes that ordinary case; it is
NOT a complete guard on its own (`git add -f` still tracks an ignored path,
and `.gitignore` has no effect on a path already tracked), which is why the
jit-context rule layer was separately changed to never recommend or
describe running `./supertool` at all -- see `supertool-required.md` and
`00-README.md`. `00-README.md` previously claimed `./supertool` was
"gitignored on purpose", which was false until this fix: only this
repository's local, machine-scoped git config protected against it.

Must-fire (the vulnerable name is now covered) paired with a must-not-fire
control (an unrelated, legitimately-tracked file must not start matching
merely because a pattern was added), per this repo's own "pair every
must-not-fire with a must-fire" convention (CLAUDE.md).
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / ".oss"))

import statusline  # for _safe_which, see the docstring below


def _is_ignored(relative_path):
    """True when `git check-ignore` reports the path (any code path) is
    ignored by some rule reachable from the repo root -- the real
    authority for "is this path exempt from being tracked", not a string
    search over `.gitignore`'s own text.

    Resolves `git` through `statusline._safe_which` rather than handing
    `subprocess.run` a bare name (#725's own audit, applied to its own
    test): `cwd=REPO_ROOT` below is the checkout this test runs pytest
    inside, on a CI leg that also runs an unreviewed pull request's own
    tree -- the exact scenario a bare argv[0] loses to on Windows, which is
    the identical class this whole issue closes elsewhere in this diff.
    """
    resolved = statusline._safe_which("git")
    assert resolved is not None, "git must be resolvable on PATH to run this test"
    result = subprocess.run(
        [resolved, "check-ignore", "-q", relative_path],
        cwd=REPO_ROOT,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def test_root_supertool_is_gitignored_by_the_tracked_gitignore():
    """Must-fire: `/supertool` at the repository root must be covered by the
    TRACKED `.gitignore`, not merely by this one machine's
    `.git/info/exclude` -- so a fresh clone or a contributor's PR checkout
    is protected too, without depending on any local git config."""
    gitignore_text = (REPO_ROOT / ".gitignore").read_text()
    assert "supertool" in gitignore_text, (
        "the tracked .gitignore must name /supertool so an ordinary `git add`/"
        "`git add -A` in a PR branch will not silently track a file at that "
        "path (a `git add -f` still can -- .gitignore alone is not the guard "
        "against running whatever lands there; see supertool-required.md)"
    )
    assert _is_ignored("supertool"), (
        "git check-ignore must actually treat a root-level `supertool` path "
        "as ignored, not just contain a string that looks related"
    )


def test_unrelated_tracked_file_is_not_swept_up_by_the_new_pattern():
    """Must-not-fire pairing: a real, permanently-tracked file at the repo
    root (this repo's own CLAUDE.md) must still NOT be reported as
    git-ignored -- proving the new pattern is scoped to `supertool` and did
    not accidentally widen into something that hides real tracked content."""
    assert not _is_ignored("CLAUDE.md")
