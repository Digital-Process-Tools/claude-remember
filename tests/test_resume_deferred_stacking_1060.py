"""#1060: SDK hosts fire SessionStart with source=resume on every resumed
turn, not once per session. Each start backgrounds the deferred phase
(capture-gap-check.sh, #660); before this fix nothing stopped a second start
for the SAME session from launching a SECOND deferred phase while the first
was still running (or orphaned after its own parent hook exited), and the
reporter measured 42 stacked processes and host load 275.

Fix: a stale-safe per-session PID file (session-start-hook.sh, keyed on
CURRENT_SESSION_ID) -- a second deferred-phase launch for a session whose
PID file still names a LIVE process is skipped; a dead PID is treated as
stale and overwritten.

TDD shape: a positive control (a genuinely DIFFERENT session's deferred
phase must still launch) is paired with the actual claim (a second start for
a session whose deferred phase is still alive must not launch a second one)
-- the "must fire" / "must not fire" pairing CLAUDE.md requires for a
negative assertion.

Deterministic by construction, not by timing: "a deferred phase is still
running for session S" is simulated directly by pre-seeding
`REMEMBER_DIR/tmp/deferred-<S>.pid` with the PID of a REAL long-lived
process this test starts and controls (`sleep 60`), rather than racing the
real capture-gap-check.sh child to reach some gate before the assertion
runs. The guard function is pure bash (`[ -f ]`, `kill -0`) and cannot tell
a seeded PID from one the hook wrote itself -- the guard being skipped is
then falsified or confirmed by whether that PID FILE gets overwritten,
which only happens on the launch path, never on the skip path.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from .test_session_start_windows_benchmark_669 import BASH, SESSION_START, _env, _store

pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash on this host (#432)")

HOOK_TIMEOUT = 45
# A "has the background launch had time to happen" settle window -- not a
# race against the deferred phase's own work, since what is being asserted
# is whether session-start-hook.sh's own FOREGROUND dispatch decision wrote
# a new PID file, not whether any backgrounded work finished.
SETTLE_SECONDS = 1.0


def _payload(session_id: str, source: str) -> str:
    return json.dumps({
        "session_id": session_id,
        "transcript_path": f"/does/not/matter/{session_id}.jsonl",
        "hook_event_name": "SessionStart",
        "source": source,
        "cwd": "/does/not/matter",
    })


def _run_session_start(env: dict, session_id: str, source: str):
    return subprocess.run(
        [BASH, SESSION_START.as_posix()],
        input=_payload(session_id, source), env=env,
        capture_output=True, text=True, timeout=HOOK_TIMEOUT, check=False,
    )


def test_a_resume_for_a_session_whose_deferred_phase_is_still_alive_does_not_relaunch_it(tmp_path):
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])

    session_id = "11111111-0000-4000-8000-000000001060"
    pid_file = remember / "tmp" / f"deferred-{session_id}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    # Independent of whether ANY pid-file-writing code exists at all (a
    # pre-fix tree never writes one, which would make "the file still names
    # the sleeper" pass VACUOUSLY -- it was simply never touched): a real
    # deferred-phase launch always writes this record
    # (_remember_write_slug_record, unconditional in the deferred phase),
    # so its absence after the call is direct, mechanism-independent
    # evidence that no second deferred phase ever ran.
    slug_record = remember / "tmp" / "session-slug"
    assert not slug_record.exists(), "precondition: no deferred phase has run for this project yet"

    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        pid_file.write_text(str(sleeper.pid), encoding="utf-8")

        result = _run_session_start(env, session_id, "resume")
        assert result.returncode == 0, result.stderr[-2000:]
        time.sleep(SETTLE_SECONDS)

        # The launch path OVERWRITES this file with `$!` of whatever it just
        # forked (session-start-hook.sh's own new code); the skip path never
        # touches it at all. Still naming the sleeper's PID is therefore
        # direct proof the guard took the skip branch, not an inference from
        # timing.
        assert pid_file.read_text(encoding="utf-8").strip() == str(sleeper.pid), (
            "a resume for a session whose deferred phase is still alive "
            "relaunched it instead of being skipped -- the PID file no "
            f"longer names the still-running sleeper (now: "
            f"{pid_file.read_text(encoding='utf-8')!r})"
        )
        assert not slug_record.exists(), (
            "a resume for a session whose deferred phase is still alive "
            "launched a SECOND deferred phase anyway -- its own unconditional "
            "session-slug record appeared, which could only happen if the "
            "launch path ran (mechanism-independent of the pid-file check "
            "above, which a tree with no guard at all would pass vacuously)"
        )
        assert sleeper.poll() is None, (
            "precondition broke mid-test: the sleeper this test uses as a "
            "stand-in live process exited on its own"
        )
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_a_fresh_sessions_deferred_phase_still_launches_normally(tmp_path):
    """Positive control, paired with the test above: a session with NO
    pre-existing PID file at all (ordinary first start) must still get a
    PID file written for its own deferred phase -- otherwise the "did not
    relaunch" claim above could be passing because the guard, or
    session-start-hook.sh's own dispatch, silently stopped launching
    anything at all, which would be a much worse regression than #1060
    reports.
    """
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])

    session_id = "22222222-0000-4000-8000-000000001060"
    pid_file = remember / "tmp" / f"deferred-{session_id}.pid"
    assert not pid_file.exists(), "precondition: no PID file should exist yet"

    result = _run_session_start(env, session_id, "startup")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 15
    while time.time() < deadline and not pid_file.is_file():
        time.sleep(0.1)
    assert pid_file.is_file(), (
        "positive control: a fresh session with no existing PID file never "
        "got one written -- the harness cannot see a launch happen at all"
    )
    pid_text = pid_file.read_text(encoding="utf-8").strip()
    assert pid_text and pid_text.isdigit(), f"PID file does not hold a PID: {pid_text!r}"


def test_a_resume_for_a_session_whose_pid_file_is_stale_relaunches_it(tmp_path):
    """The guard is stale-safe, not merely a lock: a PID file naming a
    PROCESS THAT HAS EXITED (a crashed or already-finished deferred phase)
    must not permanently block every future start for that session id.
    """
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])

    session_id = "33333333-0000-4000-8000-000000001060"
    pid_file = remember / "tmp" / f"deferred-{session_id}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    dead = subprocess.Popen(["sleep", "0.1"])
    dead.wait(timeout=10)
    assert dead.poll() is not None, "precondition: the stand-in process must have exited"
    pid_file.write_text(str(dead.pid), encoding="utf-8")

    result = _run_session_start(env, session_id, "resume")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 15
    while time.time() < deadline:
        current = pid_file.read_text(encoding="utf-8").strip() if pid_file.is_file() else ""
        if current and current != str(dead.pid):
            break
        time.sleep(0.1)
    else:
        pytest.fail(
            "a resume for a session whose PID file named a DEAD process "
            "never relaunched the deferred phase -- the guard is not "
            f"stale-safe (pid file still: {pid_file.read_text(encoding='utf-8') if pid_file.is_file() else '<missing>'!r})"
        )
