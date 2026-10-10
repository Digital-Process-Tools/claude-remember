"""Real-wscript.exe evidence for the ORIGINAL v0.42.3 shipped hidden-launch
shape (#1002 round 3, maintainer instruction).

Round 2 (tests/test_windows_real_wscript_1002.py) fixed scripts/lib-detach.sh
based on two REASONED-only claims raised by the v0.42.3 release audit --
trap.d/1002.detach-windows-silent-skip-after-launch.md (trap B: the old code
backgrounds wscript.exe with a trailing "&" and unconditionally returns 0, so
a post-launch failure is invisible to the caller) and
trap.d/1002.wsh-strips-quotes-in-hidden-launch-c-script.md (trap A: the old
code embeds a literal double quote in one argv entry handed to wscript.exe,
and WSH's own argument parser does not honour backslash-escaped quotes) --
and then observed the NEW shape working correctly on real Windows Script
Host. It never ran the OLD, actually-shipped v0.42.3 shape against real WSH,
so whether v0.42.3 itself is actually broken in production was never settled
by observation, only reasoned about. This module closes that gap: it drives
the EXACT bytes v0.42.3 shipped (vendored under tests/fixtures/v0_42_3/, see
NOTE.md there for why vendored rather than fetched by tag at test time)
against a real wscript.exe, on the same three cases round 2 already proved
the NEW shape handles -- plain path, a path with a space, and WSH disabled.

This module runs ONLY on a real Windows host (skipped everywhere else --
never the inverse: on windows-latest these tests must run, never skip), the
same convention test_windows_real_wscript_1002.py uses, for the same reason:
nothing about WSH's own argv parser or a disabled-WSH blocking prompt can be
observed from a stub.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "v0_42_3"
LIB = (FIXTURE_DIR / "lib-detach.sh").as_posix()

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="#1002 round 3 v0.42.3 evidence -- only meaningful on a real "
           "Windows host; never skipped on windows-latest itself",
)

BASH = resolve_bash()

_WSH_KEY = r"HKCU\Software\Microsoft\Windows Script Host\Settings"


def _run(script: str, env: dict | None = None, timeout: float = 20) -> subprocess.CompletedProcess:
    assert BASH, "no real bash resolvable on this Windows host -- cannot drive the hidden-launch route"
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([BASH, "-c", script], env=full_env, capture_output=True,
                           text=True, timeout=timeout, check=False)


def _poll_for_marker(marker: Path, timeout: float = 10.0) -> bool:
    """Bounded wait for MARKER to exist and carry content. The v0.42.3 shape
    never waits on wscript.exe at all (backgrounded with a trailing "&" and
    disowned), so a marker that will eventually appear may still be in
    flight when the launcher call itself has already returned."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists() and marker.read_text(encoding="utf-8", errors="replace").strip():
            return True
        time.sleep(0.2)
    return marker.exists() and bool(marker.read_text(encoding="utf-8", errors="replace").strip())


def _save_script(directory: Path, marker: Path) -> Path:
    path = directory / "save.sh"
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("#!/bin/sh\n" + f'printf "saved\\n" >> "{marker.as_posix()}"\n')
    path.chmod(0o755)
    return path


def test_v0_42_3_plain_path_detaches_and_saves(tmp_path):
    """Case (a): plain path, no quoting hazards. v0.42.3's own
    _remember_detach_windows signature is identical to today's
    (OUTFILE PIDFILE CMD...) -- only its internals differ (an embedded
    bash -c "$c_script" instead of a dedicated pidwrap argv entry)."""
    marker = tmp_path / "marker.txt"
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    save = _save_script(tmp_path, marker)
    script = f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; echo "RC=$?"'
    r = _run(script)
    outfile_text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else "<missing>"
    assert "RC=0" in r.stdout, f"v0.42.3 shape: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert _poll_for_marker(marker), (
        "v0.42.3 OBSERVED: marker never appeared on a plain path -- the SHIPPED "
        "hidden-launch route returned (unconditional 0, never waited) but never "
        f"actually ran the save script\nlauncher outfile: {outfile_text}\n"
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )


def test_v0_42_3_path_with_space_detaches_and_saves(tmp_path):
    """Case (b): a space in the path -- the shape most likely to expose
    trap A (WSH's argv parser stripping the embedded literal quote in
    v0.42.3's own c_script, corrupting the "$0" "$@" it relies on)."""
    spacedir = tmp_path / "with space"
    spacedir.mkdir()
    marker = spacedir / "marker.txt"
    out = spacedir / "out.log"
    pid = spacedir / "pid"
    save = _save_script(spacedir, marker)
    script = f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; echo "RC=$?"'
    r = _run(script)
    outfile_text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else "<missing>"
    assert "RC=0" in r.stdout, f"v0.42.3 shape: stdout={r.stdout!r} stderr={r.stderr!r}"
    assert _poll_for_marker(marker), (
        "v0.42.3 OBSERVED: marker never appeared with a space in the path -- "
        "trap A (WSH argv parser stripping the embedded literal quote in the "
        f"shipped c_script) is the leading suspect\nlauncher outfile: {outfile_text}\n"
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )


def _wsh_enabled_value() -> str | None:
    r = subprocess.run(["reg", "query", _WSH_KEY, "/v", "Enabled"],
                        capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "Enabled" in line and "REG_DWORD" in line:
            return line.strip().rsplit(None, 1)[-1]
    return None


@pytest.fixture
def wsh_disabled():
    """Same registry fixture as test_windows_real_wscript_1002.py -- disable
    WSH for the current user, restore whatever was there before regardless
    of outcome."""
    original = _wsh_enabled_value()
    add = subprocess.run(
        ["reg", "add", _WSH_KEY, "/v", "Enabled", "/t", "REG_DWORD", "/d", "0", "/f"],
        capture_output=True, text=True, check=False,
    )
    assert add.returncode == 0, f"could not disable WSH via registry: {add.stdout!r} {add.stderr!r}"
    try:
        yield
    finally:
        if original is None:
            subprocess.run(["reg", "delete", _WSH_KEY, "/v", "Enabled", "/f"],
                            capture_output=True, check=False)
        else:
            subprocess.run(
                ["reg", "add", _WSH_KEY, "/v", "Enabled", "/t", "REG_DWORD", "/d", original, "/f"],
                capture_output=True, check=False,
            )


def test_v0_42_3_wsh_disabled_silent_skip(tmp_path, wsh_disabled):
    """Case (c), trap B: with WSH disabled, v0.42.3's own
    _remember_detach_windows backgrounds wscript.exe with a trailing "&" and
    unconditionally returns 0 -- never waiting on it, so the caller's own
    `if ! _remember_detach_windows ...; then nohup ...; fi` fallback has no
    non-zero return to react to regardless of what wscript.exe eventually
    does. Reproduces the exact caller shape post-tool-hook.sh uses. Unlike
    round 2's NEW-shape test, this one has no watchdog to hit -- the old
    code never waits at all -- so a hang here, if any, happens in a
    detached, disowned background job the test's own subprocess.run does
    not block on; the 20s default timeout covers only the bash call that
    launches it, not whatever wscript.exe itself goes on to do."""
    marker = tmp_path / "marker.txt"
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    save = _save_script(tmp_path, marker)
    script = (
        f'. "{LIB}"; '
        f'if ! _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; then '
        f'nohup "{save.as_posix()}" >> "{out}" 2>&1 & echo $! > "{pid}"; fi'
    )
    r = _run(script, timeout=30)
    outfile_text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else "<missing>"
    assert _poll_for_marker(marker), (
        "v0.42.3 OBSERVED: WSH disabled and the save never happened within the "
        "poll window -- the shipped code's unconditional return 0 means the "
        "caller's nohup fallback never fires (trap B, #1002 round 1 release "
        f"audit)\nstdout={r.stdout!r} stderr={r.stderr!r} launcher outfile: {outfile_text}"
    )


def test_v0_42_3_positive_control(tmp_path):
    """Positive control, same role as test_windows_real_wscript_1002.py's
    own: proves the marker-and-poll mechanism detects a real, successful
    save on its own, independent of _remember_detach_windows entirely."""
    marker = tmp_path / "marker.txt"
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    save = _save_script(tmp_path, marker)
    script = f'nohup "{save.as_posix()}" >> "{out}" 2>&1 & echo $! > "{pid}"'
    _run(script)
    assert _poll_for_marker(marker), "the nohup path itself never wrote the marker -- harness is broken"
