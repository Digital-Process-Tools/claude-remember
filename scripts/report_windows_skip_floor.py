"""Report the Windows leg's own skip ratio against a recorded floor (#507).

#497 measured the `windows-latest` CI legs reporting `success` while 1201 of
1960 collected tests (roughly 61%) skip -- 92 (now more; see the PR body for
this repository's own recount) test modules blanket-skip on `sys.platform ==
"win32"`, and the matrix has no detector for how much of the suite that
covers. A green leg that skipped most of the suite renders exactly like a
green leg that ran all of it, which is this project's own named defect class
(`CLAUDE.md`: "a check that did not run must not render like a check that
found nothing"), reappearing here at matrix scale instead of inside one test.

This module does not triage the 92 modules -- that is #497's own much larger
follow-up, explicitly out of scope for the change that added this file. It
only makes the ratio visible on every run, the same way
`scripts/report_test_durations.py` (#510) makes a dominant test's share of
suite time visible: a pure `analyze()`/`format_report()` pair, wired into
`conftest.py`'s existing `pytest_terminal_summary` hook so it runs
automatically, with no extra flag, on every leg of the matrix -- Windows and
otherwise.

**Deliberately a report, never a gate** -- the same choice #510 made and
states explicitly as "what NOT to add". Today's actual Windows ratio is
already far over any floor worth recording (#497's own 61%), so a hard
`sys.exit(1)` here would turn every `windows-latest` leg of every future pull
request red until the 92-module triage lands, for a condition this change
does not fix and was told not to attempt. That blocks unrelated work on an
already-tracked, already-labelled (`priority:high`) issue rather than
surfacing it. Annotating loudly -- a `::warning::` GitHub Actions command in
addition to the plain-text report -- makes the leg impossible to read as a
quiet, uninspected green without making it a merge blocker for work that has
nothing to do with #497.
"""

from __future__ import annotations

from dataclasses import dataclass

# 10% of collected tests is the issue's own suggested starting point (#507's
# "Ask" item 2: "A floor of 10% ... pick another if measured"). Measured
# against this repository's own tree at the time this file was added: 107 of
# 174 test modules now blanket-skip on `win32` (up from #497's 92; see the PR
# body), which on a full local run collects 2042 tests. This module cannot
# measure the actual Windows-leg skip count itself -- nobody on this project
# has a Windows box to run it on (**reasoned, not observed** -- the same
# label this README already uses for a claim it cannot verify locally) -- so
# the floor is deliberately kept low rather than fitted to today's number:
# a floor picked to sit just under "whatever it already is" would stop
# flagging the very drift #497 found (92 -> 107 with nothing announcing it),
# which is the one thing this file exists to prevent happening silently
# again.
DEFAULT_FLOOR = 0.10


@dataclass
class SkipFloorReport:
    state: str  # "not-applicable" | "under-floor" | "over-floor" | "could-not-measure"
    skipped: int | None = None
    total: int | None = None
    ratio: float | None = None
    floor: float | None = None
    reason: str | None = None


def analyze(
    skipped,
    total,
    *,
    is_windows: bool,
    floor: float = DEFAULT_FLOOR,
) -> SkipFloorReport:
    """Classify one run's skip ratio into one of four named states.

    `is_windows` is passed in rather than read from `sys.platform` inside
    this function so the pure logic stays testable from any host -- the
    caller (`conftest.py`) is the one place that reads the real platform.
    """
    if not is_windows:
        return SkipFloorReport(
            state="not-applicable",
            reason="this leg is not Windows -- the floor only guards the "
            "win32 blanket-skip pattern (#497)",
        )
    if total is None or not isinstance(total, int) or total <= 0:
        return SkipFloorReport(
            state="could-not-measure",
            reason=f"total collected test count is not a usable number ({total!r})",
        )
    if skipped is None or not isinstance(skipped, int) or skipped < 0:
        return SkipFloorReport(
            state="could-not-measure",
            reason=f"skipped test count is not a usable number ({skipped!r})",
        )

    ratio = skipped / total
    state = "over-floor" if ratio > floor else "under-floor"
    return SkipFloorReport(
        state=state,
        skipped=skipped,
        total=total,
        ratio=ratio,
        floor=floor,
    )


def format_report(result: SkipFloorReport) -> str:
    """Render `result` as human-readable text. Never returns a blank string
    for any state -- see this module's own docstring and #510's identical
    argument for why a silent no-op is exactly the defect class this exists
    to close."""
    lines = ["-- windows skip floor report (#507) --"]

    if result.state in ("not-applicable", "could-not-measure"):
        lines.append(f"state: {result.state} ({result.reason})")
        return "\n".join(lines)

    lines.append(f"state: {result.state}")
    lines.append(
        f"skipped: {result.skipped}/{result.total} ({result.ratio:.1%}), "
        f"floor {result.floor:.0%}"
    )
    if result.state == "over-floor":
        lines.append(
            "OVER FLOOR -- more of this leg is skipped (the win32 blanket-skip "
            "pattern, #497) than the recorded floor allows. This does not fail "
            "the build (see this file's own module docstring); it is the loud "
            "version of the same green #497 measured as silent."
        )

    return "\n".join(lines)


def github_actions_warning(result: SkipFloorReport) -> str | None:
    """A `::warning::` workflow command for `result`, or `None` if this
    result does not warrant one. Kept separate from `format_report` so the
    plain-text report -- read by a human scrolling the log -- and the
    annotation -- read by GitHub's own Checks UI -- can each be tested and
    reasoned about on their own."""
    if result.state != "over-floor":
        return None
    return (
        "::warning title=Windows CI skip floor exceeded::"
        f"{result.skipped}/{result.total} tests skipped "
        f"({result.ratio:.1%}) on this Windows leg, over the recorded "
        f"{result.floor:.0%} floor (#497, #507)."
    )
