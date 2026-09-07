"""The docs/windows-skip-triage.md list must match the tree it triages (#497).

#497's own body describes exactly this drift already happening once: the
issue cited "92" blanket-skip modules, and by the time this fix landed the
live tree had 107 by the same grep-shape count (and 94 by the corrected,
AST-based count this repo now uses -- see `scripts/windows_skip_triage_497.py`'s
own module docstring for why the two numbers differ). A triage doc that is
never checked against the tree it lists is exactly the kind of number nobody
re-derives before quoting it.

This test recomputes the *live* set of win32 blanket-skip modules itself,
via `scripts.windows_skip_triage_497.find_blanket_skip_modules` -- never a
hardcoded count -- and diffs it against the set of module paths the doc
actually lists. Per this repo's CLAUDE.md ("a negative assertion needs a
positive control"), the real doc's "must match" case is paired with a "must
fire" case: a deliberately wrong fixture list is asserted to fail the same
comparison, so the test cannot pass merely because comparison logic never
runs.

The doc's table is parsed via `scripts.windows_skip_triage_497.parse_doc_table_rows`
rather than a second, independently written row regex here, so a future
consistency guard on this table cannot silently disagree with this one about
what counts as a row (#595's own self-review). The prose-total guard that
originally motivated sharing this parser,
`tests/test_windows_skip_triage_prose_totals_595.py`, was itself removed by
#613 along with the prose numbers it checked -- see
docs/windows-skip-triage.md's "Verdicts" section and
`tests/test_windows_skip_triage_no_stale_prose_counts_613.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.windows_skip_triage_497 import (
    find_blanket_skip_modules,
    parse_doc_table_rows,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
DOC_PATH = os.path.join(REPO_ROOT, "docs", "windows-skip-triage.md")


def _doc_listed_paths(doc_text: str) -> set[str]:
    return {path for path, _reason, _verdict in parse_doc_table_rows(doc_text)}


def test_doc_lists_exactly_the_live_blanket_skip_modules():
    live = set(find_blanket_skip_modules(TESTS_DIR))
    assert live, "sanity: the live tree must have at least one blanket-skip module"

    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()
    listed = _doc_listed_paths(doc_text)

    missing_from_doc = live - listed
    stale_in_doc = listed - live
    assert not missing_from_doc, (
        f"{len(missing_from_doc)} module(s) blanket-skip on win32 but are not "
        f"triaged in {DOC_PATH}: {sorted(missing_from_doc)}"
    )
    assert not stale_in_doc, (
        f"{len(stale_in_doc)} module(s) are listed in {DOC_PATH} but no longer "
        f"blanket-skip on win32 on the live tree: {sorted(stale_in_doc)}"
    )


def test_a_deliberately_wrong_list_is_caught():
    # MUST fire: a positive control for the assertions above. Without this,
    # a broken `_doc_listed_paths` regex (e.g. one that matches nothing) would
    # make both `missing_from_doc` and `stale_in_doc` above vacuously empty,
    # and the real test would pass whether or not the doc says anything true.
    live = set(find_blanket_skip_modules(TESTS_DIR))
    assert live

    fake_doc = "\n".join(f"| `{p}` | fake reason | unclear |" for p in sorted(live)[:-1])
    listed = _doc_listed_paths(fake_doc)

    missing_from_doc = live - listed
    assert missing_from_doc, (
        "positive control failed to fire: dropping one real module from the "
        "fixture list should have produced a nonempty missing_from_doc set"
    )
