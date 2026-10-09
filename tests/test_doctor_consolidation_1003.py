"""doctor.sh reports the silent states #1003 describes.

Two independent WARNs, both left out of the VERDICT ladder on purpose (same
convention as log rotation and case divergence, both already WARN-only):

1. The selected Python is below this plugin's supported floor. detect-
   tools.sh's own candidate loop now enforces the floor itself, so this is
   mainly a backstop for a path that bypasses it -- the tool-verdict cache
   (#668) keys on PATH identity only; it never re-verifies that the
   interpreter STILL on disk at that path still meets the floor once the
   cache has been trusted, so an interpreter downgraded in place (a pyenv
   switch, a reinstalled venv) without PATH changing is reported stale.
   doctor.sh calls `_remember_run_python -V` for real on every run,
   independent of the cache, so it catches exactly this gap.

2. Consolidation's last recorded attempt failed and no later attempt has
   succeeded since -- the reporter's exact silent state: capture
   (PostToolUse) keeps working, "capture is working" in the Verdict ladder
   describes PostToolUse and nothing else, and nothing before this issue
   ever looked at run-consolidation.sh's OWN log entries.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_doctor_oversized_store_348 import CAP, _fill, _project, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


def _python_stub(bindir: Path, version_text: str) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\necho "{version_text}"\nexit 0\n', encoding="utf-8")
    stub.chmod(0o755)


def test_a_floor_meeting_python_has_no_floor_warn(tmp_path):
    """Positive control: the ordinary case (a real, floor-meeting python3)
    must not warn at all -- a check that fires unconditionally would tell
    every healthy install its Python is unsupported."""
    home, project, remember = _project(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "below this plugin's supported floor" not in result.stdout, (
        "a healthy, floor-meeting Python was warned about:\n" + result.stdout
    )


def test_an_interpreter_downgraded_after_caching_still_warns(tmp_path):
    """The cache (#668) keys on PATH identity, not on what is actually at
    that path -- it never re-probes once a PATH string has been seen
    before. Simulate the gap: cache a floor-meeting python3, then replace
    the SAME path with a below-floor stub without touching PATH. detect-
    tools.sh will trust the stale cache and never re-probe, but doctor.sh's
    own `-V` call is unconditional and must still catch it."""
    home, project, remember = _project(tmp_path)
    bindir = tmp_path / "bin"
    cache_tmpdir = tmp_path / "cachetmp"
    cache_tmpdir.mkdir()
    extra_env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(cache_tmpdir),
    }

    _python_stub(bindir, "Python 3.12.0")
    first = _run(home, project, remember, extra_env)
    assert first.returncode == 0, first.stderr
    assert "below this plugin's supported floor" not in first.stdout, first.stdout

    # Same PATH, same cache dir -- the interpreter AT that path "downgrades"
    # without the cache's own key (the PATH string) ever changing.
    _python_stub(bindir, "Python 2.7.18")
    second = _run(home, project, remember, extra_env)

    assert second.returncode == 0, second.stderr
    assert "below this plugin's supported floor" in second.stdout, (
        "doctor did not warn about a Python that is actually below the "
        "floor at the moment it ran, even though the tool-verdict cache "
        "still names it as the accepted interpreter:\n" + second.stdout
    )
    assert "2.7.18" in second.stdout, second.stdout


def _log_line(remember: Path, line: str) -> None:
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_file = logs / "memory-2026-01-01.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"12:00:00 {line}\n")


def test_consolidation_errors_with_no_later_success_warns(tmp_path):
    """The reporter's exact silent state: consolidation has failed and
    never once recovered, while nothing else in the report would say so."""
    home, project, remember = _project(tmp_path)
    _log_line(remember, "[consolidation] start")
    _log_line(remember, "[consolidation] ERROR: pipeline failed -- boom")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "Consolidation's last recorded attempt failed" in result.stdout, (
        "a consolidation log showing only ERROR entries produced no "
        "warning:\n" + result.stdout
    )
    assert "boom" in result.stdout, result.stdout


def test_consolidation_done_after_an_earlier_error_is_ok(tmp_path):
    """Positive control, same mechanism: an ERROR followed by a later
    success must read as healthy, not as the still-broken state above --
    otherwise a single historical failure would haunt the report forever
    even after the pipeline recovered on its own."""
    home, project, remember = _project(tmp_path)
    _log_line(remember, "[consolidation] ERROR: pipeline failed -- boom")
    _log_line(remember, "[consolidation] done: 3 files consolidated")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "Consolidation: last recorded attempt succeeded" in result.stdout, (
        "a consolidation history ending in success was not reported OK:\n"
        + result.stdout
    )
    assert "Consolidation's last recorded attempt failed" not in result.stdout, (
        "an old ERROR, since superseded by a later success, still reads "
        "as the broken state:\n" + result.stdout
    )


def test_no_consolidation_history_is_neutral_not_a_claim(tmp_path):
    """Third state: nothing recorded yet (a fresh install) must say so,
    never silently render as either the healthy or the broken line --
    "I looked and found nothing" and "nothing has happened yet" are
    different facts."""
    home, project, remember = _project(tmp_path)
    _fill(remember / "recent.md", 100)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "No consolidation attempt recorded yet" in result.stdout, result.stdout
    assert "Consolidation: last recorded attempt succeeded" not in result.stdout
    assert "Consolidation's last recorded attempt failed" not in result.stdout


def test_consolidation_health_does_not_move_the_verdict(tmp_path):
    """Same convention as log rotation and case divergence: this is a
    second, independent signal and must never override the VERDICT
    ladder's own existing arms (#1003's instructions are explicit that
    this stays WARN-only, like those two)."""
    home, project, remember = _project(tmp_path)
    _log_line(remember, "[consolidation] ERROR: pipeline failed -- boom")
    _fill(remember / "recent.md", CAP + 1000)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        if line.startswith("VERDICT:"):
            assert "boom" not in line, (
                "the consolidation-log WARN leaked into the VERDICT line, "
                "which must stay driven by the existing arms only:\n" + line
            )
            return
    raise AssertionError("no VERDICT line in output:\n" + result.stdout)
