"""Root-level pytest plugin: the #510 test-duration reporter.

`pytest` already prints `--durations` (wired via `addopts` in
`pyproject.toml`); nothing read it before this, so a single test dominating
a suite had no detector but a human scrolling a CI log by chance (#510's own
worked example: a test at 43.92s against ~6.5s for the next slowest, roughly
a fifth of the whole run, found only because a maintainer happened to read
that far).

`pytest_terminal_summary` fires at the end of every `pytest` invocation --
local or any leg of the CI matrix -- with no extra flag required, and reads
the same `TestReport` objects pytest's own summary is built from off
`terminalreporter.stats`, rather than re-parsing pytest's own formatted
`--durations` text. The actual duration math lives in
`scripts/report_test_durations.py`, kept separate so it can be unit-tested
without driving a nested pytest session.

This intentionally never raises and never touches `exitstatus`: a reporter
bug must not turn a green run red, and #510 is explicit that this is a
report a human reads, never a gate. That includes the import of
`scripts/report_test_durations.py` itself -- it happens inside
`pytest_terminal_summary`'s own `try`, not at module scope, so a defect in
that module degrades to the `could-not-measure` state instead of aborting
collection for the whole session (a module-scope import failure is a
`conftest.py` collection error, which pytest treats as fatal: zero tests
run, non-zero exit, no report at all -- exactly the total, silent failure
this feature exists to prevent, just promoted from "one test" to "every
test"). See `tests/test_conftest_import_failure_510.py` for the case this
guards against.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))


def pytest_sessionstart(session):
    # Recorded here rather than trusted off `terminalreporter` -- pytest does
    # not guarantee that object carries a session-start timestamp under any
    # stable name across versions, and a `getattr(..., None)` against a name
    # that silently stopped existing would turn "measured" into
    # "could-not-measure" on some future pytest with nothing pointing at why.
    session.config._report_test_durations_510_start = time.time()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    try:
        from scripts.report_test_durations import (
            DEFAULT_BASELINE_PATH,
            analyze,
            collect_from_reports,
            format_report,
            load_baseline,
        )

        reports = [
            report
            for report_list in terminalreporter.stats.values()
            for report in report_list
            if hasattr(report, "duration")
        ]
        durations = collect_from_reports(reports)

        start = getattr(config, "_report_test_durations_510_start", None)
        total_seconds = (time.time() - start) if start is not None else None

        baseline, baseline_error = load_baseline(DEFAULT_BASELINE_PATH)
        result = analyze(durations, total_seconds, baseline)
        result.baseline_error = baseline_error

        terminalreporter.write_line(format_report(result))
    except Exception as exc:  # noqa: BLE001 -- pragma: no cover - defensive; see module docstring
        terminalreporter.write_line(
            f"-- test duration report (#510) --\nstate: could-not-measure (reporter raised {exc!r})"
        )
