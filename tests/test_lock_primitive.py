"""Tests for scripts/lib-lock.sh — the shared lock primitive (#182).

The rule these enforce: at most one process may hold a given lock at a time,
including when the previous holder died without releasing it.

Why the concurrency tests go to N=10 rather than stopping at 2: the acquisition
this replaced measured 0/40 multi-winner rounds at N=2 and 11/40 at N=8. A
two-process test passes on broken locking, which is exactly how the previous
fix shipped looking correct.
"""

from __future__ import annotations

import os
import subprocess
import sys
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


def _race_script(lock_dir: Path, winners_file: Path, n: int) -> str:
    """N processes contend for the same lock; each winner appends one line.

    The critical section is deliberately not instantaneous — a lock that only
    looks correct because the section is too short to overlap proves nothing.
    """
    return f"""
    source {LIB_LOCK_SH}
    contend() {{
        if lock_acquire "{lock_dir}" 0; then
            echo "win $$" >> "{winners_file}"
            sleep 0.15
            lock_release "{lock_dir}"
        fi
    }}
    for i in $(seq 1 {n}); do contend & done
    wait
    """


@pytest.mark.parametrize("n", [2, 4, 8, 10])
def test_at_most_one_winner_per_round(n, tmp_path):
    """Across {ROUNDS} rounds at N processes, never two holders at once."""
    multi_winner_rounds = 0
    no_winner_rounds = 0

    for round_no in range(ROUNDS):
        lock_dir = tmp_path / f"n{n}-r{round_no}.lock"
        winners = tmp_path / f"n{n}-r{round_no}.winners"
        winners.write_text("", encoding="utf-8")

        result = _run(_race_script(lock_dir, winners, n))
        assert result.returncode == 0, result.stderr

        won = [line for line in winners.read_text(encoding="utf-8").splitlines() if line]
        if len(won) > 1:
            multi_winner_rounds += 1
        if not won:
            no_winner_rounds += 1

    assert multi_winner_rounds == 0, (
        f"{multi_winner_rounds}/{ROUNDS} rounds at N={n} had more than one holder — "
        "the lock is not mutually exclusive"
    )
    assert no_winner_rounds == 0, (
        f"{no_winner_rounds}/{ROUNDS} rounds at N={n} had no winner at all — "
        "a silently missed cycle is as bad as a double one"
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

    `mkdir` can never succeed against one, so without the migration every save
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


def test_legacy_lock_file_with_a_live_holder_is_honoured(tmp_path):
    """The pre-upgrade holder may still be running across the upgrade.

    Deleting its lock because the format changed would start a second save
    alongside it — the exact concurrency the lock exists to prevent, handed out
    once per upgrade.
    """
    lock_path = tmp_path / "legacy-live.lock"
    lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    result = _run(f"""
    source {LIB_LOCK_SH}
    lock_acquire "{lock_path}" 0 && echo ACQUIRED || echo REFUSED
    """)
    assert "REFUSED" in result.stdout, (
        f"took a legacy lock from a live holder: {result.stdout} {result.stderr}"
    )
    assert lock_path.is_file(), "the live holder's lock file must survive"


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
