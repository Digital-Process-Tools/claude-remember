"""doctor.sh's below-floor FAIL line names the floor itself (#1007).

Follow-up from the review of #1005 (fixes #1003). When every Python on PATH
is below the supported floor, detect-tools.sh's own FATAL text names each
rejected candidate and the version it reported, but never the floor those
versions were measured against. doctor.sh relays that text unchanged in its
"FAIL Python:" line -- a user reading either one cannot tell which version to
install without reading the source.

detect-tools.sh cannot grow that message itself: it is inlined into every
compiled hook, and #1005 left only 37 bytes of budget under the compiled
session-start-hook.sh size cap (#1006). doctor.sh is not inlined, so the fix
lives there -- same convention as doctor's existing below-floor WARN (the
cache-hit backstop, #1003), which already interpolates
``${_REMEMBER_PY_FLOOR_MAJOR}.${_REMEMBER_PY_FLOOR_MINOR}`` and says "Install
a newer Python."
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_doctor_oversized_store_348 import _project, _run


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


def _below_floor_only_env(tmp_path: Path) -> dict[str, str]:
    """Shadow EVERY name `_remember_python`'s candidate loop tries
    ("python3", "python", "python3.9".."python3.13") with a below-floor
    stub, PREPENDED to the real PATH rather than replacing it.

    Unlike test_detect_tools_floor_1003.py's `_isolated_env`, doctor.sh
    (not detect-tools.sh alone) needs real coreutils (`rm`, `mktemp`, ...)
    for sections before the Tools block, so the real PATH cannot be
    dropped outright here. That means a real, floor-meeting interpreter
    already on PATH (this dev machine has homebrew python3.13, and a CI
    image may have several python3.N's via setup-python) would otherwise
    be found and accepted, defeating the "every candidate below the
    floor" premise -- so every name the loop can try is stubbed and
    shadowed, not just "python3".
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    for name in ("python3", "python", "python3.13", "python3.12",
                 "python3.11", "python3.10", "python3.9"):
        stub = bindir / name
        stub.write_text('#!/bin/sh\necho "Python 3.8.10"\nexit 0\n', encoding="utf-8")
        stub.chmod(0o755)
    cache_tmpdir = tmp_path / "cachetmp"
    cache_tmpdir.mkdir(exist_ok=True)
    return {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(cache_tmpdir),
    }


def test_the_fatal_fail_line_names_the_floor(tmp_path):
    """The reporter's exact gap: the FAIL line must say what to install."""
    home, project, remember = _project(tmp_path)
    extra_env = _below_floor_only_env(tmp_path)

    result = _run(home, project, remember, extra_env)

    assert result.returncode == 0, result.stderr
    assert "FAIL Python:" in result.stdout, result.stdout
    assert "below the floor this plugin supports" in result.stdout, (
        "the existing FATAL text (relayed from detect-tools.sh) is gone -- "
        "this must be an addition, not a replacement:\n" + result.stdout
    )
    assert "3.9" in result.stdout, (
        "the FAIL Python line never names the floor this plugin requires, "
        "so a user reading it cannot tell which version to install:\n"
        + result.stdout
    )
    assert "install" in result.stdout.lower(), (
        "the FAIL Python line names no remedy:\n" + result.stdout
    )


def test_a_healthy_python_gets_no_floor_remedy_line(tmp_path):
    """Positive control: an ordinary, floor-meeting install must not sprout
    a 'need Python 3.9+' line in its OK report -- a check that fires
    unconditionally would tell every healthy install it is unsupported."""
    home, project, remember = _project(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "OK   python:" in result.stdout, result.stdout
    assert "need Python" not in result.stdout, (
        "a healthy, floor-meeting Python was told to install a newer one:\n"
        + result.stdout
    )
