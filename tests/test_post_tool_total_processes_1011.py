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

ROUND 2 OF ROUND 2 (this file's own self-review finding, #1011 follow-up):
a CI run of the below-threshold test asserted the whole hook invocation's
TOTAL fork count (`strace -f bash "$HOOK"`, every clone/fork/vfork anywhere
in the process tree -- libs sourced, `detect-tools.sh` probes, the
background `save-session.sh` detach, all of it) against 6, a number that
only ever described `_pt_scan_lines`'s own local contribution. It measured
67 on every one of 9 runs across every ubuntu/Python leg -- a real,
reproducible number, just checked against the wrong claim. The whole-hook
total was never pinned against anything meaningful before this round: this
file now compares the SAME whole-hook strace measurement across three real
trees -- the tagged v0.42.4 release (before #913's incremental scan existed
at all), the tagged v0.43.0 release (this issue's own immediately preceding,
already-regressed baseline), and HEAD (this fix) -- instead of against a
number nobody re-derived. `_pt_scan_lines` and its call site are a few
processes out of a much larger total; this file no longer pretends it is
measuring their contribution alone.
"""

from __future__ import annotations

import atexit
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.test_post_tool_incremental_scan_913 import (
    HOOK,
    REPO_ROOT,
    _env,
    _project,
    _reap,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)

# clone3 is included alongside the legacy clone/fork/vfork trio: modern
# glibc can route process creation through clone3 on a recent kernel, and
# a trace set missing it would silently undercount on exactly the hosts
# most likely to run this test (current ubuntu-latest images).
_CLONE_RE = re.compile(r"^\d+\s+(clone3?|fork|vfork)\(")

# Tag -> materialized tree root, or None once materializing it has been
# tried and failed (no network, tag genuinely absent, archive/extract
# failure) -- cached once per test session rather than re-attempted per
# call, since a failure mode here (no network reaching origin) is a
# property of the whole run, not of one test.
_TREE_CACHE: dict[str, Path | None] = {}


def _strace_path():
    return shutil.which("strace")


def _materialize_tree(tag):
    """Extract the tagged release's full tree (whatever `git archive` has
    recorded for it -- the whole `scripts/` directory, since the hook
    sources half a dozen sibling libs by relative path, not just the one
    file) into a throwaway directory, fetching the tag shallowly first if
    this checkout does not already have it (a CI checkout is commonly
    shallow and tag-less). Returns None -- never raises -- on anything that
    keeps the comparison from being possible here: no git, no network, the
    tag genuinely does not exist. A caller that gets None for a tag must
    skip (not fail) the comparison against that baseline; None is "could
    not measure," never "measured zero."
    """
    if tag in _TREE_CACHE:
        return _TREE_CACHE[tag]
    git = shutil.which("git")
    if git is None:
        _TREE_CACHE[tag] = None
        return None
    verify = subprocess.run(
        [git, "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", tag + "^{commit}"],
        capture_output=True, timeout=30, check=False,
    )
    if verify.returncode != 0:
        fetch = subprocess.run(
            [git, "-C", str(REPO_ROOT), "fetch", "--depth=1", "origin", "tag", tag],
            capture_output=True, timeout=60, check=False,
        )
        if fetch.returncode != 0:
            _TREE_CACHE[tag] = None
            return None
    dest = Path(tempfile.mkdtemp(prefix="remember-tree-" + tag.replace("/", "_") + "-"))
    archive = subprocess.run(
        [git, "-C", str(REPO_ROOT), "archive", "--format=tar", tag],
        capture_output=True, timeout=60, check=False,
    )
    if archive.returncode != 0 or not archive.stdout:
        shutil.rmtree(dest, ignore_errors=True)
        _TREE_CACHE[tag] = None
        return None
    extract = subprocess.run(
        ["tar", "-x", "-C", str(dest)],
        input=archive.stdout, capture_output=True, timeout=60, check=False,
    )
    if extract.returncode != 0 or not (dest / "scripts" / "post-tool-hook.sh").exists():
        shutil.rmtree(dest, ignore_errors=True)
        _TREE_CACHE[tag] = None
        return None
    atexit.register(shutil.rmtree, dest, True)
    _TREE_CACHE[tag] = dest
    return dest


def _count_forks(env, hook=HOOK):
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
           "-o", log, "bash", str(hook)]
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


def _min_forks_for_tree(tmp_path, label, hook, plugin_root, *, n, cooldown_ts=None):
    """Run the below-threshold (or, with cooldown_ts set, cooldown) path
    `n` times against one tree's own hook + sibling libs, and return the
    minimum total fork count seen -- the same min-of-n convention the
    release auditor and this file's own positive control already use,
    since a single run's count is noisy (background reaping, scheduler
    jitter) but the floor is stable."""
    deltas = []
    for i in range(n):
        home, project, remember, _transcript = _project(
            tmp_path / f"{label}-{i}", jsonl_lines=10, cooldown_ts=cooldown_ts)
        env = _env(tmp_path / f"{label}-{i}", home, project)
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        env["_STRACE_LOG"] = str(tmp_path / f"{label}-{i}.strace.log")
        count = _count_forks(env, hook=hook)
        assert count is not None, (
            f"strace produced no readable trace log for tree={label!r}"
        )
        deltas.append(count)
        _reap(remember)
    return min(deltas), deltas


@pytest.mark.skipif(_strace_path() is None,
                     reason="strace not on PATH -- exact fork counting needs it; "
                            "this leg cannot see the class #1011 regressed")
def test_below_threshold_path_total_forks_vs_tagged_baselines(tmp_path):
    """Compares the SAME whole-hook strace measurement across three real
    trees instead of against a hand-picked number: v0.42.4 (before #913's
    incremental scan existed at all), v0.43.0 (this issue's own immediately
    preceding, already-regressed release), and HEAD (this fix). A tagged
    tree that cannot be materialized here (no network reaching origin, or
    the tag genuinely absent from this checkout) is skipped for its own
    comparison rather than silently treated as a pass or a fail -- the
    assertion against it is what gets skipped, not the whole test, so HEAD
    is still checked against whichever baseline(s) are actually available.
    """
    n = 9
    head_min, head_all = _min_forks_for_tree(
        tmp_path, "head", HOOK, REPO_ROOT, n=n)

    results = {"HEAD": (head_min, head_all)}
    skipped = []
    for tag in ("v0.42.4", "v0.43.0"):
        tree = _materialize_tree(tag)
        if tree is None:
            skipped.append(tag)
            continue
        tag_min, tag_all = _min_forks_for_tree(
            tmp_path, tag.replace(".", "_"),
            tree / "scripts" / "post-tool-hook.sh", tree, n=n)
        results[tag] = (tag_min, tag_all)

    summary = ", ".join(
        f"{label}=min:{vals[0]} all:{sorted(vals[1])}"
        for label, vals in results.items()
    )
    print(f"#1011 below-threshold path, whole-hook strace totals -- {summary}"
          + (f" (skipped, could not materialize: {skipped})" if skipped else ""))

    if "v0.42.4" not in results and "v0.43.0" not in results:
        pytest.skip(
            "could not materialize either tagged comparison tree (no "
            f"network reaching origin, or tags absent from this checkout) "
            f"-- HEAD alone measured min={head_min} all={sorted(head_all)}, "
            "not checked against a baseline"
        )

    if "v0.42.4" in results:
        baseline_min = results["v0.42.4"][0]
        assert head_min <= baseline_min, (
            f"below-threshold path, whole-hook strace total -- {summary} -- "
            f"HEAD ({head_min}) regressed past v0.42.4's own baseline "
            f"({baseline_min}); this fix must not fork more than the "
            f"release that existed before #913's incremental scan did"
        )

    if "v0.43.0" in results:
        regressed_min = results["v0.43.0"][0]
        assert head_min < regressed_min, (
            f"below-threshold path, whole-hook strace total -- {summary} -- "
            f"HEAD ({head_min}) did not improve on v0.43.0's own "
            f"already-regressed total ({regressed_min}); this fix's whole "
            f"point was to fork fewer processes than the release it follows"
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
    report a flat number regardless of which path actually executed. Runs
    against HEAD only: this is a check on the measurement method itself,
    not a cross-tree comparison, so a tagged tree's own availability (or
    lack of it) is irrelevant here."""
    n = 9
    below_min, below_all = _min_forks_for_tree(tmp_path, "below", HOOK, REPO_ROOT, n=n)
    import time
    cooldown_ts = int(time.time()) - 5
    cool_min, cool_all = _min_forks_for_tree(
        tmp_path, "cool", HOOK, REPO_ROOT, n=n, cooldown_ts=cooldown_ts)
    assert cool_min < below_min, (
        f"cooldown path ({sorted(cool_all)}) did not fork fewer processes than "
        f"the below-threshold path ({sorted(below_all)}) -- either the scan is "
        f"no longer being skipped under cooldown, or this counting method "
        f"cannot see the decrease it exists to detect"
    )
