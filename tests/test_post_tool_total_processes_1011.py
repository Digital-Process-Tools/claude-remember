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

_CLONE_RE = re.compile(r"^\d+\s+(clone|fork|vfork)\(")


def _strace_path():
    return shutil.which("strace")


def _count_forks(env):
    """Total clone/fork/vfork syscalls the hook's own process tree makes,
    via a raw (non -c) strace trace -- counting matching lines avoids
    depending on a specific strace version's `-c` summary-table layout."""
    strace = _strace_path()
    log = env["_STRACE_LOG"]
    cmd = [strace, "-f", "-qq", "-e", "trace=clone,fork,vfork",
           "-o", log, "bash", str(HOOK)]
    subprocess.run(cmd, capture_output=True, env=env, timeout=120, input=b"",
                    check=False)
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    return sum(1 for line in lines if _CLONE_RE.match(line))


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
    assert min(deltas) <= 9, (
        f"below-threshold path forked {min(deltas)} processes (min of {n}) -- "
        f"at or above the pre-fix #1011 round-1 regression (9); the fork "
        f"reduction in _pt_scan_lines has regressed again: {sorted(deltas)}"
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
    assert min(cool_d) < min(below_d), (
        f"cooldown path ({sorted(cool_d)}) did not fork fewer processes than "
        f"the below-threshold path ({sorted(below_d)}) -- either the scan is "
        f"no longer being skipped under cooldown, or this counting method "
        f"cannot see the decrease it exists to detect"
    )
