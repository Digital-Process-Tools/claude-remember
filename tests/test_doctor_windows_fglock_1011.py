"""#1011: Windows 11 25H2 win32kfull.sys bug leaks one kernel Token object
per console-attached process start, until a real Restart. The confirmed
workaround is setting the LIVE foreground lock timeout to 0 -- an opt-in
system change this plugin must never apply itself (see docs/windows.md).

doctor.sh's WARN-only check decides, from a build number and the live
SPI_GETFOREGROUNDLOCKTIMEOUT value, whether to point the user at that
workaround. These tests drive the decision with
_REMEMBER_DOCTOR_FORCE_WINDOWS / _REMEMBER_DOCTOR_WIN_BUILD_OVERRIDE /
_REMEMBER_DOCTOR_FGLOCK_TIMEOUT_OVERRIDE -- test-only env hooks that let the
logic run (and be asserted on) on every platform, not only a real Windows
host: the real PowerShell call, tested on a real Windows host, lives in
test_windows_real_fglock_1011.py instead.

Per this repo's CLAUDE.md, a negative assertion needs a positive control:
every "must not WARN" case here is paired with a "must WARN" case, so a
broken harness that prints nothing cannot pass by accident.

This module blanket-skips on win32 for the same reason test_doctor.py and
test_doctor_cap_disabled_360.py already do (bash subprocess + POSIX
semantics) -- tracked in docs/windows-skip-triage.md in the same commit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

def _run(tmp_path: Path, overrides: dict) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    env.update(overrides)
    return subprocess.run(["bash", str(DOCTOR)], env=env, check=False,
                          capture_output=True, text=True, timeout=120)


def _fglock_block(stdout: str) -> str:
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if "Windows foreground-lock check (#1011)" in line:
            block = []
            for later in lines[i + 1:]:
                if later == "":
                    break
                block.append(later)
            return "\\n".join(block)
    return ""

def test_doctor_always_exits_zero_with_the_new_check_present(tmp_path):
    """A diagnostic that dies instead of reporting is the problem (same bar
    test_doctor.py's own sanity test holds the whole script to)."""
    result = _run(tmp_path, {"_REMEMBER_DOCTOR_FORCE_WINDOWS": "1",
                              "_REMEMBER_DOCTOR_WIN_BUILD_OVERRIDE": "26200",
                              "_REMEMBER_DOCTOR_FGLOCK_TIMEOUT_OVERRIDE": "0"})
    assert result.returncode == 0, result.stderr
    assert "VERDICT:" in result.stdout, "died before reaching a verdict"

def test_affected_build_with_nonzero_timeout_warns(tmp_path):
    """Positive control: an affected build whose live timeout has not been
    changed from its default is exactly the case the issue asks doctor.sh
    to flag."""
    result = _run(tmp_path, {
        "_REMEMBER_DOCTOR_FORCE_WINDOWS": "1",
        "_REMEMBER_DOCTOR_WIN_BUILD_OVERRIDE": "26200",
        "_REMEMBER_DOCTOR_FGLOCK_TIMEOUT_OVERRIDE": "2147483647",
    })
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert block.startswith("WARN"), block
    assert "#1011" in block
    assert "docs/windows.md" in block

def test_affected_build_with_zero_timeout_does_not_warn(tmp_path):
    """Negative case: the opt-in workaround is already applied, so there is
    nothing to flag -- paired with the WARN case above as the positive
    control for this assertion."""
    result = _run(tmp_path, {
        "_REMEMBER_DOCTOR_FORCE_WINDOWS": "1",
        "_REMEMBER_DOCTOR_WIN_BUILD_OVERRIDE": "26300",
        "_REMEMBER_DOCTOR_FGLOCK_TIMEOUT_OVERRIDE": "0",
    })
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert "WARN" not in block, block
    assert block.startswith("OK"), block

def test_unaffected_build_does_not_warn(tmp_path):
    """A build below the confirmed-affected floor (26200) must never WARN,
    whatever the timeout is -- the build check is the gate, not the timeout
    alone."""
    result = _run(tmp_path, {
        "_REMEMBER_DOCTOR_FORCE_WINDOWS": "1",
        "_REMEMBER_DOCTOR_WIN_BUILD_OVERRIDE": "19045",
        "_REMEMBER_DOCTOR_FGLOCK_TIMEOUT_OVERRIDE": "2147483647",
    })
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert "WARN" not in block, block
    assert block.startswith("OK"), block

def test_not_windows_prints_nothing(tmp_path):
    """Off Windows the section must not print at all -- there is nothing to
    check, and a line claiming otherwise would be noise on every other
    platform's doctor run."""
    result = _run(tmp_path, {"_REMEMBER_DOCTOR_FORCE_WINDOWS": "0"})
    assert "Windows foreground-lock check" not in result.stdout

def test_powershell_unavailable_reports_could_not_check_not_a_false_ok(tmp_path):
    """On an affected build, with no build-number override, the real
    PowerShell probe runs and fails (no powershell on this CI platform) --
    the third state, never silently folded into OK or WARN."""
    result = _run(tmp_path, {"_REMEMBER_DOCTOR_FORCE_WINDOWS": "1"})
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert "WARN" in block
    assert "could not" in block.lower() or "unexpected" in block.lower(), block
