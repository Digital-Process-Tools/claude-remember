"""Tests for scripts/report_test_durations.py (#510).

`pytest` already prints `--durations`; nothing in this repo read it before
this change, so a single test growing to dominate a suite had no detector
but a human scrolling a CI log by chance -- see #510's own worked example,
a 43.92s test found that way, roughly a fifth of the run.

These tests exercise the pure analysis functions directly (`collect_from_reports`,
`analyze`, `format_report`, `load_baseline`) rather than running a real nested
`pytest` -- the module is written so the duration math never needs pytest's own
process to be exercised, and driving a second pytest session from inside this
suite would tangle this repo's own `--cov-fail-under=80` addopt around a session
it was never meant to measure.

Every "must not fire"/"must not flag" case here is paired with a positive
control in the same test that does trigger the state, per this repo's
CLAUDE.md: a negative assertion with no positive control also passes when the
harness is simply broken.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.report_test_durations import (
    DurationReport,
    analyze,
    collect_from_reports,
    format_report,
    load_baseline,
    save_baseline,
)


class _FakeReport:
    """Stands in for pytest's own `TestReport` -- the hook in conftest.py only
    ever reads `.nodeid`, `.when` and `.duration` off whatever it is handed."""

    def __init__(self, nodeid, when, duration):
        self.nodeid = nodeid
        self.when = when
        self.duration = duration


# ---------------------------------------------------------------------------
# collect_from_reports
# ---------------------------------------------------------------------------


def test_collect_from_reports_sums_phases_per_nodeid():
    reports = [
        _FakeReport("tests/test_x.py::test_a", "setup", 0.01),
        _FakeReport("tests/test_x.py::test_a", "call", 1.0),
        _FakeReport("tests/test_x.py::test_a", "teardown", 0.02),
        _FakeReport("tests/test_x.py::test_b", "call", 0.5),
    ]
    totals = collect_from_reports(reports)
    assert totals == {
        "tests/test_x.py::test_a": 1.03,
        "tests/test_x.py::test_b": 0.5,
    }


def test_collect_from_reports_skips_reports_missing_duration_or_nodeid():
    # Positive control: a well-formed report is still counted alongside the
    # malformed ones, so a bug that dropped everything would not read as a pass.
    class _NoDuration:
        nodeid = "tests/test_x.py::test_c"

    class _NoNodeid:
        duration = 3.0

    class _BadDuration:
        nodeid = "tests/test_x.py::test_d"
        duration = "not-a-number"

    reports = [
        _NoDuration(),
        _NoNodeid(),
        _BadDuration(),
        _FakeReport("tests/test_x.py::test_e", "call", 0.25),
    ]
    totals = collect_from_reports(reports)
    assert totals == {"tests/test_x.py::test_e": 0.25}


def test_collect_from_reports_empty_input_yields_empty_dict():
    assert collect_from_reports([]) == {}


# ---------------------------------------------------------------------------
# analyze -- the three states
# ---------------------------------------------------------------------------


def test_analyze_could_not_measure_when_no_durations_collected():
    # This is the state the issue calls out by name: a step that emits
    # nothing here is indistinguishable from a suite with no hot test.
    result = analyze({}, total_seconds=10.0, baseline=None)
    assert result.state == "could-not-measure"
    assert result.reason


def test_analyze_could_not_measure_when_total_seconds_unusable():
    durations = {"tests/test_x.py::test_a": 1.0}
    for bad_total in (None, 0.0, -5.0):
        result = analyze(durations, total_seconds=bad_total, baseline=None)
        assert result.state == "could-not-measure"
        assert result.reason


def test_analyze_no_baseline_when_baseline_absent():
    durations = {
        "tests/test_x.py::test_a": 9.0,
        "tests/test_x.py::test_b": 1.0,
    }
    result = analyze(durations, total_seconds=10.0, baseline=None)
    assert result.state == "no-baseline"
    assert result.slowest_nodeid == "tests/test_x.py::test_a"
    assert result.slowest_seconds == 9.0
    assert result.share == 0.9
    assert result.baseline_share is None


def test_analyze_measured_when_baseline_present_and_reports_delta():
    # Positive control for the "measured" state and its comparison, paired
    # against the no-baseline case immediately above.
    durations = {
        "tests/test_x.py::test_a": 9.0,
        "tests/test_x.py::test_b": 1.0,
    }
    baseline = {"slowest_share": 0.5}
    result = analyze(durations, total_seconds=10.0, baseline=baseline)
    assert result.state == "measured"
    assert result.share == 0.9
    assert result.baseline_share == 0.5


def test_analyze_ranks_top_n_descending_and_respects_limit():
    durations = {f"tests/test_x.py::test_{i}": float(i) for i in range(1, 6)}
    result = analyze(durations, total_seconds=15.0, baseline=None, top_n=3)
    assert [nodeid for nodeid, _ in result.top] == [
        "tests/test_x.py::test_5",
        "tests/test_x.py::test_4",
        "tests/test_x.py::test_3",
    ]
    assert len(result.top) == 3


# ---------------------------------------------------------------------------
# format_report -- the three states are distinguishable in the printed text
# ---------------------------------------------------------------------------


def test_format_report_could_not_measure_names_the_state_and_reason():
    result = DurationReport(state="could-not-measure", reason="no per-test durations were collected")
    text = format_report(result)
    assert "could-not-measure" in text
    assert "no per-test durations were collected" in text


def test_format_report_no_baseline_says_so_out_loud():
    # The issue is explicit: "no-baseline" must be said out loud, not
    # rendered as if there were no change to report.
    result = analyze(
        {"tests/test_x.py::test_a": 4.0, "tests/test_x.py::test_b": 1.0},
        total_seconds=5.0,
        baseline=None,
    )
    text = format_report(result)
    assert "no-baseline" in text
    assert "no change" not in text.lower()
    assert "80.0%" in text  # the share is reported even with nothing to compare to


def test_format_report_measured_shows_share_and_baseline_delta():
    result = analyze(
        {"tests/test_x.py::test_a": 4.0, "tests/test_x.py::test_b": 1.0},
        total_seconds=5.0,
        baseline={"slowest_share": 0.5},
    )
    text = format_report(result)
    assert "measured" in text
    assert "80.0%" in text
    assert "50.0%" in text


def test_format_report_never_empty_for_any_state():
    # Positive control for "must not silently emit nothing": every state,
    # including the empty/degenerate ones, produces non-blank text.
    for result in (
        DurationReport(state="could-not-measure", reason="x"),
        analyze({}, total_seconds=None, baseline=None),
        analyze({"a": 1.0}, total_seconds=1.0, baseline=None),
        analyze({"a": 1.0}, total_seconds=1.0, baseline={"slowest_share": 0.1}),
    ):
        text = format_report(result)
        assert text.strip()


# ---------------------------------------------------------------------------
# load_baseline
# ---------------------------------------------------------------------------


def test_load_baseline_missing_file_returns_none_with_no_error():
    baseline, error = load_baseline(Path("/definitely/does/not/exist/baseline.json"))
    assert baseline is None
    assert error is None


def test_load_baseline_valid_file_returns_dict(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"slowest_share": 0.33, "slowest_nodeid": "x"}))
    baseline, error = load_baseline(path)
    assert baseline == {"slowest_share": 0.33, "slowest_nodeid": "x"}
    assert error is None


def test_load_baseline_corrupt_json_returns_none_and_an_error(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text("{ not valid json")
    baseline, error = load_baseline(path)
    assert baseline is None
    assert error is not None
    assert "baseline.json" in error


def test_load_baseline_missing_required_field_returns_none_and_an_error(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"unrelated": 1}))
    baseline, error = load_baseline(path)
    assert baseline is None
    assert error is not None


# ---------------------------------------------------------------------------
# save_baseline
# ---------------------------------------------------------------------------


def test_save_baseline_writes_a_file_load_baseline_can_read_back(tmp_path):
    path = tmp_path / "baseline.json"
    result = analyze(
        {"tests/test_x.py::test_a": 4.0, "tests/test_x.py::test_b": 1.0},
        total_seconds=5.0,
        baseline=None,
    )
    save_baseline(path, result)

    loaded, error = load_baseline(path)
    assert error is None
    assert loaded == {"slowest_nodeid": "tests/test_x.py::test_a", "slowest_share": 0.8}


def test_save_baseline_refuses_a_could_not_measure_result(tmp_path):
    path = tmp_path / "baseline.json"
    result = DurationReport(state="could-not-measure", reason="no durations")
    try:
        save_baseline(path, result)
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert not path.exists()
