"""Integration test for the #507 half of the root `conftest.py` hook.

The unit tests in `test_report_windows_skip_floor_507.py` exercise the pure
functions in `scripts/report_windows_skip_floor.py` directly. This file
checks the other half, the same split `test_conftest_duration_hook_510.py`
already makes for its own #510 report: that a real, unmodified `pytest` run
-- the exact thing `.github/workflows/tests.yml`'s "Run tests" step invokes,
with no extra flag -- actually prints the #507 report via
`pytest_terminal_summary`. Without this, the unit tests could all be green
while the hook itself were never wired up, never imported, or silently
swallowed by pytest plugin discovery.

Uses pytest's own `pytester` fixture to run a real, separate `pytest`
subprocess against a copy of this repo's `conftest.py` and both reporter
modules, so it never nests inside this repo's own `--cov-fail-under=80`
addopt.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stage_conftest_and_modules(pytester):
    shutil.copy(REPO_ROOT / "conftest.py", pytester.path / "conftest.py")
    scripts_dir = pytester.path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "__init__.py").touch()
    shutil.copy(
        REPO_ROOT / "scripts" / "report_test_durations.py",
        scripts_dir / "report_test_durations.py",
    )
    shutil.copy(
        REPO_ROOT / "scripts" / "report_windows_skip_floor.py",
        scripts_dir / "report_windows_skip_floor.py",
    )


def test_report_prints_automatically_with_no_extra_flag(pytester):
    _stage_conftest_and_modules(pytester)
    pytester.makepyfile(
        test_sample="""
        def test_fast():
            assert True
        """
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(
        [
            "*windows skip floor report (#507)*",
            "*state: *",
        ]
    )


def test_report_states_not_applicable_off_windows_and_never_says_over_floor_wrongly(
    pytester, monkeypatch
):
    # MUST NOT FIRE (paired positive control below): off Windows, this leg's
    # own ratio -- even with real skips present -- must read as
    # not-applicable, never as under/over-floor.
    if sys.platform == "win32":
        import pytest

        pytest.skip("this test's own assertion is specifically the non-Windows path")

    _stage_conftest_and_modules(pytester)
    pytester.makepyfile(
        test_sample="""
        import pytest

        def test_a():
            assert True

        @pytest.mark.skip(reason="exercise the skipped branch")
        def test_b():
            assert True
        """
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*windows skip floor report (#507)*", "*state: not-applicable*"])
    assert "OVER FLOOR" not in result.stdout.str()
    assert "over-floor" not in result.stdout.str()


def test_report_does_not_change_the_run_s_exit_code(pytester):
    # Positive control paired with the assertion above: a suite with a real
    # failure still exits non-zero -- the #507 reporter hook is not
    # accidentally swallowing failures either.
    _stage_conftest_and_modules(pytester)
    pytester.makepyfile(
        test_sample="""
        def test_that_fails():
            assert False
        """
    )

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(failed=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*windows skip floor report (#507)*"])
