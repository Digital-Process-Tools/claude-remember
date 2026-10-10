"""#1011: on a real Windows host, drive doctor.sh's two PowerShell probes
(build number, live SPI_GETFOREGROUNDLOCKTIMEOUT) for real -- the decision
logic itself is covered on every platform in test_doctor_windows_fglock_1011.py
via env-var overrides, which never shell out to a real powershell.exe.

This module runs ONLY on a real Windows host (skipped everywhere else --
never the inverse: on windows-latest these tests must run, never skip),
following test_windows_real_wscript_1002.py's own convention.

The CI build is never 26200+ (confirmed-affected), so the WARN/OK-affected
branch is not expected to fire here -- that branch is exercised by the
override-based unit tests instead. What this module confirms for real is
narrower and still worth having: both PowerShell probes return a bare
integer on a real Windows host, and the unaffected-build path (this CI
host's own build number) does not WARN -- the observed negative control.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._bash_runner import resolve_bash

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="#1011 real-PowerShell evidence -- only meaningful on a real "
           "Windows host; never skipped on windows-latest itself",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"
BASH = resolve_bash()

def _project_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _run_probe(tmp_path: Path, func_name: str) -> subprocess.CompletedProcess:
    """Source doctor.sh for real (which also runs its own full diagnostic
    report as a side effect -- discarded) and then call one of its two
    PowerShell-probing functions directly, for real, on this host."""
    assert BASH, "no real bash resolvable on this Windows host"
    env = _project_env(tmp_path)
    script = f'source "{DOCTOR}" >/dev/null 2>&1; {func_name}'
    return subprocess.run([BASH, "-c", script], env=env, check=False,
                          capture_output=True, text=True, timeout=60)

def test_real_build_probe_returns_an_integer(tmp_path):
    result = _run_probe(tmp_path, "_remember_doctor_fglock_build")
    assert result.stdout.strip().isdigit(), (
        f"build probe did not return a bare integer: {result.stdout!r} "
        f"(stderr: {result.stderr!r})"
    )


def test_real_timeout_probe_returns_an_integer(tmp_path):
    result = _run_probe(tmp_path, "_remember_doctor_fglock_timeout")
    assert result.stdout.strip().isdigit(), (
        f"timeout probe did not return a bare integer: {result.stdout!r} "
        f"(stderr: {result.stderr!r})"
    )


def test_this_ci_hosts_own_build_does_not_warn(tmp_path):
    """OBSERVED negative control: the real build this CI leg runs on is not
    26200+ (confirmed-affected), so the real end-to-end check must not WARN
    here -- it is exercised positively by the override-based unit tests in
    test_doctor_windows_fglock_1011.py instead."""
    env = _project_env(tmp_path)
    env["_REMEMBER_DOCTOR_FORCE_WINDOWS"] = "1"
    result = subprocess.run([BASH, str(DOCTOR)], env=env, check=False,
                            capture_output=True, text=True, timeout=120)
    lines = result.stdout.splitlines()
    block = []
    for i, line in enumerate(lines):
        if "Windows foreground-lock check (#1011)" in line:
            for later in lines[i + 1:]:
                if later == "":
                    break
                block.append(later)
            break
    assert block, f"the #1011 check section never printed: {result.stdout}"
    assert not any(ln.startswith("WARN") for ln in block), block
