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
