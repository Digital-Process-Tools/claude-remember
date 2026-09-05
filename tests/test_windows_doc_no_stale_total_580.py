"""docs/windows.md must not state an absolute total call-site count (#580).

#580 is the second time the "N `_remember_forward_slash` call sites in
total" sentence drifted out of date: the doc said 10, a live grep on main
counted 15, and #524 records the same drift happening once before. Rather
than adding a third guarded-total mechanism (the shape
`tests/test_windows_skip_triage_497.py` uses for a different doc), the
maintainer decision for #580 is to drop the absolute total from the prose
entirely and let the per-issue breakdown -- which already enumerates which
issue fixed which call site -- carry the story on its own.

An earlier version of this test matched only the exact word order "N ...
call sites in total". Both spawned reviewers (#580's self-review) flagged
that a re-drift phrased even slightly differently -- "a total of N call
sites", "N total call sites", "in total there are N call sites" -- would
sail past a fixed-word-order regex while the test kept reporting green,
which is precisely the false confidence #580 exists to remove. This version
instead checks, for every digit run in the doc, whether "call site" and
"total" both occur within a small window of characters around it
(whitespace-normalized, so a line-wrapped phrase still matches) -- a
co-occurrence check that is order- and phrasing-independent rather than one
more literal string to keep in sync. It is still not exhaustive prose
matching (a rewrite that never uses the word "total", or that separates the
digit from "call sites" by more than the window, would still slip past),
and this docstring says so rather than re-claiming the completeness the
first version overclaimed.

Per this repos CLAUDE.md ("a negative assertion needs a positive control"),
the "doc has no such co-occurrence" case is paired with a "the check
actually fires on such a co-occurrence" case, on several fixture strings
covering different phrasings, so this test cannot pass merely because the
check never fires.
"""

from __future__ import annotations

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATH = os.path.join(REPO_ROOT, "docs", "windows.md")

# How far either side of a digit run to look for "call site" and "total"
# co-occurring. Wide enough to span a backticked helper name or a line wrap,
# narrow enough not to fire on two unrelated mentions of "total" elsewhere
# in a long paragraph.
_WINDOW = 80


def _find_stale_total(text: str) -> str | None:
    """Return the first digit run sitting near both "call site" and "total",
    or None. Whitespace inside the window is collapsed to single spaces
    before the substring check, so a phrase split across a line wrap (or
    written with irregular spacing) still counts as adjacent."""
    for match in re.finditer(r"\d+", text):
        start = max(0, match.start() - _WINDOW)
        end = min(len(text), match.end() + _WINDOW)
        window = re.sub(r"\s+", " ", text[start:end].lower())
        if "call site" in window and "total" in window:
            return match.group(0)
    return None


def test_doc_states_no_absolute_call_site_total():
    with open(DOC_PATH, encoding="utf-8") as fh:
        doc_text = fh.read()

    stale_digits = _find_stale_total(doc_text)
    assert stale_digits is None, (
        f"docs/windows.md still states what reads like an absolute "
        f"call-site total that nothing recomputes, and that has already "
        f"drifted twice (#524, #580): found {stale_digits!r} near both "
        f"'call site' and 'total'"
    )


def test_positive_control_fires_on_several_stale_total_phrasings():
    # MUST fire: a positive control for the assertion above, across several
    # word orders -- not just the one literal phrase the first version of
    # this test hardcoded, which is the gap #580's own reviewers found.
    # Without this, a broken check (e.g. one that matches nothing) would
    # make the real test pass whether or not the doc says anything of the
    # kind, in any phrasing.
    phrasings = [
        "10 `_remember_forward_slash` call sites in total across four files.",
        "a total of 15 call sites across four files.",
        "15 total call sites across four files.",
        "in total there are 15 call sites across four files.",
        "15 call\nsites in total across four files.",
    ]
    for fixture in phrasings:
        assert _find_stale_total(fixture) is not None, (
            f"positive control failed to fire on phrasing {fixture!r}: the "
            f"check should catch a stale-total sentence regardless of word "
            f"order"
        )
