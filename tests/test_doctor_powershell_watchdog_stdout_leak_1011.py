"""#1011 round 6: _remember_doctor_run_powershell_file's watchdog subshell

    ( sleep "$_ps_timeout"; kill -9 "$_ps_pid" 2>/dev/null ) &

inherited this function's own stdout/stderr unredirected. A caller that
captures the function's output via command substitution ($(...)) -- which
every real caller does, e.g.
`_FGLOCK_TIMEOUT=$(_remember_doctor_fglock_timeout)` -- reads from a pipe
that stays open as long as ANY process holds its write end, and `sleep`
(forked as a child of the watchdog subshell, since the subshell runs two
sequential commands and bash cannot exec-replace it) kept that write end
open for the FULL bound even after PowerShell had already answered and the
function had already printed its real result via `printf`. Confirmed on
real windows-latest CI (PR #1024 merge-check logs): each real-host test in
test_windows_real_fglock_1011.py took ~2x its bound (11s at a 5s bound,
31s at a 15s bound) -- one full bound per probe, not the ~1s PowerShell
itself needed.

This module drives the REAL production function directly (extracted
verbatim out of scripts/doctor.sh at test time, not reimplemented) with a
stub powershell.exe this test controls, so the mechanism is provable on
every OS, not only a real Windows host (that real-PowerShell evidence
stays in test_windows_real_fglock_1011.py).

Per this repo's CLAUDE.md, a negative assertion needs a positive control:
the "must return fast" case below is paired with a "must still be killed
at the bound" case, so a watchdog that was removed outright (never killing
a hung probe) would also fail the suite, not just pass it by accident.

This module blanket-skips on win32 for the same reason test_doctor.py and
test_doctor_windows_fglock_1011.py already do (bash subprocess + POSIX
semantics) -- tracked in docs/windows-skip-triage.md in the same commit.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

_FAST_STUB = """#!/bin/sh
# Answers instantly regardless of which of the two real .ps1 probe files it
# is handed -- this test only cares about the watchdog's own timing, never
# about which probe ran or what it returns.
printf '%s' "42"
"""

_HANG_STUB = """#!/bin/sh
# Never exits on its own. The ONLY way this test's process tree clears is
# the watchdog's kill at the bound -- positive control for the
# kill-on-timeout path this fix must not remove.
sleep 300
"""


def _extract_function(name: str) -> str:
    """Pull one function's definition out of scripts/doctor.sh verbatim, so
    this test drives the real production code rather than a reimplementation
    of its watchdog pattern -- read fresh at test time, so it exercises
    whatever is currently in the file, buggy or fixed."""
    lines = DOCTOR.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _stub_dir(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "powershell.exe"
    stub.write_text(body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run(tmp_path: Path, stub_body: str, bound: int):
    bin_dir = _stub_dir(tmp_path, stub_body)
    script = tmp_path / "probe.ps1"
    script.write_text("ignored\n", encoding="utf-8")
    function_src = _extract_function("_remember_doctor_run_powershell_file")
    wrapper = (
        "set -u\n"
        + function_src
        + "\n"
        + f'out=$(_remember_doctor_run_powershell_file "{script}" {bound})\n'
        + 'rc=$?\n'
        + 'printf \'OUT=%s\\n\' "$out"\n'
        + 'printf \'RC=%s\\n\' "$rc"\n'
        + f'if pgrep -f "sleep {bound}$" >/dev/null 2>&1; then\n'
        + '    printf \'STRAY_SLEEP=1\\n\'\n'
        + 'else\n'
        + '    printf \'STRAY_SLEEP=0\\n\'\n'
        + 'fi\n'
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    start = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", wrapper], env=env, check=False, capture_output=True, text=True, timeout=60
    )
    elapsed = time.monotonic() - start
    return result, elapsed


def test_fast_powershell_returns_well_under_a_large_bound(tmp_path):
    """The bug: a stub that answers in effectively 0s still had to wait out
    the FULL bound because the watchdog's own `sleep` kept the command
    substitution's pipe open. Bound is deliberately large (17s, a value
    unlikely to collide with an unrelated `sleep` elsewhere on the host) so
    a fix collapsing this to "well under 2s" is unambiguous evidence, not a
    coincidence of a short bound."""
    bound = 17
    result, elapsed = _run(tmp_path, _FAST_STUB, bound)
    assert "RC=0" in result.stdout, result.stdout + result.stderr
    assert "OUT=42" in result.stdout, result.stdout + result.stderr
    assert elapsed < 2.0, (
        f"took {elapsed:.2f}s against a {bound}s bound for a stub that answers "
        f"instantly -- the watchdog's own sleep is holding the command "
        f"substitution's pipe open (#1011 round 6)"
    )


def test_fast_powershell_leaves_no_stray_sleep_process(tmp_path):
    """Positive-direction companion to the timing assertion above: even if a
    fix made the elapsed time look fine by some other means, a `sleep`
    child of the watchdog subshell that outlives the function call is still
    a leaked process -- this asserts directly that none remains."""
    bound = 19
    result, elapsed = _run(tmp_path, _FAST_STUB, bound)
    assert elapsed < 2.0, result.stdout + result.stderr
    assert "STRAY_SLEEP=0" in result.stdout, (
        "a `sleep` child of the watchdog is still alive after the function "
        "returned:\n" + result.stdout + result.stderr
    )


def test_hung_powershell_is_still_killed_at_the_bound(tmp_path):
    """Positive control: a probe that never answers on its own must still be
    killed at the bound and degrade to a non-zero return, not hang
    forever -- the fix above must not come at the cost of this path."""
    bound = 2
    result, elapsed = _run(tmp_path, _HANG_STUB, bound)
    assert "RC=0" not in result.stdout, result.stdout + result.stderr
    assert bound - 0.5 <= elapsed <= bound + 5, (
        f"took {elapsed:.2f}s against a {bound}s kill-on-timeout bound -- "
        f"expected close to the bound itself, not a multiple of it"
    )
