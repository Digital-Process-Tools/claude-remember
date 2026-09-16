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
import shlex
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


def test_a_backward_clock_step_is_reported_as_could_not_measure_not_a_negative_number(tmp_path):
    """Self-review finding on the #706 fix itself: a wall clock stepped
    BACKWARD between the start and end reads (an NTP correction mid-hook) is
    an un-named FOURTH state hiding inside what looks like "measured" --
    without a clamp, `_REMEMBER_HOOK_ELAPSED_S` goes negative and the daily
    log gets a plausible-looking "session-start took -3s" instead of an
    honest "could not measure".

    EPOCHSECONDS cannot be pinned (confirmed elsewhere in this suite), so
    this forces the `date +%s` fallback path with
    `_REMEMBER_HOOK_FORCE_DATE_FALLBACK=1` -- a test-only seam mirroring
    lib-clock.sh's own REMEMBER_NO_PRINTF_T -- and shims `date` to answer a
    LATER epoch first (start) and an EARLIER one second (end), the exact
    shape of a backward step.
    """
    home, project, remember = _store(tmp_path)
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir(parents=True, exist_ok=True)
    (fake_dir / "date-calls.log").write_text("")
    fake_date = fake_dir / "date"
    # First call answers a LATER epoch (t0), every call after answers an
    # EARLIER one (t1 and beyond) -- a stable backward step regardless of
    # exactly how many `date +%s` calls this run happens to make, rather
    # than a short queue that would raise IndexError/echo nothing on an
    # unexpected extra call.
    fake_date.write_text(
        '#!/bin/bash\n'
        'here="$(cd "$(dirname "$0")" && pwd)"\n'
        'echo "$@" >> "$here/date-calls.log"\n'
        'count_file="$here/date-count"\n'
        'n=$(cat "$count_file" 2>/dev/null || echo 0)\n'
        'echo $((n + 1)) > "$count_file"\n'
        'if [ "$n" -eq 0 ]; then\n'
        '  echo 2000000000\n'
        'else\n'
        '  echo 1000000000\n'
        'fi\n'
    )
    fake_date.chmod(0o755)
    # #488 (tests/test_session_end_log_names_488.py): Path.chmod(0o755) run
    # by a native Windows Python only ever toggles the read-only attribute --
    # MSYS2's own exec()/PATH-search checks may see none of it, and Git Bash
    # falls through to the REAL `date` on PATH instead of this one.
    # Best-effort second attempt: ask bash itself to chmod the file, which
    # goes through MSYS2's own permission layer instead of Windows Python's.
    if os.name == "nt" and BASH is not None:
        subprocess.run(
            [BASH, "-c", f"chmod +x {shlex.quote(fake_date.as_posix())}"],
            capture_output=True, timeout=10, check=False,
        )

    env = _env(home, project, remember, f"{fake_dir}{os.pathsep}{os.environ['PATH']}")
    env["_REMEMBER_HOOK_FORCE_DATE_FALLBACK"] = "1"

    result = _run_hook(env)
    assert result.returncode == 0, result.stderr[-2000:]

    calls_log = (fake_dir / "date-calls.log")
    if not (calls_log.is_file() and calls_log.read_text().strip()) and os.name == "nt":
        # POSIX chmod actually sets the executable bit, so an empty
        # date-calls.log there means a real regression (the hook stopped
        # calling `date`), not the #488 Windows-only shim-detection gap --
        # skipping unconditionally would convert that into a silent SKIP
        # with a Windows-flavoured excuse that is false on this platform.
        pytest.skip(
            "the date PATH shim did not intercept `date +%s` on this "
            "platform -- date-calls.log stayed empty, consistent with the "
            "real `date` binary answering instead (#488's own class: a "
            "chmod'd-by-native-Windows-Python shim is not always treated "
            "as executable by Git Bash/MSYS2's PATH search). #706's own "
            "fix is not what this pins and is unaffected either way."
        )

    log_text = _daily_log_text(remember)
    assert "could not measure its own duration" in log_text, (
        f"a backward clock step must be reported as could-not-measure: {log_text!r}"
    )
    assert "-1000000000s" not in log_text and " -" not in log_text.split(
        "session-start"
    )[-1][:20], (
        f"a negative duration leaked into the daily log instead of being "
        f"clamped to could-not-measure: {log_text!r}"
    )
    assert "=== SESSION-START ===" not in result.stdout, (
        "an unmeasurable duration must never surface as a slow-session banner"
    )
