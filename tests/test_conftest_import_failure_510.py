"""A broken `scripts/report_test_durations.py` must degrade the #510
reporter to `could-not-measure`, never take the whole pytest session down
with it (review finding on #510's own diff).

The import used to sit at `conftest.py` module scope, outside the hook's
`try/except`. A module-scope import failure in a `conftest.py` is a pytest
collection error -- fatal for the whole session: zero tests run, non-zero
exit, and no #510 report printed at all. That is exactly the "reports
nothing and nobody notices" failure #510 was filed to close, just widened
from one slow test to the entire suite. The import now happens inside
`pytest_terminal_summary`'s own `try`.
"""

from __future__ import annotations

from pathlib import Path

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stage_conftest(pytester):
    (pytester.path / "conftest.py").write_text(
        (REPO_ROOT / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )


def test_a_broken_report_module_does_not_abort_the_session(pytester):
    _stage_conftest(pytester)
    scripts_dir = pytester.path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "report_test_durations.py").write_text(
        "raise RuntimeError('simulated import-time failure')\n", encoding="utf-8"
    )
    pytester.makepyfile(
        test_sample="""
        def test_fast():
            assert True
        """
    )

    result = pytester.runpytest_subprocess()

    # The real point: the broken reporter module must not take collection
    # down with it -- the actual test still runs and passes.
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*could-not-measure*"])
