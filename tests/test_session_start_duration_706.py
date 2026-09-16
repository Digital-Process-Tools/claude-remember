"""#706: the SessionStart hook must say how long it took.

From #660's own investigation, the single sharpest piece of feedback: a
reporter spent days measuring this plugin before finding the actual cause on
their own machine (a kernel leak inflating every fork 3-10x). The plugin
cannot diagnose a slow HOST and must not try to -- but a hook that states its
own runtime is how the next person learns the question is not about the
plugin at all.

Two things are asserted, and the negative half is paired with a positive
control using the SAME mechanism (the configurable threshold) rather than
trying to make the test harness itself run slowly -- an unreliable way to
force a "slow" branch, and exactly the kind of flaky timing dependency this
file avoids by making the THRESHOLD the variable instead of the CLOCK:

* ALWAYS: a "session-start took Ns" line reaches the daily log, regardless of
  how fast the hook was. A line on every start would be noise in the SESSION
  itself, but the daily log is the record #226/#706 both want this kind of
  measurement to build up in.
* ONLY OVER THRESHOLD: the same number is ALSO printed into the session
  output, framed as its own section, so a slow host is visible without
  digging into the daily log first.

The bar (CLAUDE.md): would this test still pass if the code did nothing? No
-- before the fix, MEMORY_LOG_DATE... no, wrong issue's bar; here: before the
fix there is no "session-start took" line anywhere, in the log or in the
session, so both assertions below are genuinely new coverage rather than
restating what the hook always did.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from .test_session_start_windows_benchmark_669 import (
    BASH,
    SESSION_START,
    _env,
    _payload,
    _store,
    _write_no_crlf,
)

pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash on this host (#432)"
)


def _run_hook(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, SESSION_START.as_posix()],
        input=_payload(),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _daily_log_text(remember: Path) -> str:
    """The single memory-YYYY-MM-DD.log file a fresh fixture's first hook
    run creates -- there is exactly one, so a glob is simpler and no less
    correct than deriving today's date independently of the hook under
    test."""
    logs = sorted((remember / "logs").glob("memory-*.log"))
    assert logs, f"no daily log file was created under {remember / 'logs'}"
    return logs[0].read_text(encoding="utf-8", errors="replace")


def test_a_fast_run_logs_its_duration_but_stays_quiet_in_the_session(tmp_path):
    """Default threshold (5s, config.example.json's own default -- nothing
    written here overrides it). A test fixture's hook run is nowhere near
    5s, so the daily log gets the record and the session output stays
    exactly as it was before #706 -- no new section, no new noise."""
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])

    result = _run_hook(env)
    assert result.returncode == 0, result.stderr[-2000:]

    log_text = _daily_log_text(remember)
    assert "session-start took" in log_text, (
        f"the daily log never recorded this hook's own duration: {log_text!r}"
    )

    assert "=== SESSION-START ===" not in result.stdout, (
        "a fast run must not surface a duration line in the session itself "
        "-- that is noise on a healthy host, and the whole reason the "
        "threshold exists"
    )


def test_a_run_over_threshold_also_surfaces_the_duration_in_the_session(tmp_path):
    """POSITIVE CONTROL for the test above, using the SAME mechanism (the
    configurable threshold) rather than an unreliable attempt to make the
    hook itself run slowly. session_start_slow_threshold_s: 0 means ANY
    measured duration (0s included) is "at or above" the threshold, so this
    is not a timing-dependent test -- it is a threshold-dependent one, and
    the threshold is the one thing this test controls directly.

    Without this control, a broken implementation that never prints the
    session-facing line at all would still pass the "stays quiet" half of
    the test above -- silence would look like correct behaviour in both the
    quiet case and the always-quiet-because-broken case.
    """
    home, project, remember = _store(tmp_path)
    _write_no_crlf(
        remember / "config.json",
        json.dumps({"session_start_slow_threshold_s": 0}),
    )
    env = _env(home, project, remember, os.environ["PATH"])

    result = _run_hook(env)
    assert result.returncode == 0, result.stderr[-2000:]

    assert "=== SESSION-START ===" in result.stdout, (
        f"threshold 0 must surface the duration in the session -- stdout: "
        f"{result.stdout!r}"
    )

    # ALWAYS still holds under the surfaced case too -- the daily log is not
    # a fallback for when the session line is absent, it is unconditional.
    log_text = _daily_log_text(remember)
    assert "session-start took" in log_text, (
        f"the daily log stopped recording once the session line started "
        f"showing up too: {log_text!r}"
    )
