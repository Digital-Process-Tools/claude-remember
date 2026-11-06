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


def _run_vcs(args):
    """Thin subprocess wrapper, fixed cwd and capture, shared by every
    ref-resolution helper below."""
    binary = "git"
    return subprocess.run(
        [binary] + args, cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )


def _resolve_main_ref():
    """A PR's own CI checkout is commonly shallow and single-ref -- just
    the merge commit fetched at depth 1, with the default branch's own
    history and tree never fetched at all, so a bare branch name can be
    genuinely unresolvable (or resolvable yet treeless) there. A fresh,
    shallow fetch of the remote tip is the most robust first move: it
    works whether or not the checkout already happened to populate a
    local ref, and every CI runner has the network access to do it
    against the same remote the checkout action itself set up. Same
    convention as test_session_start_budget_rollover_1051.py's own
    helper of this name.
    """
    default_branch = "main"
    remote = "origin"
    fetch = _run_vcs(["fetch", "--quiet", "--depth", "1", remote, default_branch])
    if fetch.returncode == 0:
        return "FETCH_HEAD"
    for candidate in (remote + "/" + default_branch, default_branch, remote + "/HEAD"):
        result = _run_vcs(["rev-parse", "--verify", candidate])
        if result.returncode == 0:
            return candidate
    return None


def _write_ref_file(ref: str, rel_path: str, dest: Path) -> None:
    """Write REF's copy of REL_PATH to DEST, for running the SAME
    scenario against a baseline's actual code rather than a hardcoded
    number either side could drift past."""
    result = _run_vcs(["show", ref + ":" + rel_path])
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    dest.write_text(result.stdout, encoding="utf-8")


def _timed_uncontended_pairs(lib_lock_sh: Path, tmp_dir: Path, n: int) -> float:
    """Average ms per uncontended lock_acquire+lock_release pair, sourcing
    LIB_LOCK_SH specifically -- lets the SAME measurement run against
    this branch's own file and a baseline's, in the same process, same
    interpreter, same machine."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = f"""
    source "{lib_lock_sh}"
    for i in $(seq 1 {n}); do
        D="{tmp_dir}/fast-$i.lock"
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
    return elapsed / n * 1000


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
    # Computed OUTSIDE the f-string, not nested inside one: a conditional
    # expression plus a same-quote-type string literal inside an f-string
    # placeholder is PEP 701 syntax, accepted only by Python >=3.12 --
    # this repo's own CI runs 3.9-3.11 too, and a 3.12-only f-string fails
    # at COLLECTION with "SyntaxError: unterminated string literal",
    # before any test in the file can even run (confirmed by direct repro
    # with py_compile under 3.9/3.10/3.11).
    if expected == 1:
        why = (
            "the bounded pid-file read alone, since $BASHPID makes "
            "_lock_self_set a zero-fork path"
        )
    else:
        why = (
            "the bounded pid-file read plus one per _lock_self_set call "
            "(acquire and release), since this bash has no $BASHPID"
        )
    assert len(cats) == expected, (
        f"an uncontended acquire+release forked cat {len(cats)} times -- "
        f"expected {expected} ({why}): {cats}"
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
    main_ref = _resolve_main_ref()
    if main_ref is None:
        pytest.skip(
            "no local ref for the default branch resolvable in this "
            "checkout -- cannot pin against that code here"
        )
    main_lib = tmp_path / "ref-lib-lock.sh"
    try:
        _write_ref_file(main_ref, "scripts/lib-lock.sh", main_lib)
    except RuntimeError as exc:
        pytest.skip(
            f"ref {main_ref!r} resolved but the lib-lock.sh content "
            f"could not be read there: {exc}"
        )

    N = 10
    base_ms = _timed_uncontended_pairs(main_lib, tmp_path / "base", N)
    branch_ms = _timed_uncontended_pairs(LIB_LOCK_SH, tmp_path / "branch", N)

    assert base_ms > 0, (
        "positive control: the baseline's own uncontended pair measured "
        "~0ms, too fast to compare a budget against"
    )
    factor, slack_ms = 4.0, 25.0
    budget_ms = base_ms * factor + slack_ms
    assert branch_ms <= budget_ms, (
        f"an uncontended lock_acquire+lock_release pair averaged "
        f"{branch_ms:.1f}ms over {N} pairs on THIS branch, vs "
        f"{base_ms:.1f}ms on the baseline (same interpreter, same loop, "
        f"same machine) -- budget was base*{factor}+{slack_ms}ms = "
        f"{budget_ms:.1f}ms."
    )


def _candidate_bashes() -> list[tuple[str, str]]:
    """Every distinct bash worth checking the stderr-silence fix against.

    The notification this fix removes was observed directly to be
    bash-VERSION-dependent, not universal: killing a still-job-tracked,
    multi-statement background subshell (the watchdog's exact shape)
    produced the notification under macOS's shipped /bin/bash (3.2, no
    $BASHPID -- also what CI's macos-latest runner resolves "bash" to,
    confirmed earlier in this same issue's own fork-count tests) but was
    observed NOT to reproduce under a newer homebrew bash on the same
    machine. A dev machine where `BASH` (resolve_bash(), PATH-based)
    happens to resolve to that newer bash would let every assertion below
    pass trivially, for the wrong reason, if it only ever ran against
    `BASH`. Checking both -- `BASH` (whatever this run's "bash" on PATH
    is, covering CI) and literal "/bin/bash" when present (covering a dev
    machine where PATH resolves elsewhere) -- is what the scheduler's own
    "check it on /bin/bash 3.2 and a newer bash" asked for, and is also
    exactly how the positive control test below caught this gap while
    these tests were being written.
    """
    seen: dict[str, str] = {}
    if BASH:
        seen.setdefault(os.path.realpath(BASH), BASH)
    if sys.platform != "win32" and Path("/bin/bash").is_file():
        seen.setdefault(os.path.realpath("/bin/bash"), "/bin/bash")
    return [(path, path) for path in seen.values()]


_BASH_IDS = [label for label, _ in _candidate_bashes()] or ["no-bash"]
_BASH_PARAMS = [path for _, path in _candidate_bashes()] or [None]
_skip_no_bash = pytest.mark.skipif(
    not _candidate_bashes(), reason="no usable bash found -- see BASH skip above",
)
# `os.mkfifo` does not exist on Windows at all (no FIFO special files on
# NTFS) -- caught on CI (windows-latest): the attribute lookup itself
# raises AttributeError before the test body's own logic ever runs, which
# is a platform gap, not the stderr-silence behaviour this test exists to
# pin. `hasattr` rather than `sys.platform == "win32"`: the absence of
# the attribute is the actual fact being skipped on, not a guess about
# which platforms lack it.
_skip_no_mkfifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="os.mkfifo does not exist on this platform (Windows has no "
    "FIFO special files) -- the dataless-pid-file simulation this test "
    "needs cannot be built here",
)


@_skip_no_bash
@pytest.mark.parametrize("bash_path", _BASH_PARAMS, ids=_BASH_IDS)
def test_an_uncontended_lock_cycle_writes_nothing_to_stderr_1101_1103(
    tmp_path, bash_path,
):
    """Scheduler-measured follow-up: nearly every release of an uncontended
    lock printed a bash job-control notification to stderr --
    `scripts/lib-lock.sh: line 181: <pid> Terminated: 15  ( if [ ... ) `
    -- because `_lock_read_pid_timeout`'s watchdog subshell was explicitly
    `kill`ed while still tracked in this shell's own job table. That kill
    is what a hook runner's own stderr would surface on every save; a
    `</dev/null >/dev/null 2>&1` on the subshell itself cannot reach it,
    because the notification is printed by THIS shell's bookkeeping when
    it next notices, not by the subshell being signalled.
    """
    N = 20
    script = f"""
    source "{LIB_LOCK_SH}"
    for i in $(seq 1 {N}); do
        D="{tmp_path}/quiet-$i.lock"
        rm -rf "$D"
        lock_acquire "$D" 0 >/dev/null
        lock_release "$D" >/dev/null
    done
    """
    result = subprocess.run(
        [bash_path, "-c", script],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stderr == "", (
        f"an uncontended lock_acquire+lock_release loop wrote to stderr -- "
        f"this is bash's own job-control notification for the watchdog "
        f"subshell being killed while still in the job table, visible to "
        f"anything that runs a save hook and would otherwise see a clean "
        f"run ({bash_path}): {result.stderr!r}"
    )


@_skip_no_bash
@_skip_no_mkfifo
@pytest.mark.parametrize("bash_path", _BASH_PARAMS, ids=_BASH_IDS)
def test_a_timed_out_pid_read_also_writes_nothing_to_stderr_1101_1103(
    tmp_path, bash_path,
):
    """Same finding, the #1103 escalation path: a dataless pid file (here a
    FIFO with no writer, same simulation `test_an_unreadable_pid_file_is_
    refused_quickly_rather_than_hanging` in test_lock_primitive.py uses)
    forces the watchdog to actually fire its own kill of `cat`, racing the
    explicit kill of the watchdog itself -- confirmed by direct repro to
    produce the identical stderr notification as the fast path above, not
    a different one tied to killing `cat`.
    """
    lock_dir = tmp_path / "dataless-quiet.lock"
    lock_dir.mkdir()
    os.mkfifo(lock_dir / "pid")
    script = f"""
    source "{LIB_LOCK_SH}"
    _LOCK_PID_READ_TIMEOUT=1
    lock_acquire "{lock_dir}" 0 >/dev/null
    """
    result = subprocess.run(
        [bash_path, "-c", script],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.stderr == "", (
        f"a timed-out pid-file read wrote to stderr ({bash_path}): "
        f"{result.stderr!r}"
    )


@_skip_no_bash
@pytest.mark.parametrize("bash_path", _BASH_PARAMS, ids=_BASH_IDS)
def test_the_stderr_assertions_above_would_catch_a_real_regression_1101_1103(
    bash_path,
):
    """Positive control for the two tests above: proves the harness can
    actually SEE bash's own job-control notification in the first place,
    using the identical shape the real watchdog has (a backgrounded
    subshell running MULTIPLE statements, including a sleep, explicitly
    killed while still tracked in the job table) -- without this, a
    harness that silently failed to capture stderr at all would let the
    two "writes nothing" tests above pass for the wrong reason.

    This control is itself what caught the bash-version dependency
    documented on `_candidate_bashes` above: a single-simple-command
    subshell (`( sleep 1 ) &`) does NOT reproduce the notification at all
    (bash's exec-optimization means `$!` there is the real `sleep`
    itself, not a wrapper), and a lone kill-and-echo of the multi-statement
    shape, run only once, also failed to reproduce it reliably even on
    macOS's shipped /bin/bash -- repeating the pattern in a loop (as the
    real fast-path repro and this file's own two tests above already do,
    both using a `for` loop over many lock cycles) is what made it
    reliable, confirmed by direct repro before writing this test. A newer
    homebrew bash on the same dev machine that found this bug never
    printed the notification for this script even looped. A bash that
    cannot demonstrate the notification here is SKIPPED rather than
    failed: the two "writes nothing" tests for that same bash are then
    true but vacuous, since there is nothing they could have caught on a
    bash that never made this noise regardless.
    """
    script = """
    for i in $(seq 1 5); do
        (
            x=1
            while :; do
                sleep 0.2
                x=$(( x + 1 ))
            done
        ) </dev/null >/dev/null 2>&1 &
        p=$!
        sleep 0.05
        kill "$p" 2>/dev/null
    done
    echo after_loop
    """
    result = subprocess.run(
        [bash_path, "-c", script],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.stderr == "":
        pytest.skip(
            f"{bash_path} never prints bash's own job-control notification "
            f"for a killed, non-disowned multi-statement background "
            f"subshell -- the 'writes nothing to stderr' tests above are "
            f"true but vacuous on this bash, confirmed observationally, "
            f"not assumed"
        )
