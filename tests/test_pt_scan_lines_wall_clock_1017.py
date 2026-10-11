"""#1017: `_pt_scan_lines` (scripts/post-tool-hook.sh) counted newlines in
the appended chunk with a per-byte bash parameter substitution over the
chunk's own text, which is QUADRATIC in chunk size -- both bash 3.2 and
5.3. Measured by the release auditor on macOS arm64: 110.81s for a 2MB
chunk on bash 5.3.15, 415.67s for a 50KB chunk on /bin/bash 3.2.57 (this
repo's documented floor shell, and macOS's shipped default). Output was
always correct; only the cost regressed, and it regressed the exact #913
scenario (the first post-upgrade call on an existing session rescans the
whole transcript) to far worse than the 229ms whole-file `wc -l` #913
itself replaced.

Each test below pins a hard wall-clock bound, using chunk sizes the
auditor already measured directly against the pre-fix code, so a failure
here is not a guess about where the line sits -- it is the same
measurement, re-run. `tests.spawn_counting.make_shim_dir` is deliberately
NOT used here: these tests care about wall-clock time, which a PATH-shim
fork-counting harness does not slow down measurably, but a test that
forgot to account for it would -- so the hook runs against the real PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

from tests.test_post_tool_incremental_scan_913 import (
    HOOK,
    TRANSCRIPT_LINE,
    _env,
    _project,
    _reap,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)


def _bash32():
    """/bin/bash on a real bash-3.2 host (macOS's shipped default, and this
    repo's own documented floor). None if /bin/bash is absent or is not
    actually 3.2 (e.g. a Linux CI image where /bin/bash is already 5.x) --
    the caller skips rather than silently testing the wrong shell."""
    path = "/bin/bash"
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                              timeout=5, text=True, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if "version 3.2" in out else None


def _run_with(bash_path, env, *, timeout=60):
    return subprocess.run([bash_path, str(HOOK)], capture_output=True, env=env,
                           timeout=timeout, input=b"", check=False)


def _run(env, *, timeout=60):
    return _run_with(shutil.which("bash") or "bash", env, timeout=timeout)


# -- bash 3.2.57, the repo's documented floor: the issue's own measurement --

def test_warm_scan_on_bash32_floor_is_bounded(tmp_path):
    """#1017's own repro, run for real: a 10KB warm chunk on /bin/bash
    3.2.57 measured at ~7s pre-fix. A linear fix (one `wc -l` fork over
    the chunk, no quadratic term) must land comfortably under that."""
    bash32 = _bash32()
    if bash32 is None:
        pytest.skip("no real /bin/bash 3.2 found on this host -- cannot "
                     "exercise this repo's documented floor shell")

    home, project, remember, transcript = _project(tmp_path, jsonl_lines=10)
    env = _env(tmp_path, home, project)

    cold = _run_with(bash32, env, timeout=30)
    assert cold.returncode == 0, cold.stderr[:400]

    # ~10KB of appended bytes -- the issue's own "10KB -> 7s" bash-3.2
    # measurement.
    chunk = TRANSCRIPT_LINE * (10_000 // len(TRANSCRIPT_LINE) + 1)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(chunk)

    start = time.monotonic()
    warm = _run_with(bash32, env, timeout=30)
    elapsed = time.monotonic() - start
    _reap(remember)

    assert warm.returncode == 0, warm.stderr[:400]
    assert elapsed < 2.5, (
        f"warm scan of a ~10KB chunk on /bin/bash 3.2 took {elapsed:.2f}s -- "
        "#1017's own measurement for this exact size was ~7s on the "
        "quadratic code this bound exists to catch"
    )


# -- whatever bash resolves off PATH (bash 5.x on most CI images) -----------

def test_warm_scan_of_a_500kb_chunk_is_bounded(tmp_path):
    """#1017's own measurement: a 500KB warm chunk cost 7.26s on bash 5.3
    with the quadratic count. A linear fix stays well under that."""
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=10)
    env = _env(tmp_path, home, project)

    cold = _run(env, timeout=60)
    assert cold.returncode == 0, cold.stderr[:400]

    chunk = TRANSCRIPT_LINE * (500_000 // len(TRANSCRIPT_LINE) + 1)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(chunk)

    start = time.monotonic()
    warm = _run(env, timeout=60)
    elapsed = time.monotonic() - start
    _reap(remember)

    assert warm.returncode == 0, warm.stderr[:400]
    assert elapsed < 2.0, (
        f"warm scan of a ~500KB chunk took {elapsed:.2f}s -- #1017's own "
        "measurement for this exact size was 7.26s on the quadratic code "
        "this bound exists to catch"
    )


def test_cold_full_rescan_of_a_1mb_transcript_is_bounded(tmp_path):
    """The #1017 scenario itself: the FIRST call on an existing session
    (no sidecar yet) rescans the whole transcript from offset 0 -- exactly
    the hundreds-of-MB case #913 was fixing, now at #1017's own measured
    1000KB -> 28.58s on bash 5.3. A 1MB transcript, cold, stays well under
    that bound once the count is linear."""
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=1)
    chunk = TRANSCRIPT_LINE * (1_000_000 // len(TRANSCRIPT_LINE) + 1)
    transcript.write_text(chunk, encoding="utf-8")
    env = _env(tmp_path, home, project)

    start = time.monotonic()
    cold = _run(env, timeout=90)
    elapsed = time.monotonic() - start
    _reap(remember)

    assert cold.returncode == 0, cold.stderr[:400]
    assert elapsed < 5.0, (
        f"cold full-transcript scan of a ~1MB transcript took {elapsed:.2f}s "
        "-- #1017's own measurement for this exact size was 28.58s on the "
        "quadratic code this bound exists to catch"
    )


def test_scan_time_scales_linearly_not_quadratically(tmp_path):
    """The cheap scaling check #1017 asked for: doubling the chunk size
    must not cost anywhere near 4x the time (what the issue's own
    measurements show for the quadratic code: 250KB->1.9s, 500KB->7.26s,
    a ~3.8x step). A linear fix's ratio should sit close to 2x; this
    allows up to ~3.5x for fork/IO noise without re-admitting the
    quadratic regression."""
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=10)
    env = _env(tmp_path, home, project)
    cold = _run(env, timeout=60)
    assert cold.returncode == 0, cold.stderr[:400]

    unit = len(TRANSCRIPT_LINE)
    small = TRANSCRIPT_LINE * (250_000 // unit + 1)
    with transcript.open("a", encoding="utf-8") as f:
        f.write(small)
    start = time.monotonic()
    warm_small = _run(env, timeout=60)
    small_elapsed = time.monotonic() - start
    assert warm_small.returncode == 0, warm_small.stderr[:400]

    big = TRANSCRIPT_LINE * (250_000 // unit + 1)  # one more 250KB -> 500KB total appended
    with transcript.open("a", encoding="utf-8") as f:
        f.write(big)
    start = time.monotonic()
    warm_big = _run(env, timeout=60)
    big_elapsed = time.monotonic() - start
    _reap(remember)
    assert warm_big.returncode == 0, warm_big.stderr[:400]

    floor = max(small_elapsed, 0.05)  # avoid a near-zero denominator on a fast box
    ratio = big_elapsed / floor
    assert ratio <= 3.5, (
        f"doubling the appended chunk size cost {ratio:.2f}x the time "
        f"({small_elapsed:.3f}s -> {big_elapsed:.3f}s) -- quadratic growth "
        "would show ~4x here, per #1017's own measured doublings"
    )
