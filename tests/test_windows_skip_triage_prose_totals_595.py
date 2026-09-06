"""docs/windows-skip-triage.md's prose totals must track the live table, not a
frozen snapshot (#595).

`tests/test_windows_skip_triage_497.py` already guards the *table*: it recomputes
the live set of win32 blanket-skip modules via
`scripts.windows_skip_triage_497.find_blanket_skip_modules` and diffs it against
every row the table lists, so the table itself cannot silently drift. It says
nothing about the doc's own prose, though, which restates four of the table's
own counts in five sentences the table-diff test never looks at: the total
module count (three sentences), and the `unclear`, `convertible` and
`not-convertible` verdict counts (one sentence each). #589/#591 added two new
rows to the table (in sync, per that test) without touching the prose, and the
doc said "98" and "78 modules" for a while, while the live tree (and the table
itself) had already grown to 102 modules / 82 `unclear`.

Rather than hardcoding the current live numbers here (which would just move the
staleness one file over -- this test would go stale the next time a row is
added), this test ties the prose to the table's *own* row/verdict counts:
whatever the table says is the ground truth (that is what the #497 test
guards), and the prose is stale exactly when it disagrees with the table it is
standing next to. That also means this test does not need to recompute
anything from `tests/` itself -- it reads `docs/windows-skip-triage.md` once,
counts its own table rows and verdicts (via the same
`scripts.windows_skip_triage_497.parse_doc_table_rows` helper
`tests/test_windows_skip_triage_497.py` uses, so the two guards cannot
silently disagree about what counts as a row), and checks the five prose
sentences against that count.

This test's own first version (#595's initial commit) only covered the total
and `unclear` counts; a self-review reviewer flagged that the doc separately
states "8 modules" (convertible) and "12 modules" (not-convertible) right next
to the same table, uncovered by anything, and that this is the identical
staleness risk left half-fixed. The two missing patterns below close that.

Per this repo's CLAUDE.md ("a negative assertion needs a positive control"),
the real "prose matches table" case is paired with a "must fire" case: fixture
doc text with the table left alone but one prose number wrong is asserted to
fail the same comparison, so this test cannot pass merely because the
extraction regexes never match anything.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.windows_skip_triage_497 import parse_doc_table_rows

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO_ROOT, "docs", "windows-skip-triage.md")

# The five sentences in docs/windows-skip-triage.md that restate a count the
# table itself already carries. Each capture group is the digit run that must
# equal the live row count for that "kind" ("total" = every row, or one of
# the three verdict strings the table's own third column uses).
_PROSE_PATTERNS = [
    (re.compile(r"## The count: (\d+), not \d+"), "total"),
    (re.compile(r"finds \*\*(\d+)\*\* modules on this tree"), "total"),
    (re.compile(r"supply on a Windows runner via Git Bash\. (\d+) modules\."), "convertible"),
    (re.compile(r"path-format incompatibility\. (\d+) modules\."), "not-convertible"),
    (re.compile(r"not that read\. (\d+) modules\."), "unclear"),
    (re.compile(r"bulk of these (\d+) reasons"), "total"),
]


def _table_counts(doc_text: str) -> dict[str, int]:
    rows = parse_doc_table_rows(doc_text)
    counts: dict[str, int] = {"total": len(rows)}
    for _path, _reason, verdict in rows:
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _prose_mismatches(doc_text: str) -> list[str]:
    counts = _table_counts(doc_text)
    mismatches = []
    for pattern, kind in _PROSE_PATTERNS:
        match = pattern.search(doc_text)
        if match is None:
            mismatches.append(f"pattern {pattern.pattern!r} not found in doc")
            continue
        stated = int(match.group(1))
        expected = counts.get(kind, 0)
        if stated != expected:
            mismatches.append(
                f"pattern {pattern.pattern!r} states {stated}, but the table's "
                f"own {kind!r} row count is {expected}"
            )
    return mismatches


def test_prose_totals_match_the_live_table():
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    counts = _table_counts(doc_text)
    assert counts["total"] > 0, "sanity: the table must have at least one row"

    mismatches = _prose_mismatches(doc_text)
    assert not mismatches, (
        "docs/windows-skip-triage.md's prose totals disagree with its own "
        "table (#595): " + "; ".join(mismatches)
    )


def test_positive_control_fires_on_a_stale_prose_number():
    # MUST fire: a positive control for the assertion above. Without this, a
    # broken pattern (e.g. one that never matches) would make the real test
    # pass whether or not the prose actually agrees with the table.
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    counts = _table_counts(doc_text)
    wrong_total = counts["total"] + 1
    stale_doc = doc_text.replace(
        f"## The count: {counts['total']}, not 107",
        f"## The count: {wrong_total}, not 107",
    )
    assert stale_doc != doc_text, "fixture setup: replacement did not match live doc text"

    mismatches = _prose_mismatches(stale_doc)
    assert mismatches, (
        "positive control failed to fire: a deliberately wrong prose number "
        "should have produced a nonempty mismatch list"
    )


def test_positive_control_fires_on_a_stale_convertible_count():
    # MUST fire: same as above, but for the convertible pattern added after
    # self-review -- without this, a broken pattern for it would make the
    # real test pass whether or not the prose actually agrees with the table
    # on that count specifically.
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    counts = _table_counts(doc_text)
    wrong_convertible = counts.get("convertible", 0) + 1
    stale_doc = doc_text.replace(
        f"supply on a Windows runner via Git Bash. {counts.get('convertible', 0)} modules.",
        f"supply on a Windows runner via Git Bash. {wrong_convertible} modules.",
    )
    assert stale_doc != doc_text, "fixture setup: replacement did not match live doc text"

    mismatches = _prose_mismatches(stale_doc)
    assert mismatches, (
        "positive control failed to fire: a deliberately wrong convertible "
        "count should have produced a nonempty mismatch list"
    )


def test_positive_control_fires_on_a_stale_not_convertible_count():
    # MUST fire: same as above, for the not-convertible pattern. A second
    # audit round on #595 flagged that the first version of this file added
    # a dedicated fire-test for `convertible` but not for `not-convertible`
    # or `unclear` -- the identical gap the commit's own message claimed to
    # have closed for "both new counts." This closes it for not-convertible.
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    counts = _table_counts(doc_text)
    wrong_not_convertible = counts.get("not-convertible", 0) + 1
    stale_doc = doc_text.replace(
        f"path-format incompatibility. {counts.get('not-convertible', 0)} modules.",
        f"path-format incompatibility. {wrong_not_convertible} modules.",
    )
    assert stale_doc != doc_text, "fixture setup: replacement did not match live doc text"

    mismatches = _prose_mismatches(stale_doc)
    assert mismatches, (
        "positive control failed to fire: a deliberately wrong not-convertible "
        "count should have produced a nonempty mismatch list"
    )


def test_positive_control_fires_on_a_stale_unclear_count():
    # MUST fire: same as above, for the unclear pattern -- the fourth of the
    # four counts this guard checks, and the other one the second audit round
    # found had no dedicated fire-test of its own.
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    counts = _table_counts(doc_text)
    wrong_unclear = counts.get("unclear", 0) + 1
    stale_doc = doc_text.replace(
        f"not that read. {counts.get('unclear', 0)} modules.",
        f"not that read. {wrong_unclear} modules.",
    )
    assert stale_doc != doc_text, "fixture setup: replacement did not match live doc text"

    mismatches = _prose_mismatches(stale_doc)
    assert mismatches, (
        "positive control failed to fire: a deliberately wrong unclear "
        "count should have produced a nonempty mismatch list"
    )
