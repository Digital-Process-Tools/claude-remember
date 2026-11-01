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

from ._bash_runner import decode_bash_output
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


def _resume_diag(remember, session_id: str, env: dict, sleeper_pid: str) -> str:
    """Diagnostics only (never shipped): the PID marker directory listing, and
    a bash -x re-run of the same resume trimmed to the deferred-phase guard
    (kill -0 / _remember_deferred_already_running), so a Windows PID the
    MSYS/Git-Bash `kill -0` check cannot resolve shows up directly rather
    than as an unexplained relaunch.
    """
    parts = [f"REMEMBER_DIR={env.get('REMEMBER_DIR')!r}", f"sleeper (bash $!) pid={sleeper_pid}"]
    tmp_dir = remember / "tmp"
    try:
        parts.append(f"{tmp_dir} listing: {sorted(p.name for p in tmp_dir.glob('*'))}")
    except OSError as exc:
        parts.append(f"{tmp_dir} listing: could not list ({exc})")
    try:
        trace = subprocess.run(
            [BASH, "-x", SESSION_START.as_posix()],
            input=_payload(session_id, "resume").encode("utf-8"), env=env,
            capture_output=True, timeout=HOOK_TIMEOUT, check=False,
        )
        # Diagnostics-only, so this must never itself crash the test it is
        # explaining: `text=True` decodes with the strict error handler, and
        # a `bash -x` trace of a whole session-start-hook.sh run can carry a
        # byte sequence from this host's locale/filesystem that is not valid
        # UTF-8 (the sibling env-cache diagnostic hit exactly this on
        # macos-latest CI). `decode_bash_output` is the same tolerant decode
        # this repo's own bash-subprocess tests already use.
        all_lines = decode_bash_output(trace.stderr).splitlines()
        trace_lines = [
            l for l in all_lines
            if "deferred" in l or "kill" in l or "_remember_deferred_already_running" in l
        ]
        parts.append(
            f"bash -x re-run of the SAME resume (deferred/kill lines, "
            f"{len(trace_lines)} of {len(all_lines)} total; rc={trace.returncode}; "
            "this SECOND run mutates state again -- diagnostic only, not a "
            "repeat of the assertion above):\n" + "\n".join(trace_lines[:120])
        )
    except (OSError, subprocess.SubprocessError) as exc:
        parts.append(f"bash -x re-run: could not run ({exc})")
    return "\n".join(parts)


def _bash_background_sleeper(env: dict, seconds: int = 60) -> str:
    """Start a long-lived sleeper THE SAME WAY production does -- `&` inside
    an MSYS/Git-Bash process, not a native Popen -- and return the PID
    string `$!` printed for it.

    #1060 CI (windows-latest): the original version of this helper used
    `subprocess.Popen(["sleep", "60"])` directly, handing the guard a native
    Windows PID. In production the deferred phase's own PID marker is always
    `$!` of a job started with `... &` INSIDE session-start-hook.sh's own
    MSYS bash (this file's own module docstring, point 2), and MSYS/Git-
    Bash's `kill -0` -- what `_remember_pid_is_alive` actually calls -- works
    in that same MSYS pid space, which is not guaranteed to be the native
    Windows PID a directly-Popen'd process gets. Starting the sleeper from
    bash itself, through `&`/`$!`, hands the guard the identical kind of PID
    string production writes, on every platform -- a no-op change on macOS/
    Linux, where the two pid spaces always agree, and the fix on Windows,
    where they do not.
    """
    result = subprocess.run(
        [BASH, "-c", f"sleep {seconds} >/dev/null 2>&1 & echo $!; disown"],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    pid = result.stdout.strip()
    assert result.returncode == 0 and pid, (
        f"precondition: could not start a bash-backgrounded sleeper "
        f"(rc={result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return pid


def _bash_pid_is_alive(env: dict, pid: str) -> bool:
    """`kill -0` in the SAME bash/pid space `_bash_background_sleeper`
    started the process in -- the guard's own liveness check, used here only
    to verify the harness's own stand-in process, never the guard itself."""
    result = subprocess.run(
        [BASH, "-c", f"kill -0 {pid} 2>/dev/null"],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )
    return result.returncode == 0


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

    sleeper_pid = _bash_background_sleeper(env, 60)
    try:
        pid_file.write_text(sleeper_pid, encoding="utf-8")

        result = _run_session_start(env, session_id, "resume")
        assert result.returncode == 0, result.stderr[-2000:]
        time.sleep(SETTLE_SECONDS)

        # The launch path OVERWRITES this file with `$!` of whatever it just
        # forked (session-start-hook.sh's own new code); the skip path never
        # touches it at all. Still naming the sleeper's PID is therefore
        # direct proof the guard took the skip branch, not an inference from
        # timing.
        try:
            pid_file_now = pid_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            pid_file_now = None
        assert pid_file_now is not None, (
            "a resume for a session whose deferred phase is still alive "
            f"relaunched it instead of being skipped -- the PID file {pid_file} "
            f"is gone entirely (sleeper pid={sleeper_pid})\n"
            f"{_resume_diag(remember, session_id, env, sleeper_pid)}"
        )
        assert pid_file_now.strip() == sleeper_pid, (
            "a resume for a session whose deferred phase is still alive "
            "relaunched it instead of being skipped -- the PID file no "
            f"longer names the still-running sleeper (now: {pid_file_now!r})\n"
            f"{_resume_diag(remember, session_id, env, sleeper_pid)}"
        )
        assert not slug_record.exists(), (
            "a resume for a session whose deferred phase is still alive "
            "launched a SECOND deferred phase anyway -- its own unconditional "
            "session-slug record appeared, which could only happen if the "
            "launch path ran (mechanism-independent of the pid-file check "
            "above, which a tree with no guard at all would pass vacuously)"
        )
        assert _bash_pid_is_alive(env, sleeper_pid), (
            "precondition broke mid-test: the sleeper this test uses as a "
            "stand-in live process exited on its own"
        )
    finally:
        subprocess.run(
            [BASH, "-c", f"kill {sleeper_pid} 2>/dev/null"],
            env=env, capture_output=True, text=True, timeout=10, check=False,
        )


def test_the_deferred_phases_own_pid_file_is_removed_when_it_finishes(tmp_path):
    """Adjacent finding (self-review on #1060): the PID marker this guard
    relies on was never cleaned up on a normal finish -- an unbounded,
    never-removed file per session id, the same class of #362-style tmp
    leak #1079 fixes elsewhere in this same change. A deferred phase that
    runs to completion (REMEMBER_DEFER=0, so this test does not have to
    poll a background process) must remove its own marker rather than
    leave it behind forever.
    """
    # REMEMBER_DEFER=0 (synchronous inline) never writes a PID file at all
    # -- that write sits only in the backgrounded `else` branch, so this
    # must use the ordinary background path to exercise the cleanup this
    # test is actually about.
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])

    session_id = "44444444-0000-4000-8000-000000001060"
    pid_file = remember / "tmp" / f"deferred-{session_id}.pid"
    # Durable proof the deferred phase actually ran to completion, since
    # the PID file itself is the thing under test and may never be
    # observed present even when the fix is working correctly.
    slug_record = remember / "tmp" / "session-slug"

    result = _run_session_start(env, session_id, "startup")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 15
    while time.time() < deadline and not slug_record.is_file():
        time.sleep(0.1)
    assert slug_record.is_file(), (
        "precondition: the deferred phase never finished -- cannot tell "
        "cleanup from a run that simply never happened"
    )

    # The PID file is removed by an EXIT trap, which fires AFTER the
    # script's main body (including the session-slug write just polled
    # for above) -- there is a real, if normally short, window between
    # "session-slug exists" and "the trap has actually run and removed
    # the PID file". A hard assertion right after the slug poll raced
    # that window and was observed flaking on windows-latest CI (slower
    # process teardown than the slug write itself), failing with the PID
    # file still present even though cleanup does run moments later.
    # Poll the same way the slug_record wait above does, rather than
    # asserting once.
    deadline = time.time() + 15
    while time.time() < deadline and pid_file.exists():
        time.sleep(0.1)
    assert not pid_file.exists(), (
        "a deferred phase that ran to completion left its own PID marker "
        "behind instead of cleaning it up on exit"
    )


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

    # The PID file is a transient claim, not a durable record -- the
    # deferred phase removes its OWN marker on a normal finish (self-review
    # on #1060, tests/test_resume_deferred_stacking_1060.py's own cleanup
    # test), so a trivial fixture can finish and clean up between this
    # poll's checks, leaving the file briefly or never observed even though
    # a launch genuinely happened. `session-slug` is a durable record the
    # deferred phase always writes (_remember_write_slug_record,
    # unconditional) and never removes -- proof a launch occurred that
    # survives the race the pid file alone cannot.
    slug_record = remember / "tmp" / "session-slug"

    result = _run_session_start(env, session_id, "startup")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 15
    while time.time() < deadline and not slug_record.is_file():
        time.sleep(0.1)
    assert slug_record.is_file(), (
        "positive control: a fresh session never produced the deferred "
        "phase's own unconditional slug record -- the harness cannot see "
        "a launch happen at all"
    )


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

    # Same transience caveat as the fresh-session positive control above:
    # the pid file the guard reads can be gone again by the time this polls,
    # once the relaunched deferred phase finishes and cleans up its own
    # marker. `session-slug` is the durable signal a relaunch actually
    # happened.
    slug_record = remember / "tmp" / "session-slug"

    result = _run_session_start(env, session_id, "resume")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 15
    while time.time() < deadline and not slug_record.is_file():
        time.sleep(0.1)
    assert slug_record.is_file(), (
        "a resume for a session whose PID file named a DEAD process never "
        "relaunched the deferred phase -- the guard is not stale-safe"
    )


def test_a_resume_for_a_session_whose_marker_is_old_and_names_a_live_but_unrelated_pid_relaunches_it(tmp_path):
    """#1086 review (item 4): `kill -0` alone cannot tell a dead holder's
    PID from an unrelated LIVE process the OS has since reassigned the
    same PID to ("PID recycling", aggressive on Windows) -- without an age
    bound, a marker like that would make `kill -0` succeed FOREVER, and
    every future resume of that session would silently skip recovery
    indefinitely. Paired with the "still alive" test above (recent marker,
    live PID -> must NOT relaunch): this is the same "live PID" condition
    with an OLD marker, which must relaunch once the marker is older than
    REMEMBER_DEFERRED_PID_MAX_AGE.

    The "unrelated LIVE process" is simulated with this TEST's own pid --
    guaranteed alive for the whole test, and guaranteed to be nothing the
    deferred phase ever started.
    """
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])
    env["REMEMBER_DEFERRED_PID_MAX_AGE"] = "1"

    session_id = "55555555-0000-4000-8000-000000001060"
    pid_file = remember / "tmp" / f"deferred-{session_id}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    old = time.time() - 60
    os.utime(pid_file, (old, old))

    slug_record = remember / "tmp" / "session-slug"

    result = _run_session_start(env, session_id, "resume")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 15
    while time.time() < deadline and not slug_record.is_file():
        time.sleep(0.1)
    assert slug_record.is_file(), (
        "a resume for a session whose marker is OLD and names a LIVE "
        "(but unrelated) pid never relaunched the deferred phase -- "
        "there is no age bound on the PID-recycling guard"
    )

def test_a_real_backgrounded_deferred_phase_blocks_its_own_resume_end_to_end(tmp_path):
    """#1086 review (item 1): every other test in this file pre-seeds the PID
    marker itself (a stand-in process this test starts and controls) -- none
    of them depends on session-start-hook.sh's OWN marker write
    (`printf '%s' "$!" > .../deferred-<id>.pid`) ever having happened for
    real. Self-review on #1086 confirmed this directly: deleting that one
    write from the production script left all five other tests in this file
    green.

    This test drives the whole thing end to end with the REAL production
    code and no test-seeded marker: a first SessionStart backgrounds a REAL
    deferred phase, slowed down deterministically with
    REMEMBER_TEST_DEFER_DELAY (a test-only knob, a no-op in production --
    see capture-gap-check.sh's own comment on it); session-start-hook.sh's
    own write must name that real, still-sleeping deferred phase's pid; a
    second (resume) start for the SAME session issued WHILE it is still
    alive must not relaunch a second one; once it finishes (and cleans up
    its own marker), a THIRD start must launch again.
    """
    home, project, remember = _store(tmp_path)
    env = _env(home, project, remember, os.environ["PATH"])
    # 20s, not a few: #1079's own windows-latest evidence measured process-
    # start overhead alone running into multiple seconds under CI load (the
    # p50 of 8.7s this file's own module docstring area references
    # elsewhere in this repo) -- a short delay here flaked on exactly that
    # CI leg, where the resume's own dispatch did not even reach the first
    # deferred phase until it had ALREADY finished and cleaned up, making a
    # correct THIRD-style relaunch look like a broken guard.
    env["REMEMBER_TEST_DEFER_DELAY"] = "20"

    session_id = "66666666-0000-4000-8000-000000001060"
    pid_file = remember / "tmp" / f"deferred-{session_id}.pid"
    slug_record = remember / "tmp" / "session-slug"
    assert not pid_file.exists(), "precondition: no PID file should exist yet"
    assert not slug_record.exists(), "precondition: no deferred phase has run yet"

    result = _run_session_start(env, session_id, "startup")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 10
    while time.time() < deadline and not pid_file.is_file():
        time.sleep(0.05)
    assert pid_file.is_file(), (
        "a backgrounded SessionStart never wrote its own deferred-phase PID "
        "marker -- the guard this test is about has nothing real to read"
    )
    first_pid = pid_file.read_text(encoding="utf-8").strip()
    assert first_pid, "precondition: the PID marker is empty"

    # Issue a RESUME for the SAME session WHILE the first deferred phase is
    # still inside its own REMEMBER_TEST_DEFER_DELAY sleep.
    result = _run_session_start(env, session_id, "resume")
    assert result.returncode == 0, result.stderr[-2000:]
    time.sleep(SETTLE_SECONDS)

    assert pid_file.is_file(), (
        "the PID marker vanished between the first start and the resume -- "
        "either the first deferred phase finished faster than this test's "
        "own delay accounts for, or something removed it early"
    )
    assert pid_file.read_text(encoding="utf-8").strip() == first_pid, (
        "a resume for a session whose REAL, still-running deferred phase "
        "had not finished relaunched a second one instead of being skipped "
        f"-- the marker no longer names it (was {first_pid!r})"
    )
    assert not slug_record.exists(), (
        "a resume for a session whose real deferred phase was still alive "
        "launched a SECOND one anyway -- its own unconditional session-slug "
        "record appeared"
    )

    # Let the first (real) deferred phase finish and clean up its own
    # marker -- same polling shape as this file's own PID-cleanup test
    # above, for the same CI-flake reason (a real, if normally short,
    # window between the slug write and the EXIT trap actually running).
    deadline = time.time() + 40
    while time.time() < deadline and pid_file.exists():
        time.sleep(0.1)
    assert not pid_file.exists(), (
        "the real, first deferred phase never cleaned up its own PID "
        "marker on a normal finish"
    )
    deadline = time.time() + 15
    while time.time() < deadline and not slug_record.is_file():
        time.sleep(0.05)
    assert slug_record.is_file(), (
        "the real, first deferred phase never finished at all -- cannot "
        "tell a genuine completion from one that simply never ran"
    )

    # Reset the durable completion signal so the THIRD start's own launch is
    # unambiguous: session-slug is overwritten, not accumulated, so its mere
    # presence after the third start would not by itself distinguish a
    # fresh launch from the first one's own leftover record.
    slug_record.unlink()

    result = _run_session_start(env, session_id, "resume")
    assert result.returncode == 0, result.stderr[-2000:]

    deadline = time.time() + 40
    while time.time() < deadline and not slug_record.is_file():
        time.sleep(0.1)
    assert slug_record.is_file(), (
        "a resume for a session whose real deferred phase had already "
        "finished (and cleaned up its own marker) never launched a THIRD "
        "deferred phase"
    )
