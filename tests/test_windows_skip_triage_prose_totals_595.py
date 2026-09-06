"""docs/windows-skip-triage.md's prose totals must track the live table, not a
frozen snapshot (#595).

`tests/test_windows_skip_triage_497.py` already guards the *table*: it recomputes
the live set of win32 blanket-skip modules via
`scripts.windows_skip_triage_497.find_blanket_skip_modules` and diffs it against
every row the table lists, so the table itself cannot silently drift. It says
nothing about the doc's own prose, though, which restates the same two counts --
the total module count and the `unclear` verdict count -- in four sentences the
table-diff test never looks at. #589/#591 added two new rows to the table (in
sync, per that test) without touching those four sentences, and the doc has
said "98" and "78 modules" ever since, while the live tree (and the table
itself) now has 102 modules and 82 of them `unclear`.

Rather than hardcoding the current live numbers here (which would just move the
staleness one file over -- this test would go stale the next time a row is
added), this test ties the prose to the table's *own* row count: whatever the
table says is the ground truth (that is what the #497 test guards), and the
prose is stale exactly when it disagrees with the table it is standing next to.
That also means this test does not need to recompute anything from `tests/`
itself -- it reads `docs/windows-skip-triage.md` once, counts its own table
rows and verdicts, and checks the four prose sentences against that count.

Per this repo's CLAUDE.md ("a negative assertion needs a positive control"),
the real "prose matches table" case is paired with a "must fire" case: fixture
doc text with the table left alone but one prose number wrong is asserted to
fail the same comparison, so this test cannot pass merely because the
extraction regexes never match anything.
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO_ROOT, "docs", "windows-skip-triage.md")

_ROW_PATH_RE = re.compile(r"^\|\s*`(tests/[^`]+\.py)`\s*\|([^|]*)\|([^|]*)\|", re.MULTILINE)

# The four sentences in docs/windows-skip-triage.md that restate a count the
# table itself already carries. Each capture group is the digit run that must
# equal either the live total row count ("total") or the live `unclear` row
# count ("unclear").
_PROSE_PATTERNS = [
    (re.compile(r"## The count: (\d+), not \d+"), "total"),
    (re.compile(r"finds \*\*(\d+)\*\* modules on this tree"), "total"),
    (re.compile(r"not that read\. (\d+) modules\."), "unclear"),
    (re.compile(r"bulk of these (\d+) reasons"), "total"),
]


def _table_counts(doc_text: str) -> dict[str, int]:
    rows = _ROW_PATH_RE.findall(doc_text)
    total = len(rows)
    unclear = sum(1 for _path, _reason, verdict in rows if verdict.strip() == "unclear")
    return {"total": total, "unclear": unclear}


def _prose_mismatches(doc_text: str) -> list[str]:
    counts = _table_counts(doc_text)
    mismatches = []
    for pattern, kind in _PROSE_PATTERNS:
        match = pattern.search(doc_text)
        if match is None:
            mismatches.append(f"pattern {pattern.pattern!r} not found in doc")
            continue
        stated = int(match.group(1))
        expected = counts[kind]
        if stated != expected:
            mismatches.append(
                f"pattern {pattern.pattern!r} states {stated}, but the table's "
                f"own {kind} row count is {expected}"
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
