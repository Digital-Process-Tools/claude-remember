"""#1011 round 2: the below-threshold no-save path regressed in TOTAL process
creation (forks included) while tests/spawn_counting.py -- which counts only
exec'd binaries by name -- and this range's own changelog/docs kept reporting
an improvement, because both were counting a different thing than "how many
processes exist afterward." A bash subshell fork for `$(...)` or a pipe `|`
is a real process -- a real kernel object on Windows 11 25H2 per #1011's own
report -- even when it never execs a separate binary tests/spawn_counting.py
could shim and see.

This file counts the thing #1011 actually cares about: TOTAL processes
created per hook call, forks included, not just execs. On Linux, where
`strace -f` is ordinarily available, `-e trace=clone,fork,vfork` gives an
exact count with no system-noise contamination (strace only sees syscalls
made by the traced process tree, not unrelated processes on the host) --
this is the SAME class of measurement the release auditor used to catch the
original regression (two tree snapshots, PID-delta, minimum of 9 runs), but
strace's syscall-level view is immune to the PID-delta method's own
weakness: on a host running other, unrelated work concurrently (observed:
this repo's own CI runners are single-job, but a contributor's laptop with
many other processes is not), system-wide PID allocation is noisy enough
that a PID-delta minimum can still sit tens of processes above the true
value. strace sidesteps that by counting only the traced tree's own
syscalls.

Skipped (not failed) wherever `strace` is not on PATH -- macOS and Windows,
where this hook is not portable at all -- same shape as the win32 skip
every other post-tool-hook.sh subprocess test in this file's neighbourhood
carries (see docs/windows-skip-triage.md's row for this module).

POSITIVE CONTROL: the cooldown path's process count must come out LOWER
than the below-threshold path's (it skips the whole scan), proving this
method can detect a decrease and is not just reporting the same number for
every scenario because the harness itself is broken.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

import pytest

from tests.test_post_tool_incremental_scan_913 import HOOK, _env, _project, _reap

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

# clone3 is included alongside the legacy clone/fork/vfork trio: modern
# glibc can route process creation through clone3 on a recent kernel, and
# a trace set missing it would silently undercount on exactly the hosts
# most likely to run this test (current ubuntu-latest images).
_CLONE_RE = re.compile(r"^\d+\s+(clone3?|fork|vfork)\(")


def _strace_path():
    return shutil.which("strace")


def _count_forks(env):
    """Total clone/clone3/fork/vfork syscalls the hook's own process tree
    makes, via a raw (non -c) strace trace -- counting matching lines
    avoids depending on a specific strace version's `-c` summary-table
    layout. Returns None (never 0) on anything that is not a genuine,
    complete trace: strace itself exiting non-zero (an unsupported flag
    combination, a ptrace restriction on a hardened runner) is NOT the
    same thing as "the hook forked nothing," which never happens on this
    code path -- a bare 0 there would read as a pass on `0 <= N` and hide
    the very failure this test exists to catch."""
    strace = _strace_path()
    log = env["_STRACE_LOG"]
    cmd = [strace, "-f", "-qq", "-e", "trace=clone,clone3,fork,vfork",
           "-o", log, "bash", str(HOOK)]
    result = subprocess.run(cmd, capture_output=True, env=env, timeout=120,
                             input=b"", check=False)
    if result.returncode != 0:
        return None
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    count = sum(1 for line in lines if _CLONE_RE.match(line))
    if count == 0:
        # A hook call that reaches this code path always forks at least
        # the chunk read; zero real matches alongside a clean exit status
        # means the trace itself produced nothing usable (wrong flags
        # silently accepted, an empty/truncated log), not a forkless run.
        return None
    return count


@pytest.mark.skipif(_strace_path() is None,
                     reason="strace not on PATH -- exact fork counting needs it; "
                            "this leg cannot see the class #1011 regressed")
def test_below_threshold_path_total_forks_at_or_under_v0424_baseline(tmp_path):
    """v0.42.4 (before #913's incremental scan existed at all) forked a
    single `wc -l < "$TRANSCRIPT"` for this decision: 1 subshell + 1 `wc`
    exec. #1011 round 1 (#913's offset-based rewrite) and #1017 (the
    quadratic-newline-count fix after it) grew this to 3 fork-groups -- the
    outer `$(_pt_scan_lines ...)` subshell, the `tail` chunk read, and a
    `printf | wc` pipe -- 6 processes for this one decision, a real
    regression #1011 round 2 exists to undo. This pins the fix: globals
    instead of an outer command substitution, and a herestring instead of a
    pipe, both landing the function+call-site's own contribution at 4
    processes (2 for the unavoidable `tail` chunk read, 2 for `wc -l`
    counting the chunk's complete lines) -- below the 6 the whole
    below-threshold path forked before #1011 round 1 ever touched it,
    whenever there is at least one complete line to count."""
    n = 9
    deltas = []
    for i in range(n):
        home, project, remember, _transcript = _project(tmp_path / f"t{i}",
                                                          jsonl_lines=10)
        env = _env(tmp_path / f"t{i}", home, project)
        env["_STRACE_LOG"] = str(tmp_path / f"strace-{i}.log")
        count = _count_forks(env)
        assert count is not None, "strace produced no readable trace log"
        deltas.append(count)
        _reap(remember)
    # Pinned BELOW v0.42.4's own pre-#913 baseline (6), not merely below
    # the round-1 regression (9) -- a bound of "<= 9" would still pass if
    # this fix were reverted outright, since 9 <= 9 is true. 6 is the
    # ceiling: this fix's own design removes 2 of the 6 fork-groups
    # _pt_scan_lines and its call site forked at the 9-total state (the
    # outer `$(...)` wrapper, and the pipe's extra subshell), landing at
    # 4 for that specific contribution -- below what v0.42.4 forked here
    # before #913/#1017 ever added anything.
    assert min(deltas) < 6, (
        f"below-threshold path forked {min(deltas)} processes (min of {n}) -- "
        f"at or above v0.42.4's own baseline (6) for this decision, not just "
        f"the round-1 regression (9); the fork reduction in _pt_scan_lines "
        f"has regressed: {sorted(deltas)}"
    )


@pytest.mark.skipif(_strace_path() is None,
                     reason="strace not on PATH -- exact fork counting needs it; "
                            "this leg cannot see the class #1011 regressed")
def test_cooldown_path_forks_fewer_than_below_threshold_path(tmp_path):
    """POSITIVE CONTROL: the cooldown path skips the scan entirely (the
    whole `if [ "$IN_COOLDOWN" = false ]` block, globals, function
    definition and call site included, never executes), so it must measure
    strictly fewer total forks than the below-threshold path in the SAME
    run -- proving this counting method can see a real decrease, not just
    report a flat number regardless of which path actually executed."""
    n = 9
    below_d, cool_d = [], []
    import time
    cooldown_ts = int(time.time()) - 5
    for i in range(n):
        home, project, remember, _transcript = _project(tmp_path / f"b{i}",
                                                          jsonl_lines=10)
        env = _env(tmp_path / f"b{i}", home, project)
        env["_STRACE_LOG"] = str(tmp_path / f"below-{i}.log")
        below_d.append(_count_forks(env))
        _reap(remember)

        home2, project2, remember2, _transcript2 = _project(
            tmp_path / f"c{i}", jsonl_lines=10, cooldown_ts=cooldown_ts)
        env2 = _env(tmp_path / f"c{i}", home2, project2)
        env2["_STRACE_LOG"] = str(tmp_path / f"cool-{i}.log")
        cool_d.append(_count_forks(env2))
        _reap(remember2)
    assert None not in below_d, (
        f"strace produced no usable trace on the below-threshold path: {below_d}"
    )
    assert None not in cool_d, (
        f"strace produced no usable trace on the cooldown path: {cool_d}"
    )
    assert min(cool_d) < min(below_d), (
        f"cooldown path ({sorted(cool_d)}) did not fork fewer processes than "
        f"the below-threshold path ({sorted(below_d)}) -- either the scan is "
        f"no longer being skipped under cooldown, or this counting method "
        f"cannot see the decrease it exists to detect"
    )
