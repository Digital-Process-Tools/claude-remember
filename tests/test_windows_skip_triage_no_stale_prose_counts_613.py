"""docs/windows-skip-triage.md must not restate the table's own counts as
hand-maintained prose numbers (#613).

#595 added `tests/test_windows_skip_triage_prose_totals_595.py` as a reactive
backstop: it recomputed the table's own row/verdict counts and diffed them
against four numbers the prose separately stated (total x3, one each for
convertible/not-convertible/unclear). That backstop worked exactly as
designed and still drifted three times in quick succession (#595 -> #596
-> #611's follow-up commit), every time because a PR adding one new table row
was reviewed and merged green against its own stale base, before the sibling
PR carrying the up-to-date consistency test (or the up-to-date prose) had
landed. A same-PR CI leg cannot see a drift introduced by a *different*,
concurrently-developed PR's base -- the backstop only ever compares a PR's
own two halves, which are trivially self-consistent at the PR's own base.

#613's own issue names two ways to remove the class rather than reactively
catching the next instance of it: generate the prose from the table, or drop
the redundant prose numbers so there is nothing left to keep in sync. This
repo picks the second: a generator still needs *someone to run it* when a
table row changes, which is the exact step #596 and #611's precursor forgot
to take for the hand-written prose -- the discipline gap reproduces itself
one layer down. Deleting the numbers removes the sync requirement entirely:
the table is the only place a count lives, so no PR, however based, can ever
leave two disagreeing copies of it in the tree.

Per this repo's CLAUDE.md ("a negative assertion needs a positive control"),
the "must never restate a count" assertion is paired with a "must fire" case:
the exact patterns #595 used to detect a *mismatched* count are reused here
to detect a *reintroduced* count, and a reconstructed instance of #595's own
original prose sentence is asserted to still trip that detector, so this test
cannot pass merely because its patterns stopped matching anything at all.
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO_ROOT, "docs", "windows-skip-triage.md")

# The exact shapes #595 introduced to restate a table-derived count as prose.
# After #613, none of these may ever match live doc text again -- a match
# means a hand-maintained duplicate of the table's own count has crept back
# in, which is precisely the class #613 removes rather than re-guards.
_STALE_COUNT_PATTERNS = [
    re.compile(r"## The count: \d+"),
    re.compile(r"finds \*\*\d+\*\* modules on this tree"),
    re.compile(r"supply on a Windows runner via Git Bash\. \d+ modules\."),
    re.compile(r"path-format incompatibility\. \d+ modules\."),
    re.compile(r"not that read\. \d+ modules\."),
    re.compile(r"bulk of these \d+ reasons"),
]


def _read_doc() -> str:
    with open(DOC_PATH, encoding="utf-8") as fh:
        return fh.read()


def test_prose_no_longer_restates_a_hardcoded_table_count():
    doc_text = _read_doc()
    hits = [pattern.pattern for pattern in _STALE_COUNT_PATTERNS if pattern.search(doc_text)]
    assert not hits, (
        "docs/windows-skip-triage.md restates a table count as hand-maintained "
        "prose again -- #613 removed these sentences because they drifted three "
        "times (#595, #596, #611's follow-up) without anyone hand-editing them "
        "out of sync on purpose: " + "; ".join(hits)
    )


def test_positive_control_the_detector_still_catches_the_original_sentence():
    # MUST fire: proves the patterns above are not simply broken/never-matching
    # regexes that would make the assertion above trivially pass regardless of
    # what the doc says. Reconstruct one of #595's actual stale sentences
    # verbatim and confirm the detector still catches it.
    reintroduced = "## The count: 104, not 107 (and not the issue's original 92)\n"
    assert any(pattern.search(reintroduced) for pattern in _STALE_COUNT_PATTERNS), (
        "positive control failed to fire: a reconstructed instance of #595's "
        "own stale sentence should have tripped at least one detector pattern"
    )


def test_positive_control_convertible_not_convertible_unclear_sentences_too():
    # MUST fire: same as above but for the three verdict-count sentences
    # (convertible/not-convertible/unclear), which #595's own self-review
    # flagged as a separate gap from the total-count sentence alone -- the
    # same three shapes must still be caught here or the removal above could
    # silently regrow just those three.
    reintroduced = (
        "supply on a Windows runner via Git Bash. 8 modules.\n"
        "path-format incompatibility. 12 modules.\n"
        "not that read. 84 modules.\n"
    )
    matched_kinds = sum(1 for pattern in _STALE_COUNT_PATTERNS if pattern.search(reintroduced))
    assert matched_kinds == 3, (
        "positive control failed to fire: all three verdict-count sentences "
        f"should have been caught, only {matched_kinds} were"
    )
