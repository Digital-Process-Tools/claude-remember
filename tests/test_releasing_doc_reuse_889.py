"""#889 -- the reuse section of docs/releasing.md must name every script an
adopter actually needs to copy, and must disclose behavior that makes the
tooling unusable as-is for some adopters.

#856 made release-branch.yml's `publish` job call `check_version_order.py`,
and #858 made a missing/empty `hooks/hooks.json` a hard failure in both
`check_release_tree.py` and `smoke_release_tree.py` -- neither change touched
the reuse doc, so an adopter following it verbatim hits a "file not found" on
their second release, or cannot reuse the tooling at all if their plugin has
no hooks, with no warning either way.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "releasing.md"


def _reuse_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    marker = "## Reusing this in another plugin repository"
    assert marker in text, f"{DOC} has no {marker!r} section (#889)"
    start = text.index(marker)
    # Up to the next top-level heading, or end of file.
    rest = text[start + len(marker):]
    nxt = re.search(r"\n## ", rest)
    return rest[: nxt.start()] if nxt else rest


def _copy_list_line(section: str) -> str:
    # Step 1 may wrap onto several indented lines (a parenthetical aside, say) --
    # capture up to the next numbered step or the end of the section, not just
    # the first physical line.
    match = re.search(r"^1\. Copy .*?(?=\n\d+\. |\Z)", section, re.MULTILINE | re.DOTALL)
    assert match, "no numbered copy-list step 1 found in the reuse section (#889)"
    return match.group(0)


def test_reuse_copy_list_names_check_version_order():
    """#856: release-branch.yml's publish job calls check_version_order.py on
    every release after the first one with a `release` parent -- an adopter
    who copies only the three *_release_tree.py scripts gets a file-not-found
    failure the first time that applies to them."""
    line = _copy_list_line(_reuse_section())
    assert "check_version_order.py" in line, line


def test_reuse_copy_list_still_names_the_other_three_scripts():
    """Positive control: the existing three scripts must still be named --
    a rewrite that dropped them while adding check_version_order.py would
    pass the test above while breaking reuse a different way. The doc names
    them via a brace-expansion glob (`{build,check,smoke}_release_tree.py`),
    so check for each stem rather than each full filename."""
    line = _copy_list_line(_reuse_section())
    assert "_release_tree.py" in line, line
    for stem in ("build", "check", "smoke"):
        assert stem in line, line


def test_reuse_section_discloses_the_hooks_json_hard_failure():
    """#858: a missing hooks/hooks.json, or one declaring zero command hooks,
    is a hard failure in check_release_tree.py and smoke_release_tree.py, with
    no config flag to opt out (confirmed by reading both scripts). The reuse
    section names hooks.json already but never says this is unconditional --
    an adopter with no hooks of their own needs to know this before they
    start, not after a failed run."""
    section = _reuse_section()
    assert "hooks/hooks.json" in section
    mentions_failure = any(word in section for word in ("hard failure", "fails", "must"))
    assert mentions_failure, section
    assert re.search(r"zero.{0,40}hooks|no hooks of (its|your) own|requires? (a|at least one)",
                      section, re.IGNORECASE), section
