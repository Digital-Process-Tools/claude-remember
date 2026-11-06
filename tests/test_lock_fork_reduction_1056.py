"""lib-lock.sh's _lock_acquire_impl forked `dirname` and `date` on every
acquisition, even the uncontended fast path where the first `mkdir` just
succeeds and neither value is ever used (#1056 self-review finding).

Under #1056's own scenario -- many sessions ending together, each one
taking save.lock/consolidation.lock/staging.lock/the NDC commit lock at
least once per flush -- these are two more forks per lock, per flush,
competing for the same exhausted MSYS fork table as the six `dirname
"$0"` calls this issue's own fix already removed from save-session.sh's
sourcing preamble. Same bug class, same hot path, missed in the first
pass.

TDD shape: a positive control (the shim harness can see `dirname`/`date`
forks when something still makes them) paired with the actual assertion
that an uncontended lock_acquire call makes neither.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash
from spawn_counting import make_shim_dir, spawns

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash found -- this drives the real lib-lock.sh",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_LOCK_SH = REPO_ROOT / "scripts" / "lib-lock.sh"

# Coreutils spawns are invisible to the shim dir on native Windows the same
# way tests/test_session_start_spawn_reduction_679.py already documents --
# Git/bin/bash.exe prepends /usr/bin ahead of the shim dir, so dirname/date
# resolve to the real binaries first and nothing is logged.
_needs_coreutils_shims = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Git/bin/bash.exe prepends /usr/bin ahead of the shim dir, so "
    "coreutils spawns are not observable on Windows (#679) -- not covered "
    "there, same gap as test_save_session_spawn_reduction_1056.py",
)


def _uncontended_acquire_spawns(tmp_path: Path) -> list[str]:
    log = tmp_path / "spawn.log"
    shims = make_shim_dir(tmp_path, log)
    lock_dir = tmp_path / "store" / "tmp" / "some.lock"
    script = (
        f'source "{LIB_LOCK_SH}" && '
        f'lock_acquire "{lock_dir}" 5 && lock_release "{lock_dir}"'
    )
    result = subprocess.run(
        [BASH, "-c", script],
        env={**os.environ, "SPAWN_LOG": str(log),
             "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return spawns(log)


@_needs_coreutils_shims
def test_positive_control_shim_sees_dirname_and_date_when_something_forks_them(tmp_path):
    log = tmp_path / "probe.log"
    shims = make_shim_dir(tmp_path, log)
    result = __import__("subprocess").run(
        [BASH, "-c", 'dirname "$1" >/dev/null; date +%s >/dev/null', "_", "/a/b/c"],
        env={**os.environ, "SPAWN_LOG": str(log),
             "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0
    recorded = spawns(log)
    assert any(s.startswith("dirname ") for s in recorded) and any(
        s.startswith("date ") for s in recorded
    ), f"positive control failed -- harness never saw either call: {recorded}"


@_needs_coreutils_shims
def test_uncontended_lock_acquire_forks_neither_dirname_nor_date_1056(tmp_path):
    recorded = _uncontended_acquire_spawns(tmp_path)
    offending = [s for s in recorded if s.startswith(("dirname ", "date "))]
    assert not offending, (
        f"an uncontended lock_acquire (first mkdir succeeds) still forks "
        f"dirname and/or date for values nothing ends up reading: {offending}"
    )


def _bash_has_bashpid() -> bool:
    """Whether BASH is new enough to have $BASHPID.

    _lock_self_set takes a zero-fork fast path when $BASHPID is set, and a
    slow path (mktemp + a forked `sh` + a forked `cat` to read back what
    the `sh` wrote) when it is not -- bash 3.2, the version Apple still
    ships as /bin/bash, never defines it (#182's own top-of-file comment).
    Whichever `bash` this test suite's own BASH resolved to may or may not
    be that one, platform and PATH both deciding it -- CI's macos-latest
    runner resolved "bash" to /bin/bash and took the slow path even
    though this exact test was written and first passed against a
    homebrew bash on a dev machine where "bash" resolves elsewhere and
    has $BASHPID (#1101/#1103 follow-up, caught by CI, not locally).
    """
    result = subprocess.run(
        [BASH, "-c", 'echo "${BASHPID:+yes}"'],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result.stdout.strip() == "yes"


@_needs_coreutils_shims
def test_uncontended_acquire_release_forks_cat_exactly_once_1101_1103(tmp_path):
    """Self-review follow-up to #1101/#1103, finding 4: _lock_release_impl's
    read of its OWN pid file always went through the full timed-read
    machinery (mktemp + a backgrounded cat + a SECOND cat draining the
    scratch file + rm), even on the fast, uncontended release every
    PostToolUse save takes. The drain step forked cat a second time for no
    reason: the scratch file it reads is one this same function just wrote
    moments ago via mktemp, never the caller's own (possibly dataless)
    file the bound exists to guard, so a bash builtin read ($(<file)) is
    exactly as safe there and costs one fork less.

    The expected count is PLATFORM-DEPENDENT, not a flat 1 -- caught by CI
    after an earlier cut of this test hardcoded 1 and went red on
    macos-latest, which resolves "bash" to /bin/bash (3.2, no $BASHPID).
    _lock_self_set is called once by acquire (on the winning `mkdir`) and
    once by release (for its ownership check); each of those calls forks
    its OWN `cat` on a bash without $BASHPID (reading back what a forked
    `sh -c 'echo $PPID'` wrote), two calls nothing in THIS test is
    exercising or trying to fix. An uncontended acquire+release round
    trip (the first mkdir just succeeds, so _lock_try_steal is never
    reached) calls the bounded pid-file read exactly once -- from release
    -- contributing exactly one more cat fork on top of whatever
    _lock_self_set itself costs on this platform.
    """
    recorded = _uncontended_acquire_spawns(tmp_path)
    cats = [s for s in recorded if s.startswith("cat ")]
    expected = 1 if _bash_has_bashpid() else 3
    assert len(cats) == expected, (
        f"an uncontended acquire+release forked cat {len(cats)} times -- "
        f"expected {expected} ({'the bounded pid-file read alone, since '
        '$BASHPID makes _lock_self_set a zero-fork path' if expected == 1 else
        'the bounded pid-file read plus one per _lock_self_set call (acquire '
        'and release), since this bash has no $BASHPID'}): {cats}"
    )


def test_uncontended_acquire_release_does_not_pay_the_timeout_bound_1101_1103(
    tmp_path,
):
    """Scheduler-measured follow-up to the two tests above: a fork-count
    assertion alone missed a real regression, because the cost it never
    saw was never a fork -- it was a FORCED minimum sleep. Measured
    directly against this branch before this fix: 20 uncontended
    lock_acquire+lock_release pairs averaged ~70-115ms EACH (machine- and
    pipe-capture-dependent -- see the comment in _lock_read_pid_timeout on
    why), against ~9-25ms on origin/main with no bound at all. The fix
    replaced an iteration-count poll loop (which must sleep at least once
    before its first `kill -0` can ever see the reader as exited, because
    a not-yet-reaped zombie still reports "alive") with a background
    watchdog that owns the timeout entirely, so the main thread pays only
    a single blocking `wait` that returns the instant the reader actually
    exits.

    N=10 repeated pairs, averaged, to damp single-run noise; the
    threshold is wide enough to tolerate a loaded CI runner but narrow
    enough that a regression back to the old poll-loop shape (or the
    FD-inheritance bug the watchdog fix itself briefly introduced, see
    the same comment) fails this loudly rather than silently passing
    under a bound nobody would ever notice.
    """
    N = 10
    script = f"""
    source "{LIB_LOCK_SH}"
    for i in $(seq 1 {N}); do
        D="{tmp_path}/fast-$i.lock"
        rm -rf "$D"
        lock_acquire "$D" 0 >/dev/null
        lock_release "$D" >/dev/null
    done
    """
    t0 = time.time()
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True, text=True, timeout=30, check=False,
    )
    elapsed = time.time() - t0
    assert result.returncode == 0, result.stderr[-2000:]
    per_pair_ms = elapsed / N * 1000
    assert per_pair_ms < 50, (
        f"an uncontended lock_acquire+lock_release pair averaged "
        f"{per_pair_ms:.1f}ms over {N} pairs -- expected close to main's "
        f"own ~10-25ms, well under the ~70-115ms this pins as a "
        f"regression (the bounded pid-file read's own timeout machinery "
        f"must not be paid on the fast, uncontended path): "
        f"{result.stdout!r} {result.stderr!r}"
    )
