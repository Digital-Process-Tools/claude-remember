"""Integration test for the root `conftest.py` hook added by #510.

The unit tests in `test_report_test_durations_510.py` exercise the pure
functions in `scripts/report_test_durations.py` directly. This file checks
the other half: that a real, unmodified `pytest` run -- the exact thing
`.github/workflows/tests.yml`'s "Run tests" step invokes, with no extra flag
-- actually prints the #510 report, via `pytest_terminal_summary` firing at
session end. Without this, the unit tests could all be green while the hook
itself were never wired up, never imported, or silently swallowed by pytest
plugin discovery -- exactly the "reports nothing and nobody notices" failure
mode #510 exists to close.

Uses pytest's own `pytester` fixture (a built-in testing plugin for testing
pytest plugins) to run a real, separate `pytest` subprocess against a copy
of this repo's `conftest.py` and `scripts/report_test_durations.py`, so it
never nests inside -- and never gets tangled in -- this repo's own
`--cov-fail-under=80` addopt.
"""

from __future__ import annotations

import shutil
from pathlib import Path

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stage_conftest_and_module(pytester):
    """Copy the real conftest.py and scripts/report_test_durations.py into
    the pytester sandbox, so the subprocess run exercises the actual files
    this pull request changed rather than a hand-copied stand-in."""
    shutil.copy(REPO_ROOT / "conftest.py", pytester.path / "conftest.py")
    scripts_dir = pytester.path / "scripts"
    scripts_dir.mkdir()
    shutil.copy(
        REPO_ROOT / "scripts" / "report_test_durations.py",
        scripts_dir / "report_test_durations.py",
    )


def test_report_prints_automatically_with_no_extra_flag(pytester):
    _stage_conftest_and_module(pytester)
    pytester.makepyfile(
        test_sample="""
        def test_fast():
            assert True

        def test_also_fast():
            assert True
        """
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(
        [
            "*test duration report (#510)*",
            "*state: no-baseline*",
            "*slowest test:*",
            "*slowest share of total:*",
        ]
    )
    # A "no change" phrasing would misreport the very state #510 says must be
    # said out loud instead.
    assert "no change" not in result.stdout.str().lower()


def test_report_does_not_change_the_run_s_exit_code(pytester):
    # Positive control paired with the assertion below: a suite with a real
    # failure still exits non-zero -- the reporter hook is not accidentally
    # swallowing failures, only adding a report alongside them.
    _stage_conftest_and_module(pytester)
    pytester.makepyfile(
        test_sample="""
        def test_that_fails():
            assert False
        """
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(failed=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*test duration report (#510)*"])
