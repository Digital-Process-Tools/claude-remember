"""#1011: Windows 11 25H2 win32kfull.sys bug leaks one kernel Token object
per console-attached process start, until a real Restart. The confirmed
workaround is setting the LIVE foreground lock timeout to 0 -- an opt-in
system change this plugin must never apply itself (see docs/windows.md).

doctor.sh's WARN-only check decides, from a build number and the live
SPI_GETFOREGROUNDLOCKTIMEOUT value, whether to point the user at that
workaround. These tests drive the decision on every platform, not only a
real Windows host (the real PowerShell call, tested on a real Windows host,
lives in test_windows_real_fglock_1011.py instead) by:

- setting OS=Windows_NT, which is the real signal
  _remember_doctor_is_windows() already reads first -- not a doctor.sh-only
  test knob, since any real environment that sets this variable this way
  is treated as Windows by the same logic a real Windows host hits; and
- putting a stub `powershell.exe` ahead of the real PATH that answers
  whichever of the two generated .ps1 probe scripts it is handed (it tells
  them apart by grepping the script file doctor.sh wrote), returning a
  build number or a timeout value this test controls.

#1015 review round: the previous version of this file instead shipped three
env-var knobs doctor.sh's own production code read unconditionally
(_REMEMBER_DOCTOR_FORCE_WINDOWS, _REMEMBER_DOCTOR_WIN_BUILD_OVERRIDE,
_REMEMBER_DOCTOR_FGLOCK_TIMEOUT_OVERRIDE) -- a real user could set any of
them to SUPPRESS a genuine WARN (fake a safe build, fake a zero timeout, or
fake "not Windows" outright). The stub-on-PATH approach here, matching how
test_windows_hidden_detach_1002.py stubs wscript.exe, needs no such knob:
the env vars the stub itself reads to decide what to print
(_TEST_STUB_FGLOCK_BUILD / _TEST_STUB_FGLOCK_TIMEOUT) are read by the fake
powershell.exe this test creates, never by doctor.sh.

Per this repo's CLAUDE.md, a negative assertion needs a positive control:
every "must not WARN" case here is paired with a "must WARN" case, so a
broken harness that prints nothing cannot pass by accident.

This module blanket-skips on win32 for the same reason test_doctor.py and
test_doctor_cap_disabled_360.py already do (bash subprocess + POSIX
semantics) -- tracked in docs/windows-skip-triage.md in the same commit.
"""

from __future__ import annotations

import os
import stat
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

_STUB_BODY = """#!/bin/sh
# Fake powershell.exe for #1011 tests only -- tells the two real probe
# scripts apart by grepping the file named after -File, and answers with
# whichever value THIS STUB's own env vars carry. doctor.sh never reads
# these two vars; only this stub does.
file=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "-File" ]; then
        file="$arg"
    fi
    prev="$arg"
done
if [ -z "$file" ] || [ ! -f "$file" ]; then
    exit 1
fi
if grep -q "OSVersion" "$file" 2>/dev/null; then
    printf '%s' "${_TEST_STUB_FGLOCK_BUILD:-}"
elif grep -q "SystemParametersInfo" "$file" 2>/dev/null; then
    printf '%s' "${_TEST_STUB_FGLOCK_TIMEOUT:-}"
else
    exit 1
fi
"""


_BROKEN_STUB_BODY = """#!/bin/sh
# Fake powershell.exe that deterministically fails, for the
# "powershell unavailable/broken" test below. #1015 round 4 CI: macos-latest
# runners were observed to carry a REAL `powershell`/pwsh reachable via
# `command -v powershell`, which answered doctor.sh's build probe for real
# with a non-Windows .NET OSVersion.Version.Build value (seen as "2") --
# that made this test's old "there genuinely is no powershell on this CI
# platform" assumption false on that image, and the assertion flaky by
# runner image rather than by anything this plugin's own code does. A stub
# that always fails forces the degrade path deterministically, on every
# platform, regardless of what else happens to be on PATH.
exit 1
"""


def _stub_powershell(tmp_path: Path, *, broken: bool = False) -> Path:
    """Write a fake powershell.exe into its own directory and return that
    directory, to be prepended to PATH -- the file's NAME is what
    `command -v powershell.exe` matches on; its extension is inert on
    POSIX, same as the real wscript.exe stub test's own convention."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    stub = bin_dir / "powershell.exe"
    stub.write_text(_BROKEN_STUB_BODY if broken else _STUB_BODY, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def _run(tmp_path: Path, overrides: dict, *, stub_build: str | None = None,
         stub_timeout: str | None = None, windows: bool = True,
         broken_powershell: bool = False) -> subprocess.CompletedProcess:
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
    if windows:
        env["OS"] = "Windows_NT"
    else:
        env.pop("OS", None)
    if broken_powershell:
        bin_dir = _stub_powershell(tmp_path, broken=True)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    elif stub_build is not None or stub_timeout is not None:
        bin_dir = _stub_powershell(tmp_path)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        if stub_build is not None:
            env["_TEST_STUB_FGLOCK_BUILD"] = stub_build
        if stub_timeout is not None:
            env["_TEST_STUB_FGLOCK_TIMEOUT"] = stub_timeout
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
    result = _run(tmp_path, {}, stub_build="26200", stub_timeout="0")
    assert result.returncode == 0, result.stderr
    assert "VERDICT:" in result.stdout, "died before reaching a verdict"

def test_affected_build_with_nonzero_timeout_warns(tmp_path):
    """Positive control: an affected build whose live timeout is not 0 is
    exactly the case the issue asks doctor.sh to flag."""
    result = _run(tmp_path, {}, stub_build="26200", stub_timeout="2147483647")
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert block.startswith("WARN"), block
    assert "#1011" in block
    assert "docs/windows.md" in block

def test_affected_build_with_zero_timeout_does_not_warn(tmp_path):
    """Negative case: the opt-in workaround is already applied, so there is
    nothing to flag -- paired with the WARN case above as the positive
    control for this assertion."""
    result = _run(tmp_path, {}, stub_build="26300", stub_timeout="0")
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert "WARN" not in block, block
    assert block.startswith("OK"), block

def test_unaffected_build_does_not_warn(tmp_path):
    """A build below the confirmed-affected floor (26200) must never WARN,
    whatever the timeout is -- the build check is the gate, not the timeout
    alone."""
    result = _run(tmp_path, {}, stub_build="19045", stub_timeout="2147483647")
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert "WARN" not in block, block
    assert block.startswith("OK"), block

def test_not_windows_prints_nothing(tmp_path):
    """Off Windows the section must not print at all -- there is nothing to
    check, and a line claiming otherwise would be noise on every other
    platform's doctor run."""
    result = _run(tmp_path, {}, windows=False)
    assert "Windows foreground-lock check" not in result.stdout

def test_powershell_unavailable_reports_could_not_check_not_a_false_ok(tmp_path):
    """A broken powershell.exe (deterministic, not just an absent one -- see
    _BROKEN_STUB_BODY) makes the probe fail -- the third state, never
    silently folded into OK or WARN."""
    result = _run(tmp_path, {}, broken_powershell=True)
    block = _fglock_block(result.stdout)
    assert block, "the #1011 check section never printed"
    assert "WARN" in block
    assert "could not" in block.lower() or "unexpected" in block.lower(), block
