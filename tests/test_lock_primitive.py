"""Tests for scripts/lib-lock.sh — the shared lock primitive (#182).

The rule these enforce: at most one process may hold a given lock at a time,
including when the previous holder died without releasing it.

Why the concurrency tests go to N=10 rather than stopping at 2: the acquisition
this replaced measured 0/40 multi-winner rounds at N=2 and 11/40 at N=8. A
two-process test passes on broken locking, which is exactly how the previous
fix shipped looking correct.

What the concurrency tests measure is OVERLAP, not acquisitions (#293). "How
many processes acquired this lock" is not a property of `lock_acquire`: a
contender the OS schedules after the previous holder released takes the free
lock by plain `mkdir`, which is a second acquisition, correct, and not a race.
Counting it made the assertion a statement about the scheduler that read as a
statement about the lock — the shape that produced #291's misdiagnosis. The
rule above is the one worth having, so it is the one asserted: contenders
record entering and leaving the critical section in one ordered log and the
replay fails if anyone was ever inside while somebody else still was.

No clock is involved, deliberately — see `_overlap_script`. On stock macOS
bash 3.2 the best portable clock is whole seconds, which is coarser than the
intervals, and a disjointness test that cannot resolve an overlap would be
#293 again in a new costume.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process semantics (kill -0, fork races) — the lock is exercised "
    "on ubuntu/macos runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_LOCK_SH = REPO_ROOT / "scripts" / "lib-lock.sh"

ROUNDS = 40


def _run(script: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, **kwargs
    )


def _overlap_script(
    lock_dir: Path,
    events_file: Path,
    n: int,
    hold: str,
    timeout: int,
    stagger: str = "",
) -> str:
    """N processes contend for the same lock; each records entering and leaving
    the critical section, in order, in one shared append log.

    There is no clock in here. That is deliberate, and it is the design.

    The obvious way to prove two holders never coincide is a timestamp at each
    enter and each exit and a pairwise-disjoint check over the intervals. It
    cannot be done honestly on this suite's platforms. On stock macOS — bash
    3.2.57, which is the platform lib-lock.sh exists for — `EPOCHREALTIME` is
    unset and `date +%s%N` prints `1785838792N`, a literal `N`. The best
    portable clock left is whole seconds, and lib-lock.sh's own recorder
    (`_lock_timing_now`) degrades to exactly that there. Whole seconds is
    coarser than every interval being measured, so "disjoint" would decay into
    "indistinguishable" — the same defect #293 reports, wearing a different
    hat: a test that passes because it cannot see the thing it checks. File
    mtimes are no way out either; `-nt` is whole-second granular too (#303).

    Ordering needs no resolution. The appends are O_APPEND, so the order of
    lines in the log is the order of the write syscalls — and that order is
    pinned to the lock by causality rather than by the scheduler:

        `enter` is written *after* lock_acquire returns
        `exit`  is written *before* lock_release is called

    So the recorded interval is contained in the true hold interval and is
    never wider than it. A holder that genuinely released before the next one
    acquired cannot have its `exit` line land after the next holder's `enter`,
    because the release and the acquire sit between those two writes. A false
    overlap is therefore not reachable, and a real overlap is visible at any
    duration — including one shorter than any clock on this platform can name.
    """
    stagger_cmd = f"sleep {stagger}" if stagger else ""
    return f"""
    source {LIB_LOCK_SH}
    contend() {{
        if lock_acquire "{lock_dir}" {timeout}; then
            echo "enter $1" >> "{events_file}"
            sleep {hold}
            echo "exit $1" >> "{events_file}"
            lock_release "{lock_dir}"
        else
            echo "timeout $1" >> "{events_file}"
        fi
    }}
    for i in $(seq 1 {n}); do
        contend "$i" &
        {stagger_cmd}
    done
    wait
    """


def _replay(events_text: str) -> tuple[int, int, list, int]:
    """Walk the log and report how deep the critical section ever got.

    Returns (holds, timeouts, overlaps, malformed). `overlaps` is one entry per
    `enter` that arrived while somebody was still inside, carrying who arrived
    and who was already in — that is the failure this file exists to catch, and
    a bare count of it would not say who to go and look at.
    """
    held: list[str] = []
    overlaps: list[tuple[str, list[str]]] = []
    holds = 0
    timeouts = 0
    malformed = 0
    for line in events_text.splitlines():
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            # An append that did not land whole. Short O_APPEND writes are
            # atomic, so this should never happen — but a torn line makes the
            # replay unreliable in both directions, and the callers below turn
            # it into a loud skip rather than reading depth out of it.
            malformed += 1
            continue
        kind, who = parts
        if kind == "enter":
            if held:
                overlaps.append((who, list(held)))
            held.append(who)
            holds += 1
        elif kind == "exit":
            if who in held:
                held.remove(who)
        elif kind == "timeout":
            timeouts += 1
        else:
            malformed += 1
    return holds, timeouts, overlaps, malformed


# Ten, where the acquisition count this replaced used forty. A round is no
# longer one acquisition: every contender waits its turn, so a round at N=10 is
# ten hand-offs and ten chances to catch two processes inside together. Ten
# rounds is 100 hand-offs at N=10 against the old 40 rounds' 40 acquisitions —
# more contention for the same wall clock, which matters because the waiting
# path is not free (each spin costs a `stat` and a `date`).
OVERLAP_ROUNDS = 10
HOLD_SECONDS = "0.05"
# Generous on purpose. The work in a round is n * HOLD_SECONDS — half a second
# at n=10 — so this is not a deadline anything correct can miss. It is not
# asserted on: this lock has no fairness guarantee, so "every contender got in"
# is not a property to fail on. What is asserted is that somebody always did.
ACQUIRE_TIMEOUT = 30


@pytest.mark.parametrize("n", [2, 4, 8, 10])
def test_no_two_holders_overlap(n, tmp_path):
    """Across OVERLAP_ROUNDS rounds at N processes, never two holders at once.

    This counts *overlap*, not acquisitions (#293). The version it replaces
    tallied how many processes acquired the lock in a round and asserted the
    count was 1 — which is not a property of `lock_acquire`. Its winner
    released after 150ms, so a contender the OS did not schedule until after
    that release acquired the free lock by plain `mkdir`: a legitimate,
    sequential, second acquisition, tallied as a concurrent winner. Measured
    on this laptop at a 0.4s stagger: 4 contenders, 4 winners, no overlap of
    any kind. The assertion was about the scheduler and read as being about
    the lock, which is how it would have produced #291's misdiagnosis a second
    time. The scheduler-free form of that is pinned by
    test_a_sequential_reacquisition_is_not_an_overlap; the concurrent one by
    test_the_staggered_round_from_293_reports_no_overlap.

    Contenders wait (ACQUIRE_TIMEOUT) rather than giving up at timeout 0, so a
    round exercises the hand-off N times instead of once. That is where the
    teeth are: every hand-off is another chance for two to be inside together.
    """
    overlapping_rounds: list[tuple[int, list]] = []
    unmeasurable_rounds = 0
    malformed_total = 0

    for round_no in range(OVERLAP_ROUNDS):
        lock_dir = tmp_path / f"n{n}-r{round_no}.lock"
        events = tmp_path / f"n{n}-r{round_no}.events"
        events.write_text("", encoding="utf-8")

        result = _run(
            _overlap_script(lock_dir, events, n, HOLD_SECONDS, ACQUIRE_TIMEOUT)
        )
        assert result.returncode == 0, result.stderr

        holds, _timeouts, overlaps, malformed = _replay(
            events.read_text(encoding="utf-8")
        )
        malformed_total += malformed
        if overlaps:
            overlapping_rounds.append((round_no, overlaps))

        assert holds >= 1, (
            f"round {round_no} at N={n} had no holder at all — a silently "
            "missed cycle is as bad as a doubled one"
        )
        if holds < 2:
            unmeasurable_rounds += 1

    if malformed_total:
        pytest.skip(
            f"{malformed_total} event lines did not land whole, so the replay "
            f"cannot be trusted in either direction. COVERAGE LOST: mutual "
            f"exclusion at N={n} was NOT checked in this run"
        )

    assert not overlapping_rounds, (
        f"{len(overlapping_rounds)}/{OVERLAP_ROUNDS} rounds at N={n} had two "
        f"processes inside the critical section at once — the lock is not "
        f"mutually exclusive. First: {overlapping_rounds[0]}"
    )

    measurable = OVERLAP_ROUNDS - unmeasurable_rounds
    if measurable * 2 < OVERLAP_ROUNDS:
        pytest.skip(
            f"only {measurable}/{OVERLAP_ROUNDS} rounds at N={n} got two or "
            f"more contenders through the lock, so most rounds could not have "
            f"observed an overlap even if one happened. COVERAGE LOST: this "
            f"run does not support the mutual-exclusion claim"
        )


def test_a_sequential_reacquisition_is_not_an_overlap(tmp_path):
    """Take the lock, release it, take it again. Three times, one process.

    This is the structural half of #293 and it has no scheduler in it at all —
    no background jobs, no sleeps, no stagger. Three acquisitions, three
    releases, strictly in order, so `holds` is 3 on every runner that can run
    bash.

    The assertion this file used to carry — "at most one winner per round" —
    is red on exactly this. Three legitimate sequential acquisitions of a free
    lock is the behaviour `lock_acquire` promises, and counting them was never
    a measurement of mutual exclusion. That is why the measurement changed
    rather than the tolerance: a wider count would have been the same mistake
    with more slack in it.
    """
    lock_dir = tmp_path / "sequential.lock"
    events = tmp_path / "sequential.events"
    events.write_text("", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    for k in 1 2 3; do
        if lock_acquire "{lock_dir}" 0; then
            echo "enter $k" >> "{events}"
            echo "exit $k" >> "{events}"
            lock_release "{lock_dir}"
        else
            echo "timeout $k" >> "{events}"
        fi
    done
    """)
    assert result.returncode == 0, result.stderr

    holds, timeouts, overlaps, malformed = _replay(
        events.read_text(encoding="utf-8")
    )
    assert malformed == 0, "event log did not land whole"
    assert (holds, timeouts) == (3, 0), (
        f"a free lock, taken and released three times in sequence by one "
        f"process, produced {holds} acquisitions and {timeouts} timeouts — "
        "nothing was contending, so this is a defect in the lock"
    )
    assert not overlaps, (
        f"a sequential re-acquisition was read as an overlap: {overlaps} — the "
        "replay is reporting acquisitions, which is the #293 defect itself"
    )


def test_the_staggered_round_from_293_reports_no_overlap(tmp_path):
    """The concurrent shape #293 was filed on, at the stagger it names.

    Four contenders started 0.4s apart, each holding 0.15s at timeout 0. Each
    finds the lock free — the previous holder released 250ms earlier — and
    takes it. Measured here: 4 acquisitions, zero overlap. The old rule reads
    that as a four-way failure of mutual exclusion.

    Unlike the sequential test above, how many contenders get in IS up to the
    scheduler: at timeout 0 a contender that the OS delays into the previous
    holder's 150ms window gives up rather than waiting, and on a loaded runner
    that happens. Observed once in six runs on this laptop, 3 of 4. So the
    count is NOT asserted on — asserting it would rebuild #293's defect inside
    the test written to retire it. What is asserted is the overlap, which is
    scheduler-independent; the count only decides whether this run had anything
    to say, and a run that did not says so out loud.
    """
    n = 4
    lock_dir = tmp_path / "stagger.lock"
    events = tmp_path / "stagger.events"
    events.write_text("", encoding="utf-8")

    result = _run(_overlap_script(lock_dir, events, n, "0.15", 0, stagger="0.4"))
    assert result.returncode == 0, result.stderr

    holds, _timeouts, overlaps, malformed = _replay(
        events.read_text(encoding="utf-8")
    )
    assert malformed == 0, "event log did not land whole"
    assert not overlaps, (
        f"a staggered sequential re-acquisition was read as an overlap: "
        f"{overlaps} — the replay is reporting the scheduler, not the lock"
    )
    if holds < 2:
        pytest.skip(
            f"only {holds}/{n} contenders got the lock, so this run contained "
            f"no re-acquisition to misread. COVERAGE LOST: the concurrent half "
            f"of #293 was NOT exercised (the sequential half above still was)"
        )


def test_stale_lock_from_a_dead_holder_is_taken_over(tmp_path):
    """A lock whose owner died must not block the next process forever."""
    lock_dir = tmp_path / "stale.lock"
    lock_dir.mkdir()
    # 999999 is above the PID ceiling on both macOS (99999) and a default Linux
    # (4194304 is the max, but /proc/sys/kernel/pid_max defaults to 32768), so
    # it names no live process on any runner.
    (lock_dir / "pid").write_text("999999", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a stale lock must be recoverable at timeout 0: {result.stderr}"
    )


def test_stale_takeover_has_exactly_one_winner(tmp_path):
    """The `mv`-based takeover must be single-winner by construction.

    `rm -rf` + retry would let several processes each clear the same dead lock
    and each proceed — the multi-winner cascade this primitive exists to stop.
    """
    for round_no in range(ROUNDS):
        lock_dir = tmp_path / f"steal-{round_no}.lock"
        winners = tmp_path / f"steal-{round_no}.winners"
        winners.write_text("", encoding="utf-8")
        lock_dir.mkdir()
        (lock_dir / "pid").write_text("999999", encoding="utf-8")

        result = _run(f"""
        source {LIB_LOCK_SH}
        contend() {{
            if lock_acquire "{lock_dir}" 0; then
                echo "win $$" >> "{winners}"
                sleep 0.1
                lock_release "{lock_dir}"
            fi
        }}
        for i in $(seq 1 8); do contend & done
        wait
        """)
        assert result.returncode == 0, result.stderr

        won = [w for w in winners.read_text(encoding="utf-8").splitlines() if w]
        assert len(won) == 1, (
            f"round {round_no}: {len(won)} processes took over the same stale lock, "
            "expected exactly 1"
        )


def test_live_holder_is_never_stolen_from(tmp_path):
    """A lock held by a living process must not be taken over."""
    lock_dir = tmp_path / "live.lock"
    result = _run(f"""
    source {LIB_LOCK_SH}
    ( source {LIB_LOCK_SH}; lock_acquire "{lock_dir}" 0 && sleep 1 ) &
    holder=$!
    sleep 0.2
    lock_acquire "{lock_dir}" 0 && echo STOLE || echo REFUSED
    wait $holder
    """)
    assert "REFUSED" in result.stdout, (
        f"acquired a lock held by a live process: {result.stdout} {result.stderr}"
    )


def test_legacy_lock_file_does_not_block_forever(tmp_path):
    """Pre-#182 installs left a regular FILE at the lock path.

    `mkdir` can never succeed against one, so without clearing it every save
    would skip forever after an upgrade — a silent, permanent outage.
    """
    lock_path = tmp_path / "legacy.lock"
    lock_path.write_text("999999\n", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_path}" 0 && echo ACQUIRED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a legacy lock FILE must not permanently block acquisition: {result.stderr}"
    )
    assert lock_path.is_dir()


def test_a_lock_file_naming_a_live_pid_is_debris_too(tmp_path):
    """#898: the pre-#182 file lock (v0.8.8, 2026-07-26) is no longer honoured.

    A holder that old cannot still be running, so a regular file at the lock
    path is cleared like any other non-directory debris, whatever PID it
    names. The live PID here is this test's own, which the old code refused
    to take over.
    """
    lock_path = tmp_path / "legacy-live.lock"
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_path}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a lock FILE must be cleared, not honoured: {result.stdout} {result.stderr}"
    )
    assert lock_path.is_dir()


def test_self_id_differs_between_sibling_subshells(tmp_path):
    """Identity must be per-process even where BASHPID does not exist.

    bash 3.2 (the /bin/bash macOS ships) has no BASHPID, and `$$` is shared by
    every subshell of one shell — so two siblings would claim the same identity
    and `lock_release`'s ownership check could not tell them apart.
    """
    result = _run(f"""
    source {LIB_LOCK_SH}
    ( _lock_self_set; echo "$_LOCK_SELF" ) > "{tmp_path}/a"
    ( _lock_self_set; echo "$_LOCK_SELF" ) > "{tmp_path}/b"
    """)
    assert result.returncode == 0, result.stderr
    a = (tmp_path / "a").read_text().strip()
    b = (tmp_path / "b").read_text().strip()
    assert a and b and a != b, (
        f"two sibling subshells reported the same identity ({a!r}) — a process "
        "that never acquired could release another's lock"
    )


def test_a_pidless_orphan_is_adopted_not_blocked_forever(tmp_path):
    """A holder killed between `mkdir` and writing its pid leaves a lock with
    nothing to judge. Without adoption, nobody can ever acquire it again."""
    lock_dir = tmp_path / "orphan.lock"
    lock_dir.mkdir()

    result = _run(f"""
    source {LIB_LOCK_SH}
    _LOCK_ADOPT_AFTER=0
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo BLOCKED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a pid-less lock directory blocked acquisition forever: {result.stderr}"
    )
    assert (lock_dir / "pid").exists()


def test_a_fresh_pidless_lock_is_not_adopted(tmp_path):
    """The same state means "holder mid-acquisition" for the first moments —
    adopting then would hand the lock to a second process."""
    lock_dir = tmp_path / "fresh.lock"
    lock_dir.mkdir()

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout, (
        f"adopted a lock a holder had just created: {result.stdout} {result.stderr}"
    )


def test_a_live_but_permanently_hung_holder_is_eventually_stolen_from(tmp_path):
    """#1101: a holder that is alive but makes no progress at all (blocked
    on I/O, a wedged pipe) is live by `kill -0` for as long as it blocks --
    observed on #1101 as over two days. Once the lock directory's own age
    exceeds _LOCK_MAX_HOLD_SECONDS, a later acquirer must be able to take it
    over anyway, regardless of liveness."""
    lock_dir = tmp_path / "hung.lock"
    lock_dir.mkdir()
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (lock_dir / "pid").write_text(str(holder.pid), encoding="utf-8")
        # Back-date the directory so it reads as held well past the bound,
        # without the test actually waiting that long.
        old_time = time.time() - 120
        os.utime(lock_dir, (old_time, old_time))

        result = _run(f"""
        source {LIB_LOCK_SH}
        _LOCK_MAX_HOLD_SECONDS=60
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "ACQUIRED" in result.stdout, (
            f"a live-but-hung holder older than the hold bound must be "
            f"reclaimable: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_live_holder_within_the_hold_bound_is_not_stolen_from(tmp_path):
    """Positive control for the test above: a live holder younger than
    _LOCK_MAX_HOLD_SECONDS must still be refused, or the bound would not be
    doing anything -- every live holder would simply be stealable."""
    lock_dir = tmp_path / "fresh-live.lock"
    lock_dir.mkdir()
    holder = subprocess.Popen(["sleep", "5"])
    try:
        (lock_dir / "pid").write_text(str(holder.pid), encoding="utf-8")

        result = _run(f"""
        source {LIB_LOCK_SH}
        _LOCK_MAX_HOLD_SECONDS=3600
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "REFUSED" in result.stdout, (
            f"a live holder well within the hold bound must not be stolen "
            f"from: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_an_unreadable_pid_file_is_refused_quickly_rather_than_hanging(
    tmp_path,
):
    """#1103: a dataless cloud-sync placeholder pid file blocks a plain `cat`
    on the read syscall rather than returning content or an error (observed
    at ~60s). Simulated here with a FIFO that has no writer, which blocks a
    `cat` exactly the same way without depending on an actual sync client.

    A timed-out read must NEVER be treated as grounds to steal the lock, no
    matter how old the lock directory is -- found in this fix's own
    self-review: a single timeout cannot tell a permanently stuck
    placeholder from a live holder's pid file that is merely slow to read
    once, and reclaiming on that guess let two processes believe they held
    the same lock whenever both the first read and the post-rename re-check
    happened to time out the same way. The fix this test pins is narrower
    than a first cut attempted: REFUSED, quickly, every time -- never
    ACQUIRED -- and the whole call must return well inside this test's own
    subprocess timeout, proving the read did not hang either."""
    lock_dir = tmp_path / "dataless.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")
    old_time = time.time() - 120
    os.utime(lock_dir, (old_time, old_time))

    result = _run(
        f"""
        source {LIB_LOCK_SH}
        _LOCK_PID_READ_TIMEOUT=1
        _LOCK_ADOPT_AFTER=60
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """,
        timeout=20,
    )
    assert "REFUSED" in result.stdout, (
        f"an unreadable pid file must never be reclaimed, however old the "
        f"lock directory is -- a timeout is not proof the holder is dead: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert (lock_dir / "pid").exists(), (
        "a refused steal must leave the original pid file in place"
    )


def test_a_permanently_dataless_pid_file_becomes_acquirable_once_old_enough(
    tmp_path,
):
    """Scheduler finding: the test above pins that a SINGLE timed-out read
    must never be grounds to steal -- correct, since one timeout cannot
    tell a merely-slow read from a permanently broken one. But refusing
    EVERY read forever, with no recovery path at all, means a pid file
    that genuinely never finishes reading (the actual #1103 shape -- a
    dataless cloud-sync placeholder whose remote copy is gone for good)
    makes `_lock_try_steal` refuse FOREVER: every later `lock_acquire`
    times out at this exact point and logs "another save holds the lock,
    skipping" permanently, just after 2s instead of ~60s (#1103 was made
    faster, not fixed). This pins the recovery path: once the lock
    directory is at least as old as `_LOCK_MAX_HOLD_SECONDS` AND the pid
    file still refuses to finish reading, a later acquirer takes it over.
    `REMEMBER_LOCK_MAX_HOLD_SECONDS=0` makes any real age qualify, the
    same trick `test_a_live_but_permanently_hung_holder_is_eventually_
    stolen_from` above already uses for the live-holder case.
    """
    lock_dir = tmp_path / "dataless-recoverable.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")
    result = _run(
        f"""
        export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
        export REMEMBER_LOCK_PID_READ_TIMEOUT=1
        source {LIB_LOCK_SH}
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """,
        timeout=15,
    )
    assert "ACQUIRED" in result.stdout, (
        f"a permanently dataless pid file old enough to exceed "
        f"_LOCK_MAX_HOLD_SECONDS must eventually become acquirable -- "
        f"a timeout is not forever proof the holder is alive: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    new_pid = (lock_dir / "pid").read_text(encoding="utf-8").strip()
    assert new_pid.isdigit(), (
        f"the takeover must leave a real, numeric pid behind, not the "
        f"FIFO or something else: {new_pid!r}"
    )


def test_concurrent_takeover_of_a_permanently_dataless_pid_file_has_one_winner(
    tmp_path,
):
    """Safety counterpart to the test above: of N contenders that all see
    the same permanently-stuck, old-enough pid file at once, exactly one
    may win the takeover -- the same single-winner guarantee
    `test_stale_takeover_has_exactly_one_winner` already pins for the
    confirmed-dead-holder case, now exercised for the never-finishes-
    reading case this issue adds.

    The atomic `mv` of the pid file alone is NOT the whole guarantee
    here, found by this exact test during development: a winner's own
    pid write recreates the path an instant after its `mv`, and a
    slower contender's `mv` can then grab that brand-new, healthy pid
    file with nothing yet telling it apart from the stuck placeholder
    it meant to steal -- reproduced directly as 7 of 8 contenders
    "winning" before the fix. `_lock_try_steal` closes that by
    re-reading the CLAIM the rename just produced (never the original
    path again, since the original genuinely never finishes reading):
    a real placeholder still times out identically on that re-read; a
    fresh pid a faster winner already wrote resolves immediately, the
    signal to restore it and refuse. This test is what the single-mv
    comment next to that re-read in lib-lock.sh points back to.
    """
    lock_dir = tmp_path / "dataless-race.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")
    events_file = tmp_path / "events.log"
    n = 8
    result = _run(
        f"""
        export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
        export REMEMBER_LOCK_PID_READ_TIMEOUT=1
        source {LIB_LOCK_SH}
        contend() {{
            if _lock_try_steal "{lock_dir}"; then
                echo "STOLE $1" >> "{events_file}"
            else
                echo "REFUSED $1" >> "{events_file}"
            fi
        }}
        for i in $(seq 1 {n}); do
            contend "$i" &
        done
        wait
        """,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = events_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n, (
        f"expected exactly {n} outcomes, one per contender: {lines!r}"
    )
    wins = [line for line in lines if line.startswith("STOLE")]
    assert len(wins) == 1, (
        f"exactly one of {n} concurrent takeovers of the same stuck pid "
        f"file must win -- got {len(wins)}: {lines!r}"
    )
    new_pid = (lock_dir / "pid").read_text(encoding="utf-8").strip()
    assert new_pid.isdigit(), (
        f"after the race, the pid file must hold a real numeric pid, "
        f"not be left empty or dataless: {new_pid!r}"
    )


def test_a_non_integer_pid_read_timeout_still_bounds_the_read(tmp_path):
    """Scheduler finding: `REMEMBER_LOCK_PID_READ_TIMEOUT=0.5` (or `2s`, or
    any value with a non-digit character) reaches `_max_iters=$((
    _LOCK_PID_READ_TIMEOUT * 20 ))` in the watchdog as a bash arithmetic
    error, which exits that subshell immediately -- before it ever sends
    the SIGTERM that is this whole bound's only way of waking a blocked
    `cat` on a dataless pid file. The read, and the caller's blocking
    `wait` on it, becomes unbounded again: the exact #1103 hang this file
    exists to close, reopened by a single bad env var. Confirmed by direct
    repro before writing this test: both bash 3.2 and a newer bash hang
    past 15s on the pre-fix code for this exact FIFO. A digits-only
    validation with a fallback to the documented default (2) closes it.
    """
    lock_dir = tmp_path / "badtimeout.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")
    result = _run(
        f"""
        export REMEMBER_LOCK_PID_READ_TIMEOUT=0.5
        source {LIB_LOCK_SH}
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        echo RESOLVED=$_LOCK_PID_READ_TIMEOUT
        """,
        timeout=15,
    )
    assert "REFUSED" in result.stdout, (
        f"a non-integer timeout override must still refuse (bounded) "
        f"rather than hang: {result.stdout!r} {result.stderr!r}"
    )
    assert "RESOLVED=2" in result.stdout, (
        f"a non-digit override must fall back to the documented default "
        f"of 2, not silently keep the bad value: {result.stdout!r}"
    )


def test_a_leading_zero_pid_read_timeout_is_read_as_decimal_not_octal(
    tmp_path,
):
    """A sibling trap to the test above: `"08"` is digits-only (passes the
    validation that test pins) but is an INVALID octal literal once bash's
    `$(( ))` arithmetic context sees the leading zero -- the same
    "arithmetic error exits before ever sending SIGTERM" failure, one
    validation layer deeper. `10#$_LOCK_PID_READ_TIMEOUT` forces base 10
    at the one place this value is used in arithmetic, matching the
    `10#$_mtime` convention `_lock_dir_age` already uses for the same
    reason. Confirmed by direct repro that this specific value (not `0.5`
    or `2s`, both already covered above) hangs without that forcing --
    unlike `01` through `07`, `08` and `09` are not valid octal digits at
    all, so this is the smallest leading-zero value that actually
    diverges between "read as decimal" (8) and "rejected by bash's own
    arithmetic as an invalid base-8 literal" (a hard error, not merely a
    different number) -- an earlier draft of this test used `03`, which
    happens to read the same both ways and silently tested nothing.
    """
    lock_dir = tmp_path / "octaltimeout.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")
    result = _run(
        f"""
        export REMEMBER_LOCK_PID_READ_TIMEOUT=08
        source {LIB_LOCK_SH}
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """,
        # 08 resolves to an 8-second read bound, and an acquire can pay it
        # more than once; 15s left no headroom on a loaded macOS runner.
        # The hang this pins is unbounded, so 40s still tells them apart.
        timeout=40,
    )
    assert "REFUSED" in result.stdout, (
        f"a leading-zero timeout value must still refuse (bounded), not "
        f"hang on an invalid-octal arithmetic error: "
        f"{result.stdout!r} {result.stderr!r}"
    )


def test_a_zero_pid_read_timeout_falls_back_to_the_default():
    """Scheduler finding: `0` (and `00`) is digits-only, so the validation
    the two tests above pin lets it through -- but it sets the watchdog's
    own bound to zero iterations, which kills a read not yet done after
    the FIRST poll-sleep (~50ms), indistinguishable from killing a
    holder that was about to finish normally. Worse, the SAME zero bound
    applies to the takeover path's post-rename re-read: a legitimate
    fresh pid write can lose that race to its own zero-length grace
    period, which is a false-timeout report that lets a second
    concurrent contender treat the just-written winner as still stuck
    and take the lock away from it -- two processes then believe they
    hold the same lock. `0`/`00` must fall back to the documented
    default (2) the same as a non-digit value does.
    """
    for bad_value in ("0", "00"):
        result = _run(
            f"""
            export REMEMBER_LOCK_PID_READ_TIMEOUT={bad_value}
            source {LIB_LOCK_SH}
            echo RESOLVED=$_LOCK_PID_READ_TIMEOUT
            """,
            timeout=10,
        )
        assert "RESOLVED=2" in result.stdout, (
            f"REMEMBER_LOCK_PID_READ_TIMEOUT={bad_value!r} must fall back "
            f"to the default of 2, not keep a zero bound: "
            f"{result.stdout!r} {result.stderr!r}"
        )


def test_a_live_holder_is_never_stolen_from_even_when_its_pid_read_times_out(
    tmp_path,
):
    """A REAL live holder whose pid file read happens to be merely slow (not
    permanently stuck) must still be refused rather than stolen from, the
    same as the already-broken-forever case: `_lock_try_steal` refuses
    unconditionally on ANY timed-out read before it ever looks at content,
    so a transient slow read on a genuinely healthy holder is not treated
    any more favourably than a permanently stuck one -- which is also what
    closes the gap a first cut of this fix had (a timeout on the live
    holder's own read being misread as "confirmed dead").

    The pid file is a FIFO with a delayed writer: opening a FIFO for read
    blocks until a writer opens it too (POSIX rendezvous semantics), so the
    read genuinely resolves eventually -- it is not broken, just slower
    than `_LOCK_PID_READ_TIMEOUT`. A first cut of this test instead
    replaced `cat` on PATH with a freshly-written shell script simulating
    the same delay, which hung unreproducibly on a macOS CI runner
    (plausibly Gatekeeper's first-run signature check on a brand-new
    executable -- not confirmed, but not reproducible locally either,
    which is itself consistent with a CI-VM-specific, first-run-only
    cost). A FIFO depends on nothing but kernel pipe semantics and the
    system's own, already-trusted `sh`/`cat`."""
    lock_dir = tmp_path / "slow-live.lock"
    lock_dir.mkdir()
    holder = subprocess.Popen(["sleep", "10"])
    pid_path = lock_dir / "pid"
    os.mkfifo(pid_path)
    writer = subprocess.Popen(
        ["sh", "-c", f'sleep 5; printf "%s" "{holder.pid}" > "{pid_path}"']
    )
    try:
        old_time = time.time() - 120
        os.utime(lock_dir, (old_time, old_time))

        result = _run(
            f"""
            source {LIB_LOCK_SH}
            _LOCK_PID_READ_TIMEOUT=1
            _LOCK_ADOPT_AFTER=5
            lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
            """,
            timeout=20,
        )
        assert "REFUSED" in result.stdout, (
            f"a live holder must never be stolen from merely because its "
            f"pid read was slow: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)
        # Not writer.wait(): if the acquire call refused and exited before
        # the backgrounded `cat` it killed ever completed the FIFO's
        # read-side open(), no read-side open() ever happens again, and the
        # writer's own (blocking) open() for write then hangs forever
        # waiting for a peer that will never arrive. SIGKILL unblocks a
        # process stuck in open() the same way it does for any blocking
        # syscall -- waiting for it to exit on its own is what hung here.
        writer.kill()
        writer.wait(timeout=10)


def test_a_reader_that_ignores_sigterm_is_still_bounded(tmp_path):
    """Self-review follow-up to #1103: this function's own comment asserts
    a SIGTERM to a process blocked in a slow read() unblocks it because no
    handler is installed. A reader that DOES install one and ignores
    SIGTERM -- or one genuinely stuck in the kernel's own uninterruptible
    I/O wait -- left the `kill` a no-op and the unconditional `wait`
    straight after it blocked with no timeout of its own. Confirmed
    directly (not merely reasoned) before this fix: a `cat` shim here that
    traps and ignores TERM made `_lock_read_pid_timeout` -- called with
    `_LOCK_PID_READ_TIMEOUT=1` -- run for as long as the shim's own long
    sleep did, well past a 10s wrapper timeout. The fix must escalate past
    a SIGTERM that did not land rather than wait on it unconditionally."""
    lock_dir = tmp_path / "ignores-term.lock"
    lock_dir.mkdir()
    pid_path = lock_dir / "pid"
    os.mkfifo(pid_path)
    bindir = tmp_path / "bin-sigterm-ignoring"
    bindir.mkdir()
    shim = bindir / "cat"
    shim_body = (
        "#!/bin/sh\n"
        'target="' + str(pid_path) + '"\n'
        'if [ "$1" = "$target" ]; then\n'
        "    trap '' TERM\n"
        "    sleep 30\n"
        "else\n"
        '    exec /bin/cat "$@"\n'
        "fi\n"
    )
    shim.write_text(shim_body, encoding="utf-8")
    shim.chmod(0o755)

    t0 = time.time()
    result = _run(
        f"""
        PATH="{bindir}:$PATH"
        source {LIB_LOCK_SH}
        _LOCK_PID_READ_TIMEOUT=1
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """,
        timeout=10,
    )
    elapsed = time.time() - t0
    assert "REFUSED" in result.stdout, (
        f"a reader that ignores SIGTERM must still be bounded and refused, "
        f"never acquired: {result.stdout!r} {result.stderr!r}"
    )
    assert elapsed < 8, (
        f"a reader that ignores SIGTERM must not be waited on unconditionally "
        f"-- this took {elapsed:.1f}s against a _LOCK_PID_READ_TIMEOUT of 1s "
        f"and a 30s-sleeping shim, meaning the kill/wait path escalated "
        f"rather than blocking on it: {result.stdout!r} {result.stderr!r}"
    )


def test_a_confirmed_empty_and_old_pid_file_is_still_reclaimed(tmp_path):
    """Second-review finding: an earlier cut of the timeout-refusal fix
    collapsed the confirmed-empty-pid branch and the timed-out-read branch
    into one unconditional refusal, which also removed the ONLY path that
    ever reclaims a pid file that exists and is empty forever (a holder
    killed between `mkdir` and writing its pid) -- `_lock_try_adopt`
    explicitly refuses whenever the pid file exists at all, timed out or
    not. A confirmed-by-a-completed-read empty pid is a fact, not a guess
    like a timeout is, so it must still be reclaimable once old enough."""
    lock_dir = tmp_path / "confirmed-empty.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("", encoding="utf-8")
    old_time = time.time() - 120
    os.utime(lock_dir, (old_time, old_time))

    result = _run(f"""
    source {LIB_LOCK_SH}
    _LOCK_ADOPT_AFTER=5
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a confirmed-empty, old pid file must still be reclaimable: "
        f"{result.stdout!r} {result.stderr!r}"
    )


def test_a_confirmed_empty_but_fresh_pid_file_is_not_reclaimed(tmp_path):
    """Positive control for the test above: the same confirmed-empty pid
    file, but too young to be debris, must still refuse -- the original
    holder-mid-acquisition protection this age gate exists for."""
    lock_dir = tmp_path / "confirmed-empty-fresh.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    _LOCK_ADOPT_AFTER=3600
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout, (
        f"a fresh, confirmed-empty pid file must not be reclaimed: "
        f"{result.stdout!r} {result.stderr!r}"
    )


def test_a_non_numeric_and_old_pid_file_is_still_reclaimed(tmp_path):
    """Self-review follow-up to #1101/#1103: `_lock_try_steal` judges a
    non-numeric pid file (e.g. "abc") the same branch as an empty one, but
    an earlier cut cleared `$_pid` to "" right there -- before the
    post-rename re-read that compares against it. The re-read then saw the
    REAL content again ("abc"), compared it to the empty string it could
    never equal, restored the file and refused -- every time, however old
    the directory. A non-numeric pid file could never be reclaimed at all
    (confirmed by reproduction: mkdir, write "abc", backdate the directory,
    `lock_acquire ... 0` returns REFUSED and the pid file is still "abc")."""
    lock_dir = tmp_path / "non-numeric.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("abc", encoding="utf-8")
    old_time = time.time() - 120
    os.utime(lock_dir, (old_time, old_time))

    result = _run(f"""
    source {LIB_LOCK_SH}
    _LOCK_ADOPT_AFTER=5
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a non-numeric, old pid file must be reclaimable -- the same as "
        f"the already-covered empty-pid case: {result.stdout!r} "
        f"{result.stderr!r}"
    )


def test_a_non_numeric_but_fresh_pid_file_is_not_reclaimed(tmp_path):
    """Positive control for the test above: the same non-numeric pid file,
    but too young to be debris, must still refuse -- otherwise the age gate
    above would not be doing anything and every non-numeric pid file would
    be reclaimed on sight, holder-mid-acquisition included."""
    lock_dir = tmp_path / "non-numeric-fresh.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("abc", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    _LOCK_ADOPT_AFTER=3600
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout, (
        f"a fresh, non-numeric pid file must not be reclaimed: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert (lock_dir / "pid").read_text() == "abc", (
        "a refused steal must leave the original, non-numeric pid file "
        "content in place"
    )


def test_release_of_an_owned_lock_still_works_when_mktemp_is_unusable(
    tmp_path,
):
    """Second-review finding: an earlier cut of _lock_read_pid_timeout's
    mktemp-failure fallback refused unconditionally instead of degrading to
    a direct read, which meant an unwritable/full TMPDIR -- an ordinary
    operational problem with nothing to do with #1103's cloud-sync
    placeholders -- made EVERY lock release and steal refuse, including a
    release of a lock the releasing process legitimately owns. That is a
    total lock outage strictly worse than the narrow hang this fix exists
    to bound, and this test pins that releasing your own lock still works
    when TMPDIR cannot be used for scratch space."""
    lock_dir = tmp_path / "mktemp-unusable.lock"
    unusable_tmpdir = tmp_path / "no-mktemp-here"
    unusable_tmpdir.mkdir(mode=0o000)
    try:
        result = _run(
            f"""
            export TMPDIR="{unusable_tmpdir}"
            source {LIB_LOCK_SH}
            lock_acquire "{lock_dir}" 0 || {{ echo ACQUIRE_FAILED; exit 0; }}
            lock_release "{lock_dir}" && echo RELEASED || echo REFUSED
            """
        )
        assert "RELEASED" in result.stdout, (
            f"releasing an owned lock must not fail just because TMPDIR is "
            f"unusable: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        unusable_tmpdir.chmod(0o755)


def test_release_refuses_rather_than_removes_on_a_timed_out_pid_read(tmp_path):
    """Self-review finding: a release whose OWN pid read times out must
    refuse (as it already does on a genuine ownership mismatch), never fall
    through to removing a directory it could not confirm was its own. A
    first cut treated the timeout the same as a legitimately empty pid
    file, which skips the ownership check entirely and deletes
    unconditionally -- reachable through this library's own documented
    `trap 'lock_release "$LOCK_DIR"' EXIT` pattern by any process whose own
    acquire never even succeeded."""
    lock_dir = tmp_path / "other-slow.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")

    result = _run(
        f"""
        source {LIB_LOCK_SH}
        _LOCK_PID_READ_TIMEOUT=1
        lock_release "{lock_dir}" && echo RELEASED || echo REFUSED
        """,
        timeout=20,
    )
    assert "REFUSED" in result.stdout, (
        f"a release that cannot confirm ownership within the timeout must "
        f"refuse, not remove the lock: {result.stdout!r} {result.stderr!r}"
    )
    assert lock_dir.exists(), (
        "a refused release must leave the lock directory in place"
    )


def test_release_does_not_destroy_a_lock_a_concurrent_steal_just_won(tmp_path):
    """Self-review follow-up to #1101/#1103, finding 3: a holder judged
    stale (alive, but held past _LOCK_MAX_HOLD_SECONDS) races its own
    legitimate release against a stealer taking the lock over. The read at
    the top of _lock_release_impl and the rm -rf at the bottom are two
    separate operations; a steal landing in between rewrites the pid file
    to the new owner's, and nothing re-checked that before this fix --
    rm -rf fired unconditionally once the FIRST check passed, deleting the
    new owner's lock. A third process would then recreate the directory
    and two holders would exist.

    Deterministic, not concurrent: `_lock_self_set` is overridden to run a
    real `_lock_try_steal` exactly once, synchronously, inside the first
    call `_lock_release_impl` makes to it -- the precise window between
    release's own pid read and its (fixed) atomic re-verify. The steal
    itself runs as a GENUINELY SEPARATE bash process (a standalone
    script, not a `()` subshell of the same interpreter): an earlier cut
    of this test ran it in a subshell and gave it an identity via
    `${BASHPID:-$$}`, correct only when $BASHPID exists. Without it
    (bash 3.2 -- CI's macos-latest resolves "bash" there), $$ inside a
    subshell reports the SAME pid as the outer shell (this file's own
    top comment explains why: $$ does not update across a subshell,
    only $BASHPID does), so the "stealer" and the release-holder
    computed IDENTICAL identities and the race this test exists to
    force never actually happened -- caught by CI, not locally, where
    "bash" happens to resolve somewhere with $BASHPID. A real separate
    process has its own real $$ on any bash version.
    """
    lock_dir = tmp_path / "race-release-steal.lock"
    lock_dir.mkdir()
    stealer_id_path = tmp_path / "stealer_id.txt"
    steal_rc_path = tmp_path / "steal.rc"
    stealer_script = tmp_path / "stealer.sh"
    stealer_script.write_text(
        f"""#!/bin/bash
source {LIB_LOCK_SH}
_lock_try_steal "{lock_dir}" >/dev/null 2>&1
echo $? > "{steal_rc_path}"
echo "$_LOCK_SELF" > "{stealer_id_path}"
""",
        encoding="utf-8",
    )
    stealer_script.chmod(0o755)
    # REMEMBER_LOCK_MAX_HOLD_SECONDS, the public env knob -- not the
    # internal _LOCK_MAX_HOLD_SECONDS shell variable. The stealer is now a
    # genuinely separate process that sources lib-lock.sh fresh, and that
    # sourcing unconditionally re-derives _LOCK_MAX_HOLD_SECONDS from the
    # env knob (`"${{REMEMBER_LOCK_MAX_HOLD_SECONDS:-1800}}"`), so setting
    # the internal variable in THIS process (as an earlier cut of this
    # test did, correct only for the subshell design it has since moved
    # away from) has no effect on the stealer at all -- it would judge
    # the holder not-yet-stale at the real default of 1800s and legitimately
    # refuse, which is a second, independent way this test silently failed
    # to force the race it exists to test (caught by CI).
    script = f"""
    source {LIB_LOCK_SH}
    export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
    echo "$$" > "{lock_dir}/pid"
    _STEAL_FIRED=0
    _lock_self_set() {{
        _LOCK_SELF="${{BASHPID:-$$}}"
        if [ "$_STEAL_FIRED" != 1 ]; then
            _STEAL_FIRED=1
            "{stealer_script}"
        fi
    }}
    lock_release "{lock_dir}"
    echo "release_rc=$?"
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "release_rc=1" in result.stdout, (
        f"a release racing a concurrent steal that already won must refuse, "
        f"not report success: {result.stdout!r} {result.stderr!r}"
    )
    assert steal_rc_path.read_text().strip() == "0", (
        f"the concurrent steal itself must have won for this to be a "
        f"meaningful test of the race, not a vacuous pass: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert lock_dir.exists(), (
        "the stale holder's release destroyed the lock directory the "
        "concurrent steal had already taken over"
    )
    stealer_id = stealer_id_path.read_text().strip()
    pid_file = lock_dir / "pid"
    pid_now = pid_file.read_text().strip() if pid_file.exists() else "<missing>"
    assert pid_now == stealer_id, (
        f"the pid file no longer holds the stealer's own pid ({stealer_id!r}) "
        f"after the stale holder's release ran -- got {pid_now!r}"
    )


def test_release_of_an_owned_uncontended_lock_still_succeeds(tmp_path):
    """Positive control for the test above: without a concurrent steal, the
    atomic re-verify the fix adds before rm -rf must not turn an ordinary,
    uncontested release into a false refusal."""
    lock_dir = tmp_path / "uncontended.lock"
    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 || {{ echo ACQUIRE_FAILED; exit 0; }}
    lock_release "{lock_dir}" && echo RELEASED || echo REFUSED
    """)
    assert "RELEASED" in result.stdout, (
        f"an ordinary, uncontested release must still succeed: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert not lock_dir.exists(), (
        "an ordinary release must still remove the lock directory"
    )


def test_release_does_not_destroy_a_lock_a_concurrent_adopter_just_won(tmp_path):
    """Self-review (round 2) finding: the finding-3 fix above only re-checks
    against a concurrent STEALER by comparing a content snapshot that can
    never actually change (the rename target only this process ever
    touches), which happens to still work because the rename to
    pid.releasing.* and a stealer's own rename of ${_dir}/pid are racing
    for the SAME source path, and whichever `mv` lands first simply makes
    the other's `mv` fail outright. What that compare does NOT catch is a
    concurrent ADOPTER: _lock_try_adopt fires whenever ${_dir}/pid is
    ABSENT and the directory is old enough -- both true for the entire
    window this release's own rename just opened (a stale-but-alive
    holder's age already exceeds the adopt threshold in any sane
    configuration), and an adopter creates a BRAND NEW ${_dir}/pid from
    nothing, which a compare against our own untouched claim file can
    never see. The fix checks whether ${_dir}/pid exists again instead.

    Deterministic, not concurrent: `mv` is overridden to run a real
    _lock_try_adopt immediately AFTER release's own `mv` of ${_dir}/pid
    actually completes -- the precise moment ${_dir}/pid becomes absent,
    which is exactly the window _lock_try_adopt needs to see. Overriding
    _lock_self_set (as the steal-race test above does) would fire too
    early here: that call happens BEFORE release's own rename, while
    ${_dir}/pid still holds the stale holder's own pid, so an adopter
    running there would simply see the pid file present and correctly
    refuse -- never a meaningful race at all.

    The adopter runs as a GENUINELY SEPARATE bash process, not a `()`
    subshell of the same interpreter, same reasoning as the steal-race
    test above: on a bash without $BASHPID (3.2 -- CI's macos-latest
    resolves "bash" there), a subshell's $$ reports the SAME pid as the
    outer shell, so echoing ${BASHPID:-$$} from inside the subshell and
    separately trusting that it matches whatever _lock_try_adopt itself
    wrote is not guaranteed. A real separate process reads back its own
    $_LOCK_SELF after the call -- guaranteed the same value
    _lock_try_adopt just wrote, since both come from one call in one
    process (caught by CI, not locally).
    """
    lock_dir = tmp_path / "race-release-adopt.lock"
    lock_dir.mkdir()
    adopter_id_path = tmp_path / "adopter_id.txt"
    adopt_rc_path = tmp_path / "adopt.rc"
    adopter_script = tmp_path / "adopter.sh"
    adopter_script.write_text(
        f"""#!/bin/bash
source {LIB_LOCK_SH}
_lock_try_adopt "{lock_dir}" >/dev/null 2>&1
echo $? > "{adopt_rc_path}"
echo "$_LOCK_SELF" > "{adopter_id_path}"
""",
        encoding="utf-8",
    )
    adopter_script.chmod(0o755)
    script = f"""
    source {LIB_LOCK_SH}
    export _LOCK_ADOPT_AFTER=0
    echo "$$" > "{lock_dir}/pid"
    _MV_FIRED=0
    mv() {{
        command mv "$@"
        local _rc=$?
        if [ "$_MV_FIRED" != 1 ] && [ "$1" = "{lock_dir}/pid" ]; then
            _MV_FIRED=1
            "{adopter_script}"
        fi
        return $_rc
    }}
    lock_release "{lock_dir}"
    echo "release_rc=$?"
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "release_rc=1" in result.stdout, (
        f"a release racing a concurrent adopter that already won must "
        f"refuse, not report success: {result.stdout!r} {result.stderr!r}"
    )
    assert adopt_rc_path.read_text().strip() == "0", (
        f"the concurrent adopter itself must have won for this to be a "
        f"meaningful test of the race, not a vacuous pass: "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert lock_dir.exists(), (
        "the stale holder's release destroyed the lock directory the "
        "concurrent adopter had already taken over"
    )
    adopter_id = adopter_id_path.read_text().strip()
    pid_file = lock_dir / "pid"
    pid_now = pid_file.read_text().strip() if pid_file.exists() else "<missing>"
    assert pid_now == adopter_id, (
        f"the pid file no longer holds the adopter's own pid ({adopter_id!r}) "
        f"after the stale holder's release ran -- got {pid_now!r}"
    )


def test_release_failure_in_an_exit_trap_is_survivable(tmp_path):
    """Both callers release from an EXIT trap under `set -e`.

    `lock_release` returning 1 is by design; if that aborts the trap, the temp
    files after it never get cleaned and the script's exit status is silently
    rewritten. This is the shape both scripts use.
    """
    lock_dir = tmp_path / "notours.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")

    result = _run(f"""
    set -e
    source {LIB_LOCK_SH}
    HAVE_LOCK=true
    cleanup() {{
        [ "$HAVE_LOCK" = true ] && {{ lock_release "{lock_dir}" || true; }}
        echo CLEANED_UP
    }}
    trap cleanup EXIT
    exit 0
    """)
    assert result.returncode == 0, (
        f"a refused release rewrote the exit status: rc={result.returncode}"
    )
    assert "CLEANED_UP" in result.stdout, (
        "the trap aborted at the failing release — everything after it, "
        "including temp-file cleanup, never ran"
    )


def test_a_non_directory_at_the_lock_path_does_not_block(tmp_path):
    """A dangling symlink or other debris must not wedge the lock forever."""
    lock_path = tmp_path / "weird.lock"
    lock_path.symlink_to(tmp_path / "does-not-exist")

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_path}" 0 && echo ACQUIRED || echo BLOCKED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a dangling symlink at the lock path blocked acquisition: {result.stderr}"
    )


def test_release_refuses_a_lock_we_do_not_own(tmp_path):
    """Defence against a future caller releasing a lock it never took."""
    lock_dir = tmp_path / "other.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_release "{lock_dir}" && echo RELEASED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout
    assert lock_dir.is_dir(), "a lock owned by another PID must survive"


def test_acquire_waits_and_succeeds_within_timeout(tmp_path):
    """A non-zero timeout queues rather than skipping."""
    lock_dir = tmp_path / "wait.lock"
    result = _run(f"""
    source {LIB_LOCK_SH}
    ( source {LIB_LOCK_SH}; lock_acquire "{lock_dir}" 0 && sleep 0.4 && lock_release "{lock_dir}" ) &
    sleep 0.1
    lock_acquire "{lock_dir}" 5 && echo ACQUIRED || echo TIMEOUT
    wait
    """)
    assert "ACQUIRED" in result.stdout, (
        f"expected to acquire once the holder released: {result.stdout} {result.stderr}"
    )


def test_lock_survives_set_e_in_the_caller(tmp_path):
    """Both scripts run under `set -e`; a losing acquire must not abort them."""
    lock_dir = tmp_path / "sete.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    result = _run(f"""
    set -e
    source {LIB_LOCK_SH}
    if lock_acquire "{lock_dir}" 0; then echo ACQUIRED; else echo SKIPPED; fi
    echo REACHED_END
    """)
    assert result.returncode == 0, result.stderr
    assert "SKIPPED" in result.stdout and "REACHED_END" in result.stdout


# ---------------------------------------------------------------------------
# #198 — _lock_dir_age, and the marker the adopter can strand
#
# The age bug is platform-shaped: `stat -f %m` succeeds on BSD and fails on GNU
# *after printing a filesystem block to stdout*, and the old implementation
# captured both probes through one command substitution. So it is broken on
# Linux and fine on macOS, and a test that only calls the real `stat` proves
# whichever answer the runner happens to hold. These shim `stat` on PATH so
# both platforms' semantics are exercised everywhere.
# ---------------------------------------------------------------------------

_GNU_STAT_SHIM = """#!/bin/sh
# GNU coreutils: `-f` is FILESYSTEM status, so `%m` is read as a filename.
# That file does not exist -> exit 1, but the real path still prints its block.
if [ "$1" = "-c" ]; then
    python3 -c 'import os,sys;print(int(os.stat(sys.argv[1]).st_mtime))' "$3"
    exit 0
fi
if [ "$1" = "-f" ]; then
    printf '  File: "%s"\\n' "$3"
    printf '    ID: 26b9871bc77c8277 Namelen: 255     Type: ext2/ext3\\n'
    echo "stat: cannot read file system information for '%m'" >&2
    exit 1
fi
exit 1
"""

_BSD_STAT_SHIM = """#!/bin/sh
if [ "$1" = "-f" ]; then
    python3 -c 'import os,sys;print(int(os.stat(sys.argv[1]).st_mtime))' "$3"
    exit 0
fi
if [ "$1" = "-c" ]; then
    echo "stat: illegal option -- c" >&2
    exit 1
fi
exit 1
"""


def _stat_shim(tmp_path, flavour: str) -> Path:
    """Install a fake `stat` and return the bin dir to prepend to PATH."""
    bindir = tmp_path / f"bin-{flavour}"
    bindir.mkdir()
    shim = bindir / "stat"
    shim.write_text(
        _GNU_STAT_SHIM if flavour == "gnu" else _BSD_STAT_SHIM, encoding="utf-8"
    )
    shim.chmod(0o755)
    return bindir


def _age_dir(path: Path, seconds: int) -> None:
    """Backdate a directory's mtime. os.utime rather than `touch`, whose
    backdating flags differ between BSD (-t) and GNU (-d @epoch)."""
    now = int(__import__("time").time())
    os.utime(path, (now - seconds, now - seconds))


@pytest.mark.parametrize("flavour", ["gnu", "bsd"])
def test_lock_dir_age_reports_the_real_age_on_either_stat(flavour, tmp_path):
    """The probe that fails must not contaminate the one that succeeds.

    GNU `stat -f %m` writes a filesystem block to stdout *and* exits 1. Captured
    through a single `$(A || B)`, that block is concatenated with the mtime B
    printed, the digits-only guard rejects the result, and the function reports
    0 — "fresh" — for a directory of any age. Every adoption check downstream
    then reads `[ 0 -lt 30 ]` and refuses, forever.
    """
    target = tmp_path / "aged.lock"
    target.mkdir()
    _age_dir(target, 500)

    result = _run(f"""
    PATH="{_stat_shim(tmp_path, flavour)}:$PATH"
    source {LIB_LOCK_SH}
    _lock_dir_age "{target}"
    """)
    assert result.returncode == 0, result.stderr
    age = int(result.stdout.strip())
    assert 480 <= age <= 520, (
        f"{flavour} stat: reported {age}s for a 500s-old directory — an age of 0 "
        "means _lock_try_adopt can never fire at the shipped threshold"
    )


@pytest.mark.parametrize("flavour", ["gnu", "bsd"])
def test_a_pidless_orphan_is_adopted_at_the_shipped_threshold(flavour, tmp_path):
    """The existing adoption test sets _LOCK_ADOPT_AFTER=0, which makes the
    comparison `[ 0 -lt 0 ]` — false — so it passes *through* a broken age
    rather than exercising it. This one leaves the default at 30 and ages the
    directory past it, so the age has to be real for adoption to happen.
    """
    lock_dir = tmp_path / "orphan.lock"
    lock_dir.mkdir()
    _age_dir(lock_dir, 3600)

    result = _run(f"""
    PATH="{_stat_shim(tmp_path, flavour)}:$PATH"
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo BLOCKED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"{flavour} stat: a one-hour-old pid-less orphan blocked acquisition at "
        f"the default threshold — a permanent, silent outage of every save: "
        f"{result.stdout} {result.stderr}"
    )
    assert (lock_dir / "pid").exists()


def test_a_stranded_adopt_marker_does_not_wedge_the_lock_forever(tmp_path):
    """`_lock_try_adopt` takes `${dir}/adopt` as its single-winner marker and
    removes it after writing the pid. An adopter killed between those two steps
    leaves no pid, no claim, and adopt/ present: `_lock_try_steal` returns early
    with nothing to judge, and every later `_lock_try_adopt` fails at `mkdir`.
    That is the same permanent outage adoption exists to answer, one level up.
    """
    lock_dir = tmp_path / "stranded.lock"
    lock_dir.mkdir()
    (lock_dir / "adopt").mkdir()
    _age_dir(lock_dir / "adopt", 3600)
    _age_dir(lock_dir, 3600)

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo BLOCKED
    """)
    assert "ACQUIRED" in result.stdout, (
        f"a stranded adopt marker wedged the lock permanently: "
        f"{result.stdout} {result.stderr}"
    )
    assert (lock_dir / "pid").exists()


# ---------------------------------------------------------------------------
# #1108 -- Ubuntu 26.04 (ubuntu-latest) lost mutual exclusion at rates up to
# 10/10 rounds on `test_no_two_holders_overlap`, never on ubuntu-24.04, macOS
# or Windows in the same CI run (run 42110563194). Hypothesis: that image's
# default coreutils (uutils, Rust) makes `mkdir` report success for a
# directory that already existed -- not reproducible on any platform this
# suite runs on without a shim, since the real `mkdir` everywhere else in
# scope (GNU, BSD, the Windows bash shell) is a single exclusive syscall. The
# shim below forces exactly that shape deterministically, on any platform,
# the same way _stat_shim above forces the GNU/BSD split without needing
# both OSes.
# ---------------------------------------------------------------------------

# `command mkdir` would NOT reach the real binary here: this shim's own
# directory is first on PATH for the very subprocess that runs it, so a
# fresh lookup from inside the shim finds the shim again -- unbounded
# self-recursion until the OS fork limit kills it, with the real mkdir(1)
# never once invoked (confirmed: 2108 nested invocations logged, directory
# never created). Resolving the real binary's absolute path OUTSIDE the
# PATH-overridden subprocess, once, and baking that path into the shim,
# is what avoids it.
_REAL_MKDIR = shutil.which("mkdir") or "/bin/mkdir"

_NONEXCLUSIVE_MKDIR_SHIM = f"""#!/bin/sh
# Simulates a `mkdir` that does not guarantee exclusivity: it calls the real
# mkdir and always reports success regardless of the real outcome, mirroring
# the symptom hypothesised for Ubuntu 26.04's uutils coreutils in #1108 -- a
# caller that trusts this exit code alone cannot tell "I just created this"
# from "this already existed".
{_REAL_MKDIR} "$@" 2>/dev/null
exit 0
"""


def _nonexclusive_mkdir_shim(tmp_path) -> Path:
    """Install the fake `mkdir` above and return the bin dir to prepend to PATH."""
    bindir = tmp_path / "bin-nonexclusive-mkdir"
    bindir.mkdir()
    shim = bindir / "mkdir"
    shim.write_text(_NONEXCLUSIVE_MKDIR_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return bindir


def test_mkdir_shim_harness_detects_a_real_double_entry(tmp_path):
    """Positive control for the fix test below: proof the shim forces the
    shape this file exists to test -- a mkdir that reports success against a
    directory that already exists -- rather than merely failing to observe
    anything. Sequential, not concurrent: nothing here depends on process
    scheduling, because the shimmed mkdir lies about exclusivity
    unconditionally, not only under a race.
    """
    bindir = _nonexclusive_mkdir_shim(tmp_path)
    lock_dir = tmp_path / "trust-only.lock"
    result = _run(f"""
    PATH="{bindir}:$PATH"
    mkdir "{lock_dir}" 2>/dev/null && echo FIRST_MKDIR_OK
    mkdir "{lock_dir}" 2>/dev/null && echo SECOND_MKDIR_OK
    """)
    assert result.returncode == 0, result.stderr
    assert "FIRST_MKDIR_OK" in result.stdout, (
        f"the shim refused even the first, genuine mkdir: {result.stdout} "
        f"{result.stderr}"
    )
    assert "SECOND_MKDIR_OK" in result.stdout, (
        "the shim did not report success for a second mkdir against the "
        "SAME already-existing directory, so this shim is not forcing the "
        f"shape this file exists to test: {result.stdout} {result.stderr}"
    )


def test_mkdir_success_alone_is_not_trusted_1108(tmp_path):
    """#1108: a mkdir that reports success for a directory that already
    exists -- the Ubuntu 26.04 shape -- must not cost mutual exclusion.
    lock_acquire has to confirm it actually won with a second, bash-native
    guard, exactly as _lock_try_adopt already does for the adoption path
    (the noclobber comment above it). That guard is an O_EXCL-style
    open(2) done by bash's own redirection, not an external binary, so it
    does not depend on mkdir -- or any other coreutils command -- being
    exclusive on the host.

    Sequential, not concurrent, and deliberately so: the first lock_acquire
    genuinely creates the directory and never releases it, so the second
    call's mkdir reports success against a lock that is provably still
    held -- a shape that can never be a valid second acquisition, with no
    scheduler race needed to produce it.
    """
    bindir = _nonexclusive_mkdir_shim(tmp_path)
    lock_dir = tmp_path / "shimmed.lock"
    result = _run(f"""
    PATH="{bindir}:$PATH"
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo FIRST_ACQUIRED
    lock_acquire "{lock_dir}" 0 && echo SECOND_ACQUIRED || echo SECOND_REFUSED
    """)
    assert result.returncode == 0, result.stderr
    assert "FIRST_ACQUIRED" in result.stdout, (
        f"the first, genuine acquisition did not succeed: {result.stdout} "
        f"{result.stderr}"
    )
    assert "SECOND_ACQUIRED" not in result.stdout, (
        f"a second lock_acquire succeeded while the first call's own "
        f"process still held the lock, purely because the shimmed mkdir "
        f"reported success for an already-existing directory (the Ubuntu "
        f"26.04 shape hypothesised in #1108): {result.stdout} {result.stderr}"
    )
    assert "SECOND_REFUSED" in result.stdout, (
        f"the second call neither acquired nor refused -- it did not run "
        f"the branch this test exists to check at all: {result.stdout} "
        f"{result.stderr}"
    )


def test_a_fresh_adopt_marker_is_left_alone(tmp_path):
    """The same state means "an adopter is mid-adoption" for the first moments.
    Clearing the marker then would let a second process adopt alongside it."""
    lock_dir = tmp_path / "livemarker.lock"
    lock_dir.mkdir()
    (lock_dir / "adopt").mkdir()
    _age_dir(lock_dir, 3600)

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout, (
        f"adopted past a marker another adopter had just created: "
        f"{result.stdout} {result.stderr}"
    )


def test_an_adopter_refuses_a_pid_that_appears_after_its_guard(tmp_path):
    """The `noclobber` write is what makes adoption safe against a pid that
    lands between the entry guard and the write — most importantly a
    `_lock_try_steal` winner, whose rename leaves `pid` briefly absent.

    Deterministic because `_lock_dir_age` is called *after* the `[ -e pid ]`
    guard, so overriding it to install a pid reproduces exactly that window.
    The adopter must refuse, and must leave the other process's pid intact.

    The concurrent race test below cannot reach this: with the marker already
    present every contender funnels into the `mv`, which only one can win, so
    only one process ever reaches the pid write at all.
    """
    lock_dir = tmp_path / "latepid.lock"
    lock_dir.mkdir()
    _age_dir(lock_dir, 3600)

    result = _run(f"""
    source {LIB_LOCK_SH}
    _lock_dir_age() {{ echo 99999 > "{lock_dir}/pid"; echo 3600; }}
    _lock_try_adopt "{lock_dir}" && echo ADOPTED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout, (
        f"adopted over a pid that appeared after the guard: {result.stdout} "
        f"{result.stderr}"
    )
    assert (lock_dir / "pid").read_text().strip() == "99999", (
        "clobbered another process's pid — this is the interleaving where a "
        "steal winner loses the lock it just took"
    )
    assert not (lock_dir / "adopt").exists(), (
        "left its marker behind after refusing, stranding the lock"
    )


def test_two_adopters_racing_on_stranded_debris_produce_one_winner(tmp_path):
    """Clearing the marker is itself a race: if two adopters both judge it dead
    and both clear it, the second removes the *fresh* marker the first just
    made, and both adopt. The clear has to be a single-winner operation.

    The round number goes into the winner line so this asserts exactly one
    winner per round. `<= ROUNDS` would be satisfied by zero winners — a test
    that passes because nothing works is the shape #198 was reported for.

    What this actually covers is the *clearing* being single-winner, and the
    lock not staying wedged. It does NOT cover the `noclobber` pid write:
    because the marker already exists, every contender fails the opening
    `mkdir` and funnels into the `mv`, which exactly one can win, so only one
    process reaches the pid write. Removing `noclobber` leaves this test green
    over 300 rounds at 8-way contention — verified. The guard is covered by
    the injection test above instead.

    The contenders call `_lock_try_adopt` and NOT `lock_acquire` (#291). That
    is the whole difference between a scheduler-independent assertion and one
    that fails on a loaded runner roughly once in twelve. `lock_acquire` also
    offers the *steal* path, and a winner here holds the lock for zero time:
    it writes its pid and its subshell exits immediately. A contender the OS
    did not schedule until after that exit therefore finds a pid whose owner
    is dead, and takes the lock over — correctly, by the primitive's stated
    contract, and recorded by the old harness as a second winner. Measured on
    a loaded laptop with a 150-200ms stagger: 1 run in 3 reported a doubled
    round, the same `{{'N': 2}}` shape as the CI failure in #291. Nothing in
    the lock was wrong; "one winner per round" was simply never an invariant
    of `lock_acquire` when the winners do not stay alive. It IS an invariant
    of `_lock_try_adopt`, which never consults liveness: once a pid exists the
    entry guard turns every later adopter away. Held at 1 winner per round
    with staggers out to 0.5s — see the companion test below for the
    takeover semantics this deliberately stops asserting over.
    """
    winners = tmp_path / "winners"
    script = f"""
    source {LIB_LOCK_SH}
    contend() {{
        if _lock_try_adopt "$1"; then echo "win $2" >> "{winners}"; fi
    }}
    for i in $(seq 1 {ROUNDS}); do
        D="{tmp_path}/race.$i.lock"
        mkdir -p "$D/adopt"
        python3 -c 'import os,sys,time; t=int(time.time())-3600; os.utime(sys.argv[1],(t,t)); os.utime(sys.argv[2],(t,t))' "$D/adopt" "$D"
        # Two simultaneous, to race on the `mv` that clears the marker, and
        # two deliberately late — the interleaving that broke the old harness
        # in #291, kept so a regression to `lock_acquire` here goes red on
        # purpose rather than once in twelve runs by accident.
        contend "$D" "$i" &
        contend "$D" "$i" &
        ( sleep 0.15; contend "$D" "$i" ) &
        ( sleep 0.20; contend "$D" "$i" ) &
        wait
        rm -rf "$D"
    done
    """
    result = _run(script)
    assert result.returncode == 0, result.stderr
    wins = winners.read_text().split() if winners.exists() else []
    rounds_won = [w for w in wins if w != "win"]
    from collections import Counter
    counts = Counter(rounds_won)
    doubled = {r: c for r, c in counts.items() if c > 1}
    assert not doubled, (
        f"rounds with more than one winner: {doubled} — two adopters cleared the "
        "same stranded marker and both took the lock"
    )
    assert len(counts) == ROUNDS, (
        f"only {len(counts)} of {ROUNDS} rounds were won at all — stranded debris "
        "is still wedging the lock, and a max-one-winner assertion alone would "
        "have called that a pass"
    )


def test_an_adopter_that_exits_leaves_a_lock_the_next_contender_takes_over(tmp_path):
    """The behaviour #291 was reported as a bug, pinned as the contract.

    An adopter writes its pid and exits without releasing. The lock is now
    held by a dead process, which is precisely the stale lock `_lock_try_steal`
    exists to recover — so the next contender takes it, and must.

    This is here because the alternative reading of #291 was "the lock admits
    two writers", and the cost of acting on that reading is high: making
    acquisition refuse this would restore the permanent, silent outage that
    #182 and #198 were both filed for. Two adopters never hold the lock *at
    the same time*; the second one holds it because the first no longer
    exists. If a future change makes this go red, that change is the bug.
    """
    lock_dir = tmp_path / "handover.lock"
    lock_dir.mkdir()
    (lock_dir / "adopt").mkdir()
    _age_dir(lock_dir / "adopt", 3600)
    _age_dir(lock_dir, 3600)

    result = _run(f"""
    source {LIB_LOCK_SH}
    # The adopter runs in a child shell and exits, exactly as the race
    # harness's contenders do — the lock is left held by a dead pid.
    ( lock_acquire "{lock_dir}" 0 && echo FIRST ) &
    wait
    lock_acquire "{lock_dir}" 0 && echo SECOND || echo BLOCKED
    """)
    assert "FIRST" in result.stdout, (
        f"the adopter did not take the stranded lock at all: {result.stdout} "
        f"{result.stderr}"
    )
    assert "SECOND" in result.stdout, (
        f"a lock left held by a dead adopter blocked the next contender — that "
        f"is the permanent outage #198 was filed for: {result.stdout} "
        f"{result.stderr}"
    )


# ---------------------------------------------------------------------------
# #1108 review round 2 -- the fix above left two more spots that trusted a
# write without the same noclobber guard, or fell through without checking
# whether anyone actually won.
# ---------------------------------------------------------------------------


def test_a_stealer_refuses_a_pid_that_appears_after_its_rename_1108(tmp_path):
    """#1108 review: a contender whose own `mkdir` falsely reported success
    (the Ubuntu 26.04 shape this PR assumes) can write its own pid into the
    exact window `_lock_try_steal` opens between renaming the dead holder's
    pid away and writing its own new one. A plain `echo >` there would
    clobber that contender's pid and both processes would believe they hold
    the lock. The steal's final write must be `noclobber`-guarded, exactly
    like `_lock_try_adopt`'s already is, and a stealer that loses this race
    must leave the winner's pid alone -- a failed steal, not a clobber.

    Deterministic, not concurrent: `cat` is overridden to inject the
    intruding pid on its first read of the stealing claim file -- the exact
    point between the rename and the write -- the same trick
    test_an_adopter_refuses_a_pid_that_appears_after_its_guard above already
    uses for the adoption path's analogous window.
    """
    lock_dir = tmp_path / "steal-race.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    _injected=0
    cat() {{
        case "$1" in
            */pid.stealing.*)
                if [ "$_injected" != 1 ]; then
                    _injected=1
                    echo 424242 > "{lock_dir}/pid"
                fi
                ;;
        esac
        command cat "$@"
    }}
    _lock_try_steal "{lock_dir}" && echo STOLE || echo REFUSED
    """)
    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout, (
        f"a losing steal must not clobber the intruder's pid: {result.stdout} "
        f"{result.stderr}"
    )
    assert (lock_dir / "pid").read_text().strip() == "424242", (
        "a losing steal clobbered the pid a different contender had already "
        "written -- the Ubuntu 26.04 shape hypothesised in #1108, where a "
        "lying mkdir lets two processes believe they hold the same lock"
    )
    assert not list(lock_dir.glob("pid.stealing.*")), (
        "a losing steal left its claim file behind, stranding litter"
    )


_READONLY_AFTER_MKDIR_SHIM = f"""#!/bin/sh
{_REAL_MKDIR} "$@"
rc=$?
if [ $rc -eq 0 ] && [ "$1" != "-p" ]; then
    chmod 0500 "$1" 2>/dev/null || true
fi
exit $rc
"""


def _readonly_after_mkdir_shim(tmp_path) -> Path:
    """Install a `mkdir` that locks its own new directory read-only right
    after creating it, so the pid write that follows fails for a reason
    that is NOT another process winning the noclobber race (permission
    denied, not EEXIST) -- nobody else ever writes a pid in this scenario.
    """
    bindir = tmp_path / "bin-readonly-after-mkdir"
    bindir.mkdir()
    shim = bindir / "mkdir"
    shim.write_text(_READONLY_AFTER_MKDIR_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return bindir


def test_a_non_race_pid_write_failure_does_not_strand_an_orphan_1108(tmp_path):
    """#1108 review: a guarded pid write can fail for a reason that has
    nothing to do with another process winning the noclobber race --
    permission denied, a disk quota, a fork limit. If `lock_acquire` treats
    every failed write as "someone else won" it leaves a pid-less directory
    behind that nobody holds and nobody will ever release, blocking every
    later acquirer until the adoption threshold (`_LOCK_ADOPT_AFTER`, 30s
    by default) -- the exact permanent-outage shape #198 and #182 were
    filed for, reintroduced one level up. `lock_acquire` must check whether
    the pid now exists before falling through: if it does not, the write
    failed for a non-race reason and the directory it just created must be
    removed immediately rather than left as an orphan.
    """
    bindir = _readonly_after_mkdir_shim(tmp_path)
    lock_dir = tmp_path / "noacc.lock"
    result = _run(f"""
    PATH="{bindir}:$PATH"
    source {LIB_LOCK_SH}
    lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert result.returncode == 0, result.stderr
    assert "ACQUIRED" not in result.stdout, (
        f"acquired despite a pid write that never happened: {result.stdout} "
        f"{result.stderr}"
    )
    assert not lock_dir.exists(), (
        f"a non-race pid-write failure left a pid-less orphan directory "
        f"behind instead of cleaning it up -- every later acquirer would "
        f"be blocked for the adoption threshold: {result.stdout} "
        f"{result.stderr}"
    )


def _chmod_unreadable_skip_reason() -> str:
    """chmod 000 does not make a file unreadable for root (and the CI
    runners for this suite are never root, but a local run as root would
    silently turn a fast-fail read into a successful one, which is the
    exact distinction these tests exist to pin)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return "chmod 000 does not block root from reading a file"
    return ""


def test_a_fast_failing_pid_read_does_not_steal_from_a_live_holder(tmp_path):
    """#1119, regression in e704ea2 (#1101/#1103): `_lock_read_pid_timeout`
    only reports a failure when the watchdog kills a HUNG read (`wait`
    returning >=128). A `cat` that fails FAST instead (EACCES, EIO, a
    cloud-sync placeholder that errors rather than hangs) returned 0 with
    EMPTY content -- indistinguishable from a genuinely empty-and-old pid
    file -- so `_lock_try_steal` stole the lock from a LIVE holder once the
    lock directory aged past `_LOCK_ADOPT_AFTER` (30s), instead of refusing
    the way a hung read already does. `chmod 000` makes `cat` fail fast
    with EACCES rather than hang, the same shape as the trap.d-logged
    repro for this issue (live `sleep` holder, lock dir 60s old, pid file
    unreadable)."""
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "fast-fail-live.lock"
    lock_dir.mkdir()
    holder = subprocess.Popen(["sleep", "30"])
    pid_file = lock_dir / "pid"
    try:
        pid_file.write_text(str(holder.pid), encoding="utf-8")
        pid_file.chmod(0o000)
        old_time = time.time() - 60
        os.utime(lock_dir, (old_time, old_time))

        result = _run(f"""
        source {LIB_LOCK_SH}
        _LOCK_ADOPT_AFTER=30
        _LOCK_MAX_HOLD_SECONDS=1800
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "REFUSED" in result.stdout, (
            f"a fast-failing pid read must never be treated as a confirmed "
            f"empty pid file -- a live holder's lock was stolen out from "
            f"under it: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        pid_file.chmod(0o644)
        holder.terminate()
        holder.wait(timeout=10)


def test_a_readable_pid_of_a_live_holder_is_the_positive_control_1119(tmp_path):
    """Positive control for the test above, same ages and bounds: with the
    pid file left READABLE, the same live holder must still refuse a
    steal -- proving this harness genuinely exercises the steal-vs-refuse
    decision rather than passing because nothing in the scenario would
    ever have been stolen regardless."""
    lock_dir = tmp_path / "readable-live.lock"
    lock_dir.mkdir()
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (lock_dir / "pid").write_text(str(holder.pid), encoding="utf-8")
        old_time = time.time() - 60
        os.utime(lock_dir, (old_time, old_time))

        result = _run(f"""
        source {LIB_LOCK_SH}
        _LOCK_ADOPT_AFTER=30
        _LOCK_MAX_HOLD_SECONDS=1800
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "REFUSED" in result.stdout, (
            f"a live holder's readable pid must still refuse a steal: "
            f"{result.stdout!r} {result.stderr!r}"
        )
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_a_persistently_unreadable_pid_is_still_recoverable_once_old_enough_1119(
    tmp_path,
):
    """#1119 fix direction: a fast-failing read must be refused like a
    hung one, never treated as confirmed-empty -- but #1103's own
    recovery must still work for a pid file that persistently cannot be
    read: once the lock directory is at least as old as
    `_LOCK_MAX_HOLD_SECONDS`, it is still reclaimable, the same recovery
    path a permanently-hanging read already has
    (test_a_permanently_dataless_pid_file_becomes_acquirable_once_old_enough).
    Without this, refusing a fast-fail read forever would trade a security
    regression for a permanent, unrecoverable outage."""
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "fast-fail-recoverable.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "pid"
    pid_file.write_text("424242", encoding="utf-8")
    pid_file.chmod(0o000)
    try:
        result = _run(f"""
        export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
        source {LIB_LOCK_SH}
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "ACQUIRED" in result.stdout, (
            f"a persistently unreadable pid file old enough to exceed "
            f"_LOCK_MAX_HOLD_SECONDS must still be recoverable -- a fast "
            f"read failure must not be refused FOREVER either: "
            f"{result.stdout!r} {result.stderr!r}"
        )
    finally:
        pid_file.chmod(0o644)


def test_a_fast_failing_pid_read_via_the_mktemp_fallback_also_refuses_1119(
    tmp_path,
):
    """Self-review follow-up to #1119: `_lock_read_pid_timeout`'s own
    mktemp-failure fallback (taken when `mktemp` itself cannot create a
    scratch file -- a full or unwritable TMPDIR) had the EXACT SAME bug the
    main fix just closed, in its own separate branch: it unconditionally
    `return 0`-ed regardless of whether the direct `cat` it falls back to
    actually succeeded, discarding `cat`'s own exit status via `|| true`.
    A fast-failing pid file reached through this corner (TMPDIR unusable
    AND the pid file itself unreadable) would still read as confirmed-
    empty and be stealable from a live holder after only
    `_LOCK_ADOPT_AFTER` (30s) rather than `_LOCK_MAX_HOLD_SECONDS` (1800s).
    Forces the fallback the same way
    test_release_of_an_owned_lock_still_works_when_mktemp_is_unusable
    already does (TMPDIR pointed at a 0o000 directory), combined with the
    same chmod-000 pid file the main #1119 test uses."""
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "mktemp-unusable-fast-fail.lock"
    unusable_tmpdir = tmp_path / "no-mktemp-here-1119"
    unusable_tmpdir.mkdir(mode=0o000)
    lock_dir.mkdir()
    holder = subprocess.Popen(["sleep", "30"])
    pid_file = lock_dir / "pid"
    try:
        pid_file.write_text(str(holder.pid), encoding="utf-8")
        pid_file.chmod(0o000)
        old_time = time.time() - 60
        os.utime(lock_dir, (old_time, old_time))

        result = _run(f"""
        export TMPDIR="{unusable_tmpdir}"
        source {LIB_LOCK_SH}
        _LOCK_ADOPT_AFTER=30
        _LOCK_MAX_HOLD_SECONDS=1800
        lock_acquire "{lock_dir}" 0 && echo ACQUIRED || echo REFUSED
        """)
        assert "REFUSED" in result.stdout, (
            f"a fast-failing pid read reached through the mktemp-failure "
            f"fallback must never be treated as a confirmed empty pid file "
            f"either -- a live holder's lock was stolen out from under it: "
            f"{result.stdout!r} {result.stderr!r}"
        )
    finally:
        pid_file.chmod(0o644)
        unusable_tmpdir.chmod(0o755)
        holder.terminate()
        holder.wait(timeout=10)


def test_a_fast_failing_claim_reread_does_not_steal_a_racing_winners_pid_1122(
    tmp_path,
):
    """#1122, follow-on to #1119/#1120 landing on a DIFFERENT call site in
    the same function. `_lock_try_steal`'s SECOND read -- of `$_claim`, the
    pid file this contender just renamed away from "${_dir}/pid" -- is
    supposed to tell apart "still the same stuck placeholder I saw on my
    first read" (safe to steal) from "a faster contender's own noclobber
    write already recreated \"${_dir}/pid\" in the gap between my dir-age
    check and my own `mv`, and my `mv` grabbed THAT instead" (unsafe --
    hand back, per the existing comment right above this re-read).
    #1119/#1120 made every read failure at this re-read report identically
    whether it is a real timeout or a fast EACCES/EIO/cloud-sync refusal,
    so a fast-failing re-read of a genuine winner's brand-new pid -- which
    can itself happen to be unreadable for a moment, unrelated to whether
    it is actually stuck -- fell through to the noclobber write below and
    clobbered the winner's lock: two simultaneous holders.

    The real race (#1101/#1103's own repro: N=8 concurrent contenders, 7 of
    8 "won") depends on OS scheduling luck too fine-grained to pin
    deterministically in a single-process test. This reproduces the exact
    mechanism instead of the timing by wrapping `_lock_file_inode`, the
    fix's own helper: its FIRST call is the "before" snapshot the fix takes
    right before the `mv` (of whatever is CURRENTLY at "${_dir}/pid", still
    the original stuck placeholder at that point) -- the wrapper records
    the real answer for that call, THEN swaps in a "winner's" pid file as
    a side effect, simulating a faster contender's own win landing in
    exactly the window between this contender's pre-mv snapshot and
    the `mv` that follows it. The `mv` therefore grabs the SWAPPED file,
    not the one the snapshot was taken of -- the real mismatch the fix
    exists to catch. The swapped-in file is ALSO chmod 000, landing on
    this issue's fast-fail shape rather than #1119's hang shape.
    """
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "steal-race.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "pid"
    pid_file.write_text("111111", encoding="utf-8")
    pid_file.chmod(0o000)
    winner_pid_text = "222222"
    try:
        result = _run(
            f"""
            export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
            export REMEMBER_LOCK_PID_READ_TIMEOUT=1
            source {LIB_LOCK_SH}
            eval "$(declare -f _lock_file_inode | sed '1s/^_lock_file_inode ()/_orig_lock_file_inode ()/')"
            # A plain shell variable will NOT do for the once-only guard
            # below: `_lock_file_inode` is always called via command
            # substitution ($(...)) at its real call sites in lib-lock.sh,
            # which forks a subshell, and a variable set inside that
            # subshell is gone the instant the subshell exits -- so
            # `_swap_done=1` would never be visible to the next call and
            # the swap would fire on EVERY call, not just the first. A
            # file on disk survives across subshells; a variable does not.
            _swap_marker="{lock_dir}/.swap-done"
            rm -f "$_swap_marker"
            _lock_file_inode() {{
                local _result
                _result=$(_orig_lock_file_inode "$@")
                if [ ! -e "$_swap_marker" ]; then
                    touch "$_swap_marker"
                    # Simulate a faster contender's own win landing in the
                    # gap between THIS contender's pre-mv snapshot (just
                    # taken, above, of the file as it stood a moment ago)
                    # and the `mv` that is about to run next: a legitimate
                    # winner's pid, which ALSO happens to fail fast to read
                    # right now -- #1122's own shape, not #1119's hang.
                    #
                    # Built via mktemp + mv, never rm-then-create in place:
                    # on a bitmap allocator (ext4's behaviour, observed on
                    # the ubuntu-24.04/py3.9 CI leg -- this exact test went
                    # STOLE there despite being green on this machine's
                    # APFS, 2000/2000 rm+recreate trials showing zero
                    # collisions locally), freeing an inode and immediately
                    # creating a new file in the same directory can hand
                    # back that SAME inode number, with nothing else
                    # allocated in between to prevent it. That collision
                    # makes the real fix's own inode comparison below see
                    # "unchanged" for a file that was, in fact, swapped --
                    # the exact ambiguity this test exists to force, except
                    # forced onto the test's own harness instead of the
                    # code under test. Creating the replacement under a
                    # temp name WHILE the original "pid" file still exists
                    # (not yet unlinked) guarantees the temp file gets an
                    # inode distinct from the original's -- two live files
                    # cannot share one inode -- and `mv` then carries that
                    # already-distinct inode into place atomically, so the
                    # swap is observably different by construction, not by
                    # allocator luck.
                    _winner_tmp=$(mktemp "{lock_dir}/.winner-pid.XXXXXX")
                    printf '%s' "{winner_pid_text}" > "$_winner_tmp"
                    chmod 000 "$_winner_tmp"
                    mv -f "$_winner_tmp" "{lock_dir}/pid"
                fi
                echo "$_result"
            }}
            if _lock_try_steal "{lock_dir}"; then
                echo STOLE
            else
                echo REFUSED
            fi
            """,
            timeout=15,
        )
        assert "REFUSED" in result.stdout, (
            f"a fast-failing re-read of the claim must not be read as "
            f"confirmed-still-stuck when a faster contender's own pid "
            f"(itself also unreadable right now) was swapped in first -- "
            f"stealing here clobbers a real winner's lock, producing two "
            f"holders: {result.stdout!r} {result.stderr!r}"
        )
        assert pid_file.exists(), (
            f"the hand-back must restore A pid file, not leave the lock "
            f"pid-less: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        if pid_file.exists():
            pid_file.chmod(0o644)
    assert pid_file.read_text(encoding="utf-8") == winner_pid_text, (
        f"the winner's own pid must survive untouched, not be overwritten "
        f"by the losing contender: got "
        f"{pid_file.read_text(encoding='utf-8')!r}, expected "
        f"{winner_pid_text!r}: {result.stdout!r} {result.stderr!r}"
    )


def test_a_persistently_unreadable_pid_still_steals_when_rename_does_not_preserve_identity_1124(
    tmp_path,
):
    """#1124: `_lock_file_inode` assumes rename preserves a file's identity.
    Not true everywhere -- FAT/exFAT under Cygwin/MSYS, SMB with
    noserverino, FUSE without use_ino (#1103's own setup). There the #1122
    identity comparison can never match for the SAME file, so a
    persistently-unreadable but genuinely stuck pid file would hand back
    on every steal attempt forever -- #1103's outage, reopened by the
    check meant to prevent a different one.

    Simulated by replacing `_lock_file_inode` with a function returning a
    fresh, never-repeating value on every call, regardless of its
    argument -- a stand-in for a filesystem whose rename never preserves
    identity. `_lock_rename_preserves_identity`'s own self-test uses the
    same function and must see the same unreliability and answer false,
    so `_lock_try_steal` must skip the comparison rather than trust it,
    and still steal.
    """
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "steal-no-identity.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "pid"
    pid_file.write_text("555555", encoding="utf-8")
    pid_file.chmod(0o000)
    try:
        result = _run(
            f"""
            export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
            export REMEMBER_LOCK_PID_READ_TIMEOUT=1
            source {LIB_LOCK_SH}
            _counter_file="{lock_dir}/.inode-counter"
            echo 0 > "$_counter_file"
            _lock_file_inode() {{
                local _n
                _n=$(cat "$_counter_file")
                _n=$((_n + 1))
                echo "$_n" > "$_counter_file"
                echo "$_n"
            }}
            if _lock_try_steal "{lock_dir}"; then
                echo STOLE
            else
                echo REFUSED
            fi
            """,
            timeout=15,
        )
        assert "STOLE" in result.stdout, (
            f"a persistently unreadable, old-enough pid file must still "
            f"be stealable even when this filesystem's identity check "
            f"can never report a match for the same file -- refusing "
            f"forever here reproduces #1103's outage: "
            f"{result.stdout!r} {result.stderr!r}"
        )
    finally:
        if pid_file.exists():
            pid_file.chmod(0o644)
    new_pid = pid_file.read_text(encoding="utf-8").strip()
    assert new_pid.isdigit(), (
        f"after a real steal, the pid file must hold this contender's "
        f"own numeric pid: {new_pid!r}"
    )


def test_identity_probe_does_not_reset_the_lock_directory_staleness_clock_1124(
    tmp_path,
):
    """#1124 self-review (Explore reviewer, finding 1):
    `_lock_rename_preserves_identity`'s own throwaway probe must not live
    inside the lock directory it is answering for -- creating, renaming
    or removing ANY entry in a directory updates that directory's own
    mtime, which is exactly what `_lock_dir_age` reads to decide staleness
    everywhere in this file. A probe inside <dir> would reset the clock on
    every single steal attempt, letting a permanently stuck lock look
    freshly touched forever and never age far enough to be reclaimed --
    the #1101/#1103 outage reopened by the very helper meant to avoid a
    different one. Probing in <dir>'s parent instead must leave <dir>'s
    own mtime untouched.
    """
    lock_dir = tmp_path / "probe-mtime.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")
    old_time = time.time() - 120
    os.utime(lock_dir, (old_time, old_time))
    result = _run(
        f"""
        source {LIB_LOCK_SH}
        _lock_self_set
        echo "before=$(_lock_dir_age "{lock_dir}")"
        _lock_rename_preserves_identity "{lock_dir}" >/dev/null 2>&1
        echo "after=$(_lock_dir_age "{lock_dir}")"
        """,
        timeout=15,
    )
    stdout = result.stdout
    before = int(stdout.split("before=")[1].split()[0])
    after = int(stdout.split("after=")[1].split()[0])
    assert before >= 100, (
        f"the harness itself is broken if the pre-probe age was not "
        f"already ~120s old: {stdout!r} {result.stderr!r}"
    )
    assert after >= 100, (
        f"_lock_rename_preserves_identity's own throwaway probe must not "
        f"touch the LOCK DIRECTORY's mtime -- creating/renaming/removing "
        f"a directory entry updates that directory's own mtime, and "
        f"_lock_dir_age reads exactly that, so probing inside the lock "
        f"directory itself resets the staleness clock every later steal "
        f"attempt on this same lock relies on, letting a permanently "
        f"stuck lock look freshly touched forever (reopens #1101/#1103): "
        f"before={before} after={after} {stdout!r} {result.stderr!r}"
    )


def test_handback_never_clobbers_a_pid_another_process_legitimately_wrote(
    tmp_path,
):
    """Self-review of #1124 (oss:auditor finding): `_lock_handback`'s whole
    purpose is to restore a claim without clobbering a pid some OTHER
    process already wrote to "${_dir}/pid" in the meantime. Exercises the
    noclobber write directly, with readable content AND a destination that
    already holds a different, legitimate pid -- the one branch nothing in
    the original diff's own tests drove down. The write must decline, the
    real pid must survive untouched, and the claim must still be dropped
    rather than left as litter.
    """
    lock_dir = tmp_path / "handback-no-clobber.lock"
    lock_dir.mkdir()
    claim = lock_dir / "pid.stealing.999999"
    claim.write_text("111111", encoding="utf-8")
    (lock_dir / "pid").write_text("222222", encoding="utf-8")
    result = _run(
        f"""
        source {LIB_LOCK_SH}
        _lock_handback "{claim}" "{lock_dir}" "111111"
        """,
        timeout=15,
    )
    assert result.stdout == "", (
        f"unexpected stdout from a hand-back: {result.stdout!r} "
        f"{result.stderr!r}"
    )
    assert (lock_dir / "pid").read_text(encoding="utf-8") == "222222", (
        f"a hand-back must never overwrite a pid another process "
        f"legitimately holds, even when the restoring content is known "
        f"and readable: {(lock_dir / 'pid').read_text(encoding='utf-8')!r}"
    )
    assert not claim.exists(), (
        "a hand-back that correctly declined to restore must still drop "
        "its own claim file rather than leave it as litter"
    )


def test_a_persistently_unreadable_claim_with_no_race_still_steals_1122(
    tmp_path,
):
    """Positive control for the test above, same harness and bounds: with
    no faster contender ever touching the pid file (no `_lock_dir_age`
    override), the re-read of `$_claim` fails the same way the first read
    did -- genuinely, consistently stuck -- and the steal must still
    succeed. Without this, the fix above could satisfy its own assertion
    by refusing every steal unconditionally, which would trade #1122's bug
    for #1119's own already-rejected failure mode (a fast-failing pid file
    becoming permanently unstealable) -- the same control shape as
    test_a_readable_pid_of_a_live_holder_is_the_positive_control_1119,
    proving this harness can show STOLE at all, not just REFUSED
    regardless of what happened."""
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "steal-no-race.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "pid"
    pid_file.write_text("111111", encoding="utf-8")
    pid_file.chmod(0o000)
    try:
        result = _run(
            f"""
            export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
            export REMEMBER_LOCK_PID_READ_TIMEOUT=1
            source {LIB_LOCK_SH}
            if _lock_try_steal "{lock_dir}"; then
                echo STOLE
            else
                echo REFUSED
            fi
            """,
            timeout=15,
        )
        assert "STOLE" in result.stdout, (
            f"a persistently unreadable pid file with no race must still "
            f"be stealable once old enough -- refusing here would trade "
            f"#1122's bug for the permanent-outage mistake #1119 already "
            f"avoided at the first read: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        pid_file.chmod(0o644)
    new_pid = pid_file.read_text(encoding="utf-8").strip()
    assert new_pid.isdigit(), (
        f"after a real steal, the pid file must hold this contender's own "
        f"numeric pid, not be left empty or non-numeric: {new_pid!r}"
    )


def test_a_winner_completing_during_the_first_read_is_not_mistaken_for_the_placeholder_1124(
    tmp_path,
):
    """#1124, follow-on finding against #1123/#1122: the #1122 "before"
    inode snapshot was taken AFTER both the first pid read (line ~484 in
    the pre-#1124 source) had already timed out AND the age check further
    down had also already run -- a window wide enough, since the read's
    own timeout is whole seconds and a rename-plus-rewrite elsewhere is
    microseconds, for another contender to complete an entire steal cycle
    inside it. If that happens, the "before" snapshot is already of the
    winner's own brand-new pid file, not of the stuck placeholder the read
    judged -- and the later #1122 comparison then sees "unchanged" for a
    file that was, in fact, swapped out from under this contender.

    This reproduces the mechanism by wrapping `_lock_read_pid_timeout`,
    the function whose first call IS the read being judged: the wrapper
    runs the real read against the real (stuck, chmod 000) placeholder
    first, getting the correct failure, then -- as a side effect timed to
    land exactly when that read returns, before anything else in
    `_lock_try_steal` runs -- swaps in a winner's own fresh pid file,
    ALSO chmod 000 so the subsequent content re-read of the claim fails
    fast too (landing on #1122's own shape, not #1119's hang shape), and
    forcing the decision down to the inode comparison. Built via mktemp +
    mv, never rm-then-create in place, for the same bitmap-allocator
    reason #1122's own test documents: a distinct temp file guarantees a
    distinct inode before the swap, so the swap is observably different by
    construction, not by allocator luck.

    Pre-#1124, the "before" snapshot captured at this point is already of
    the swapped-in winner's file, so the comparison sees a match and
    steals the winner's live lock -- two holders. Fixed by moving the
    snapshot to the top of `_lock_try_steal`, before the first read is
    even attempted, so it answers for the placeholder the read is about
    to judge, not for whatever happens to be at the path afterward.
    """
    reason = _chmod_unreadable_skip_reason()
    if reason:
        pytest.skip(reason)
    lock_dir = tmp_path / "steal-read-race.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "pid"
    pid_file.write_text("111111", encoding="utf-8")
    pid_file.chmod(0o000)
    winner_pid_text = "333333"
    try:
        result = _run(
            f"""
            export REMEMBER_LOCK_MAX_HOLD_SECONDS=0
            export REMEMBER_LOCK_PID_READ_TIMEOUT=1
            source {LIB_LOCK_SH}
            eval "$(declare -f _lock_read_pid_timeout | sed '1s/^_lock_read_pid_timeout ()/_orig_lock_read_pid_timeout ()/')"
            _swap_marker="{lock_dir}/.swap-done"
            rm -f "$_swap_marker"
            _lock_read_pid_timeout() {{
                _orig_lock_read_pid_timeout "$@"
                _rc=$?
                if [ ! -e "$_swap_marker" ]; then
                    touch "$_swap_marker"
                    _winner_tmp=$(mktemp "{lock_dir}/.winner-pid.XXXXXX")
                    printf '%s' "{winner_pid_text}" > "$_winner_tmp"
                    chmod 000 "$_winner_tmp"
                    mv -f "$_winner_tmp" "{lock_dir}/pid"
                fi
                return $_rc
            }}
            if _lock_try_steal "{lock_dir}"; then
                echo STOLE
            else
                echo REFUSED
            fi
            """,
            timeout=15,
        )
        assert "REFUSED" in result.stdout, (
            f"a winner that completes its own steal-and-rewrite cycle "
            f"while this contender's FIRST read is still in flight must "
            f"not be clobbered just because the 'before' identity snapshot "
            f"was taken late enough to already see the winner's file -- "
            f"that produces two simultaneous holders: "
            f"{result.stdout!r} {result.stderr!r}"
        )
        assert pid_file.exists(), (
            f"the hand-back must restore A pid file, not leave the lock "
            f"pid-less: {result.stdout!r} {result.stderr!r}"
        )
    finally:
        if pid_file.exists():
            pid_file.chmod(0o644)
    assert pid_file.read_text(encoding="utf-8") == winner_pid_text, (
        f"the winner's own pid must survive untouched: got "
        f"{pid_file.read_text(encoding='utf-8')!r}, expected "
        f"{winner_pid_text!r}: {result.stdout!r} {result.stderr!r}"
    )
