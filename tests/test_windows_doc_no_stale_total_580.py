"""docs/windows.md must not state an absolute total call-site count (#580).

#580 is the second time the "N `_remember_forward_slash` call sites in
total" sentence drifted out of date: the doc said 10, a live grep on main
counted 15, and #524 records the same drift happening once before. Rather
than adding a third guarded-total mechanism (the shape
`tests/test_windows_skip_triage_497.py` uses for a different doc), the
maintainer decision for #580 is to drop the absolute total from the prose
entirely and let the per-issue breakdown -- which already enumerates which
issue fixed which call site -- carry the story on its own.

This test asserts the doc contains no sentence of that shape, matched by a
regex generic enough to catch any restated total ("N call sites in total",
"N `_remember_forward_slash` call sites") rather than only the literal "10"
that drifted this time; a number-agnostic assertion is what stops this being
the third fragile guard rather than the fix. Per this repos CLAUDE.md ("a
negative assertion needs a positive control"), the "doc has no such
sentence" case is paired with a "the regex actually matches such a
sentence" case, on a fixture string, so this test cannot pass merely because
the regex never matches anything.
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO_ROOT, "docs", "windows.md")

# Matches e.g. "10 `_remember_forward_slash` call sites in total" as well as
# "15 call sites in total" -- any digit run immediately followed by (an
# optional backticked helper name and) "call sites in total".
_STALE_TOTAL_RE = re.compile(
    r"\d+\s*(?:`[^`]+`\s*)?call sites in total", re.IGNORECASE
)


def test_doc_states_no_absolute_call_site_total():
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    match = _STALE_TOTAL_RE.search(doc_text)
    assert match is None, (
        f"docs/windows.md still states an absolute call-site total that "
        f"nothing recomputes, and that has already drifted twice (#524, "
        f"#580): {match.group(0)!r}"
    )


def test_positive_control_regex_fires_on_a_stale_total_sentence():
    # MUST fire: a positive control for the assertion above. Without this,
    # a broken regex (e.g. one with a typo that matches nothing) would make
    # the real test pass whether or not the doc says anything of the kind.
    fixture = "fixed the 8 places this broke -- 10 `_remember_forward_slash` call sites in total across four files."
    assert _STALE_TOTAL_RE.search(fixture) is not None, (
        "positive control failed to fire: the regex should match a sentence "
        "of exactly the shape this test guards against"
    )
