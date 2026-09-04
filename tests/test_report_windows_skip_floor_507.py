"""Tests for scripts/report_windows_skip_floor.py (#507).

#497 measured the `windows-latest` CI legs reporting `success` over 1201
skipped tests -- roughly 61% of the suite -- with nothing counting how much
of the matrix that covers. These tests exercise the pure `analyze()` /
`format_report()` / `github_actions_warning()` functions directly, the same
choice `test_report_test_durations_510.py` makes for its own #510 module and
for the same reason: a real nested `pytest` run would tangle this repo's own
`--cov-fail-under=80` addopt around a session never meant to measure it.

Every "must not fire" case here is paired with a positive control in the
same test, per this repo's CLAUDE.md: a negative assertion with no positive
control also passes when the harness is simply broken.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.report_windows_skip_floor import (
    analyze,
    format_report,
    github_actions_warning,
)

# ---------------------------------------------------------------------------
# analyze()
# ---------------------------------------------------------------------------


def test_not_windows_is_not_applicable_regardless_of_ratio():
    # MUST NOT fire over-floor: a non-Windows leg's own ratio -- even a
    # terrible one -- must never be judged against a floor built for the
    # win32 blanket-skip pattern specifically.
    result = analyze(skipped=999, total=1000, is_windows=False)
    assert result.state == "not-applicable"
    assert result.skipped is None and result.total is None


def test_windows_under_the_floor_is_under_floor():
    result = analyze(skipped=5, total=1000, is_windows=True, floor=0.10)
    assert result.state == "under-floor"
    assert result.ratio == 0.005


def test_windows_over_the_floor_is_over_floor():
    # Positive control for the case above: the same shape of call, only the
    # ratio changed, must reach the opposite state.
    result = analyze(skipped=200, total=1000, is_windows=True, floor=0.10)
    assert result.state == "over-floor"
    assert result.ratio == 0.2


def test_exactly_at_the_floor_is_not_over():
    # The floor is a "crosses" threshold, not a "reaches" one -- 10.0% at a
    # 10% floor must not read as a regression.
    result = analyze(skipped=100, total=1000, is_windows=True, floor=0.10)
    assert result.state == "under-floor"


def test_497s_own_measured_ratio_is_over_floor():
    # #497's own worked example: 1201/1960 on windows-latest, 3.11. This is
    # the exact input this floor exists to catch.
    result = analyze(skipped=1201, total=1960, is_windows=True, floor=0.10)
    assert result.state == "over-floor"
    assert round(result.ratio, 3) == round(1201 / 1960, 3)


def test_zero_total_is_could_not_measure_not_a_crash():
    result = analyze(skipped=0, total=0, is_windows=True)
    assert result.state == "could-not-measure"
    assert result.reason is not None


def test_missing_total_is_could_not_measure():
    result = analyze(skipped=5, total=None, is_windows=True)
    assert result.state == "could-not-measure"


def test_negative_skipped_is_could_not_measure():
    result = analyze(skipped=-1, total=100, is_windows=True)
    assert result.state == "could-not-measure"


# ---------------------------------------------------------------------------
# format_report()
# ---------------------------------------------------------------------------


def test_format_report_never_returns_blank_for_any_state():
    for result in (
        analyze(skipped=999, total=1000, is_windows=False),
        analyze(skipped=5, total=1000, is_windows=True),
        analyze(skipped=200, total=1000, is_windows=True),
        analyze(skipped=0, total=0, is_windows=True),
    ):
        text = format_report(result)
        assert text.strip(), f"format_report returned blank text for state {result.state!r}"
        assert result.state in text


def test_format_report_names_over_floor_loudly():
    result = analyze(skipped=200, total=1000, is_windows=True, floor=0.10)
    text = format_report(result)
    assert "OVER FLOOR" in text
    assert "20.0%" in text


def test_format_report_under_floor_does_not_say_over_floor():
    # Positive control paired with the test above: the same function, a
    # different ratio, must not carry the loud phrase at all.
    result = analyze(skipped=5, total=1000, is_windows=True, floor=0.10)
    text = format_report(result)
    assert "OVER FLOOR" not in text


# ---------------------------------------------------------------------------
# github_actions_warning()
# ---------------------------------------------------------------------------


def test_warning_command_only_for_over_floor():
    over = analyze(skipped=200, total=1000, is_windows=True, floor=0.10)
    under = analyze(skipped=5, total=1000, is_windows=True, floor=0.10)
    not_applicable = analyze(skipped=999, total=1000, is_windows=False)

    warning = github_actions_warning(over)
    assert warning is not None
    assert warning.startswith("::warning")
    assert "200/1000" in warning

    assert github_actions_warning(under) is None
    assert github_actions_warning(not_applicable) is None
