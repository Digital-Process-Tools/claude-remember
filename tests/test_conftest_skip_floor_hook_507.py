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

Most of these use pytest's own `pytester` fixture to run a real, separate
`pytest` subprocess against a copy of this repo's `conftest.py` and both
reporter modules, so they never nest inside this repo's own
`--cov-fail-under=80` addopt.

The two Windows-branch tests near the bottom
(`test_the_windows_branch_computes_over_floor_from_real_stats` and
`test_counts_by_distinct_nodeid_not_by_report_entry_count`) do NOT use
`pytester` at all: monkeypatching `sys.platform` to `"win32"` and then
spinning up a NESTED pytest session (`runpytest_inprocess`) breaks pytest's
own internal Windows-console workaround
(`_pytest.capture._windowsconsoleio_workaround`, which itself reads
`sys.platform` during that nested session's own startup) -- observed
directly: it raises `AttributeError: module 'io' has no attribute
'_WindowsConsoleIO'` on a non-Windows host, before the nested session's own
tests even collect. So instead these two load `conftest.py` as a plain
module and call its `pytest_terminal_summary` function directly against a
hand-built fake `terminalreporter`, with `sys.platform` monkeypatched only
for the duration of that one direct call -- no nested pytest session is ever
started while the patch is active, so pytest's own startup code never runs
under the faked platform. This is the only place in this suite that drives
`conftest.py`'s actual `sys.platform == "win32"` branch and its real
`terminalreporter.stats` node-counting logic, rather than calling
`analyze()` directly with hand-fed integers.
"""

from __future__ import annotations

import importlib.util
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


def _load_real_conftest_module():
    """Load the repo's actual `conftest.py` (not a copy) as a plain module,
    so the two direct-call tests below exercise the real, committed wiring."""
    spec = importlib.util.spec_from_file_location("_conftest_507_direct", REPO_ROOT / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeReport:
    def __init__(self, nodeid):
        self.nodeid = nodeid


class _FakeTerminalReporter:
    def __init__(self, stats):
        self.stats = stats
        self.lines: list[str] = []

    def write_line(self, line):
        self.lines.append(line)


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


def test_report_states_not_applicable_off_windows_and_never_says_over_floor_wrongly(pytester):
    # MUST NOT FIRE (paired positive control below, and the two Windows-forced
    # tests further down): off Windows, this leg's own ratio -- even with real
    # skips present -- must read as not-applicable, never as under/over-floor.
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


def test_the_windows_branch_computes_over_floor_from_real_stats(monkeypatch):
    """Positive control for the "not-applicable off Windows" test above, and
    the only place in this suite that drives the real hook's `sys.platform ==
    "win32"` branch (and its real `terminalreporter.stats` extraction) rather
    than calling `analyze(..., is_windows=True)` directly with hand-fed
    integers. See this file's module docstring for why this calls
    `pytest_terminal_summary` directly instead of nesting a nested pytest
    session under a faked platform."""
    conftest = _load_real_conftest_module()
    monkeypatch.setattr(conftest.sys, "platform", "win32")

    stats = {
        "passed": [_FakeReport("t.py::test_a")],
        "skipped": [_FakeReport("t.py::test_b"), _FakeReport("t.py::test_c")],
    }
    reporter = _FakeTerminalReporter(stats)

    conftest.pytest_terminal_summary(reporter, exitstatus=0, config=None)

    output = "\n".join(reporter.lines)
    assert "windows skip floor report (#507)" in output
    assert "state: over-floor" in output
    assert "skipped: 2/3 (66.7%)" in output
    assert "::warning" in output


def test_counts_by_distinct_nodeid_not_by_report_entry_count(monkeypatch):
    """Regression pin: a test whose fixture raises on teardown produces a
    SECOND report (outcome "error", `when="teardown"`) alongside its own
    call-phase "passed" report for the same nodeid. If `conftest.py` summed
    each `terminalreporter.stats` category's report *count* instead of
    counting *distinct nodeids*, that one test would be counted twice in
    `total` while `skipped` still counted its one genuinely skipped sibling
    once -- silently diluting the reported ratio (2/4 = 50% instead of the
    true 1/3 = 33.3%)."""
    conftest = _load_real_conftest_module()
    monkeypatch.setattr(conftest.sys, "platform", "win32")

    stats = {
        "passed": [_FakeReport("t.py::test_with_bad_teardown"), _FakeReport("t.py::test_plain_pass")],
        "skipped": [_FakeReport("t.py::test_skipped")],
        "error": [_FakeReport("t.py::test_with_bad_teardown")],  # same nodeid as the passed entry
    }
    reporter = _FakeTerminalReporter(stats)

    conftest.pytest_terminal_summary(reporter, exitstatus=0, config=None)

    output = "\n".join(reporter.lines)
    assert "state: over-floor" in output
    assert "skipped: 1/3 (33.3%)" in output
    assert "1/4" not in output
    assert "50.0%" not in output
