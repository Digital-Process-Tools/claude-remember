"""
trap.d/804.edit-op-defaulted-to-main-clone-not-worktree.md leaked this
maintainer's actual local checkout paths (`/Users/floriandavid/...`) at two
lines describing a class of trap that has nothing to do with any one
maintainer's disk layout (#822). A fragment naming one person's home
directory reads as personal data rather than a portable lesson, and nothing
caught it because trap.d/ content has no automated review beyond the
curation pass reading prose.

This guards the whole directory, not just the one fragment: any trap.d/*.md
file containing a literal `/Users/<name>` (or other recognizable home-
directory absolute path) is a regression of the same class, not just of
#822's specific file.

Paired with a positive control (a fragment containing a generic placeholder
like `<worktree>` must NOT be flagged) so a check that flags everything, or
nothing, could not pass both (CLAUDE.md: a negative assertion needs a
positive control).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAP_DIR = REPO_ROOT / "trap.d"

# Matches an absolute POSIX home-directory path, e.g. /Users/floriandavid/foo
# or /home/someone/bar -- the shape a real maintainer checkout path takes.
HOME_PATH_RE = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")


def _offending_lines(text):
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if HOME_PATH_RE.search(line)
    ]


def test_no_trap_fragment_leaks_a_home_directory_path():
    assert TRAP_DIR.is_dir(), f"expected {TRAP_DIR} to exist"

    offenders = {}
    for path in sorted(TRAP_DIR.glob("*.md")):
        hits = _offending_lines(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits

    assert not offenders, (
        "trap.d fragment(s) leak a maintainer's local checkout path -- "
        "scrub to a generic placeholder like <worktree> / <main clone>: "
        f"{offenders}"
    )


def test_positive_control_a_real_leaked_path_is_detected():
    """A fragment shaped exactly like the pre-fix #804 file must be caught --
    otherwise the assertion above passes trivially because nothing is ever
    flagged, not because the real fragment is clean."""
    hits = _offending_lines(
        "Working issue #804 in worktree /Users/floriandavid/Documents/"
        "claude-remember-wt/804.\n"
    )
    assert hits, "the detector must flag a real leaked home-directory path"


def test_generic_placeholder_is_not_flagged():
    """A properly scrubbed fragment using placeholders must not be flagged,
    proving the detector doesn't just fire on every line."""
    hits = _offending_lines(
        "Working issue #804 in <worktree>. All four `edit` calls silently "
        "applied cleanly to <main clone>, not the worktree.\n"
    )
    assert not hits, "a generic placeholder must not be treated as a leak"
