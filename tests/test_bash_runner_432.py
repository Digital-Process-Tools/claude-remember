"""Tests for tests/_bash_runner.py (#432).

test_hook_cwd_leak_417.py and test_transcript_path_leak_424.py used to carry
a blanket `pytestmark = pytest.mark.skipif(sys.platform == "win32")`, so the
windows-latest CI leg collected and skipped both -- reporting the leg green
while the regression test for a release-blocking security fix never ran.
tests/_bash_runner.py replaces that with `resolve_bash()`, narrowing the skip
to "no usable bash found" so a missing interpreter is honest and a present
one actually runs the test.

The "red" this issue names is unobservable locally (it is the Windows leg
never executing); what these tests pin instead is the mechanism that decides
whether it executes, plus the failure mode if it ever silently trusted a
bogus path.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from . import _bash_runner


def test_resolve_bash_finds_a_real_bash_on_this_platform():
    """POSITIVE CONTROL for the whole #432 fix, OBSERVED on this platform.

    If resolve_bash() returned None here, every rewritten test in
    test_hook_cwd_leak_417.py and test_transcript_path_leak_424.py would
    report skipped-for-cause on macOS/Linux CI too -- the same silent-green
    failure this issue exists to close, just moved one layer down instead of
    fixed.
    """
    if sys.platform == "win32":
        pytest.skip("this pins the non-Windows path; see the Windows-specific tests below")
    assert _bash_runner.resolve_bash() is not None


def test_resolve_bash_reports_none_when_nothing_is_reachable(monkeypatch):
    """MUST NOT FIRE's positive control's twin: absence must read as absence.

    When no bash is reachable at all, resolve_bash() must say so (None)
    rather than guessing -- this is what lets the skipif in the two
    rewritten modules report skipped-for-cause instead of crashing or
    silently trusting a bash that does not exist.
    """
    monkeypatch.setattr(_bash_runner.shutil, "which", lambda _name: None)
    monkeypatch.setattr(_bash_runner.sys, "platform", "linux")
    assert _bash_runner.resolve_bash() is None


def test_resolve_bash_on_windows_prefers_git_for_windows_over_path(monkeypatch, tmp_path):
    """REASONED, not OBSERVED: this runs resolve_bash()'s Windows branch on
    whatever platform executes this test, which is not itself proof of Git
    Bash's real environment-inheritance behaviour on a Windows CI runner --
    that is what the issue says only CI can settle. What this does pin is
    the logic: PATH's own `bash` (commonly the WSL launcher on Windows) must
    not be trusted over a real Git-for-Windows install.
    """
    fake_git_bash = tmp_path / "Git" / "bin" / "bash.exe"
    fake_git_bash.parent.mkdir(parents=True)
    fake_git_bash.write_text("")
    monkeypatch.setattr(_bash_runner.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.setattr(
        _bash_runner.shutil, "which", lambda _name: "C:/Windows/System32/bash.exe"
    )
    assert _bash_runner.resolve_bash() == str(fake_git_bash)


def test_a_resolved_but_nonexistent_bash_path_fails_loudly_not_silently(tmp_path):
    """If resolve_bash() ever returned a path to a bash that does not
    actually exist, subprocess.run must raise rather than silently pass or
    silently skip -- proving the rewritten #417/#424 tests genuinely execute
    a real interpreter instead of trusting an unchecked path. This is the
    concrete stand-in for the issue's "deliberately-broken bash path makes
    them fail rather than skip": skipif only fires on `is None`, so a
    resolver bug that returns a bogus non-None path would reach exactly this
    failure mode in the real tests.
    """
    bogus = tmp_path / "not-a-real-bash"
    with pytest.raises(FileNotFoundError):
        subprocess.run(
            [str(bogus), "-c", "echo hi"],
            capture_output=True, text=True, timeout=5, check=False,
        )
