"""Byte-identity guard for the vendored v0.42.3 fixtures (#1070).

tests/test_windows_real_wscript_v0_42_3_1002.py drives the EXACT bytes
v0.42.3 shipped (tests/fixtures/v0_42_3/, see NOTE.md there) against a real
wscript.exe, and pins the expected failures with
``pytest.mark.xfail(strict=True)``. ``strict=True`` only requires *some*
failure to keep the suite green -- not the *documented* one -- so a
checkout-time corruption of these bytes (a Windows checkout converting
LF -> CRLF, say) could silently satisfy the same xfail markers with a
different failure, and nothing would notice. See issue #1070 (originally a
trap.d fragment, since curated and closed into that issue).

Two independent guards, so either one catches a regression the other
misses:

1. ``.gitattributes`` (repo root) pins the three paths ``-text``, so git
   never normalizes their line endings on any checkout, on any platform.
2. This module hashes the checked-out bytes and compares them against the
   hash of the bytes vendored at commit time, so even a corruption that
   bypassed (1) -- a future ``.gitattributes`` edit, a git version that
   ignores it, a build step that copies the file through a text-mode
   ``open()`` -- is caught directly, on every platform, not only Windows.

Deliberately NOT gated to win32: unlike the real-wscript tests, nothing
here needs Windows Script Host -- only a byte-for-byte comparison, which is
exactly as meaningful (and exactly as cheap) on Linux and macOS CI legs.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "v0_42_3"

# sha256 of the bytes vendored at commit time (see tests/fixtures/v0_42_3/
# NOTE.md: `git show v0.42.3:scripts/<name>` against tag v0.42.3, commit
# 525e07ef93a963693135c15c159285c838c9c023). Never edit a value here to make
# a failing test pass -- if a hash genuinely needs to change, re-vendor the
# fixture from the tag (NOTE.md) and recompute, because this check existing
# at all is supposed to make a silent drift impossible.
_EXPECTED_SHA256 = {
    "lib-detach.sh": (
        "91918f803598651f65035fc27e07e99e91ae145ddff3120d6db0281414608153"
    ),
    "lib-detach-pidwrap.sh": (
        "673305050460df0ceb4770d1fda5b2d8597ae39d14b90b045adb7cf20af8dd74"
    ),
    "windows-hidden-run.vbs": (
        "578fbc2b784b8b7f9e06b6719124a5300c022367e73b1291e1b9ab1c87d76d8c"
    ),
}


@pytest.mark.parametrize("name", sorted(_EXPECTED_SHA256))
def test_fixture_bytes_are_unchanged(name: str) -> None:
    path = FIXTURE_DIR / name
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == _EXPECTED_SHA256[name], (
        f"{path} no longer matches the bytes vendored from v0.42.3 at "
        f"commit time (expected sha256 {_EXPECTED_SHA256[name]}, got "
        f"{actual}) -- see NOTE.md in the same directory before touching "
        "this fixture."
    )


@pytest.mark.parametrize("name", sorted(_EXPECTED_SHA256))
def test_fixture_is_pinned_against_line_ending_rewrite(name: str) -> None:
    """Positive control for the hash check above: confirms the *mechanism*
    that is supposed to prevent corruption (``.gitattributes`` ``-text``)
    is actually wired, independent of whether the bytes happen to be intact
    right now. Without this, a checkout that silently stopped honouring
    ``.gitattributes`` would show no symptom until some future checkout
    actually rewrote a line ending -- this fails immediately instead.
    """
    rel = (FIXTURE_DIR / name).relative_to(REPO_ROOT).as_posix()
    out = subprocess.run(
        ["git", "check-attr", "text", "--", rel],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == f"{rel}: text: unset", (
        f"expected .gitattributes to pin {rel!r} as -text (git check-attr "
        f"reporting 'unset'), got: {out!r}"
    )
