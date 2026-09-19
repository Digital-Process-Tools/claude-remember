"""#725 (F12): `./supertool` at the repository root was excluded only via the
local, untracked `.git/info/exclude` -- never via the tracked `.gitignore` --
so a pull request could ship a TRACKED file literally named `supertool` at the
repo root, which `git checkout` writes over the ignored local entry-point
symlink on any clone, including a fresh one with no local exclude at all.
`tools/01-oss/00-README.md` claimed `./supertool` was "gitignored on
purpose", which was false until this fix: only this repository's local,
machine-scoped git config protected against it, and that protection does not
travel with a clone or a PR checkout.

Must-fire (the vulnerable name is now covered) paired with a must-not-fire
control (an unrelated, legitimately-tracked file must not start matching
merely because a pattern was added), per this repo's own "pair every
must-not-fire with a must-fire" convention (CLAUDE.md).
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_ignored(relative_path):
    """True when `git check-ignore` reports the path (any code path) is
    ignored by some rule reachable from the repo root -- the real
    authority for "is this path exempt from being tracked", not a string
    search over `.gitignore`'s own text."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", relative_path],
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
        "the tracked .gitignore must name /supertool so a PR cannot ship a "
        "tracked file at that path"
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
