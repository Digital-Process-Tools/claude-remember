"""Real-wscript.exe evidence for #1002 round 2 (release-audit v0.42.3 gate 3).

The pre-existing tests/test_windows_hidden_detach_1002.py drives the Windows
hidden-launch dispatch DECISION against a bash stub standing in for
wscript.exe -- it cannot see either side of the exec->CreateProcess->WSH
argv parse, so it could not settle two REASONED-only claims raised against
the fix for #1002:

  A. the old c_script shape embedded a literal double quote in a single
     argv entry handed to the real wscript.exe; whether that quote
     survived the hop unmangled was never observed on a real Windows host.
  B. if Windows Script Host itself is disabled by policy, the old shape
     backgrounded wscript.exe and unconditionally returned 0, so the
     caller's own nohup fallback never fired -- a silent skip.

This module runs ONLY on a real Windows host (skipped everywhere else --
never the inverse: on windows-latest these tests must run, never skip) and
drives the REAL scripts/windows-hidden-run.vbs through the REAL
wscript.exe, with a real on-disk save script that writes a marker file
rather than a captured-argv stub. A marker that appears means a save
genuinely happened; one that does not is the silent-skip failure mode both
trap fragments describe.
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
LIB = (REPO_ROOT / "scripts" / "lib-detach.sh").as_posix()

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="#1002 round 2 real-wscript evidence -- only meaningful on a real "
           "Windows host; never skipped on windows-latest itself",
)

BASH = resolve_bash()

_WSH_KEY = r"HKCU\Software\Microsoft\Windows Script Host\Settings"


def _run(script: str, env: dict | None = None, timeout: float = 20,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    """CI finding (#1002 round 3, job 114204833202 and siblings): a call
    with no `cwd` inherits pytest's own cwd (the repo checkout root on
    windows-latest). Any relative-path side effect the hidden route or its
    quoting produces then lands IN THE REPO rather than under tmp_path,
    tripping conftest.py's own repo-mutation guard at session teardown --
    observed as two stray `out.log`-shaped files in the checkout after the
    v0.42.3 module's quote-corruption cases ran. Callers pass `cwd=tmp_path`
    (or a subdirectory of it) so any such leak lands somewhere pytest
    cleans up on its own, never in the tree the suite was launched from."""
    assert BASH, "no real bash resolvable on this Windows host -- cannot drive the hidden-launch route"
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run([BASH, "-c", script], env=full_env, capture_output=True,
                           text=True, timeout=timeout, check=False,
                           cwd=str(cwd) if cwd else None)


def _poll_for_marker(marker: Path, timeout: float = 10.0) -> bool:
    """Bounded wait for MARKER to exist and carry content -- the detached
    route's own child may still be starting up when the launcher call
    itself returns."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists() and marker.read_text(encoding="utf-8", errors="replace").strip():
            return True
        time.sleep(0.2)
    return marker.exists() and bool(marker.read_text(encoding="utf-8", errors="replace").strip())


def _save_script(directory: Path, marker: Path) -> Path:
    """A real, on-disk save script -- not a captured-argv stub -- that
    writes a known value to MARKER once actually invoked, the same shape
    the real save-session.sh call site hands _remember_detach_windows."""
    path = directory / "save.sh"
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("#!/bin/sh\n" + f'printf "saved\\n" >> "{marker.as_posix()}"\n')
    path.chmod(0o755)
    return path


def test_real_wscript_plain_path_detaches_and_saves(tmp_path):
    """Case (a): plain path, no quoting hazards. Drives the real
    _remember_detach_windows against the real wscript.exe and the real
    windows-hidden-run.vbs; a marker written by the save script is the
    only evidence that the hidden route actually ran anything."""
    marker = tmp_path / "marker.txt"
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    save = _save_script(tmp_path, marker)
    script = f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; echo "RC=$?"'
    r = _run(script, env={"TMPDIR": str(tmp_path)}, cwd=tmp_path)
    assert "RC=0" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    outfile_text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else "<missing>"
    assert _poll_for_marker(marker), (
        "marker never appeared on a plain path -- the REAL wscript.exe hidden-"
        "launch route returned success but never actually ran the save script "
        f"(#1002 round 2 trap A/B evidence)\nlauncher outfile: {outfile_text}"
    )


def test_real_wscript_path_with_space_detaches_and_saves(tmp_path):
    """Case (b): outfile, pidfile and the save script itself all live under
    a directory containing a space -- the shape most likely to expose a
    quoting hazard across the exec->CreateProcess->WSH argv hop (trap A)."""
    spacedir = tmp_path / "with space"
    spacedir.mkdir()
    marker = spacedir / "marker.txt"
    out = spacedir / "out.log"
    pid = spacedir / "pid"
    save = _save_script(spacedir, marker)
    script = f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; echo "RC=$?"'
    r = _run(script, env={"TMPDIR": str(tmp_path)}, cwd=spacedir)
    assert "RC=0" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    outfile_text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else "<missing>"
    assert _poll_for_marker(marker), (
        "marker never appeared with a space in the path -- a spaced argument "
        "may be getting mangled across the wscript.exe/WSH argv hop "
        f"(#1002 round 2 trap A evidence)\nlauncher outfile: {outfile_text}"
    )


def _wsh_enabled_value() -> str | None:
    r = subprocess.run(["reg", "query", _WSH_KEY, "/v", "Enabled"],
                        capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None  # value does not exist today -- WSH defaults to enabled
    for line in r.stdout.splitlines():
        if "Enabled" in line and "REG_DWORD" in line:
            return line.strip().rsplit(None, 1)[-1]
    return None


@pytest.fixture
def wsh_disabled():
    """Disable Windows Script Host for the current user via the real
    registry key the two trap fragments name, and restore whatever was
    there before -- deleting the value entirely if it did not exist,
    never leaving a test-induced change behind regardless of outcome."""
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


def test_real_wscript_wsh_disabled_still_saves_via_fallback(tmp_path, wsh_disabled):
    """Case (c), #1002 round 2 trap B: with Windows Script Host disabled by
    policy, the hidden route cannot run wscript.exe at all. Reproduces the
    exact caller shape post-tool-hook.sh uses
    (`if ! _remember_detach_windows ...; then nohup ...; fi`) rather than
    the whole hook script, and asserts the save STILL happens -- via the
    nohup fallback -- because _remember_detach_windows now reports the
    launch failure instead of swallowing it."""
    marker = tmp_path / "marker.txt"
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    save = _save_script(tmp_path, marker)
    script = (
        f'. "{LIB}"; '
        f'if ! _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; then '
        f'nohup "{save.as_posix()}" >> "{out}" 2>&1 & echo $! > "{pid}"; fi'
    )
    # #1002 round 2 live finding: this is exactly the case that hung CI
    # once already (a modal WSH prompt on a headless runner never
    # resolving on its own) before lib-detach.sh's own call grew a hard
    # `timeout 10` bound. 30s here gives that bound, plus bash/registry
    # overhead, comfortable room without letting a still-hanging call
    # silently re-stall this one test for the job's full timeout-minutes.
    r = _run(script, timeout=30, env={"TMPDIR": str(tmp_path)}, cwd=tmp_path)
    outfile_text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else "<missing>"
    assert _poll_for_marker(marker), (
        "WSH disabled and the save never happened -- the fallback the real "
        "caller relies on did not fire, which means _remember_detach_windows "
        "is still not reporting the launch failure (#1002 round 2 trap B)\n"
        f"stdout={r.stdout!r} stderr={r.stderr!r} launcher outfile: {outfile_text}"
    )


def test_real_wscript_healthy_path_latency(tmp_path, capsys):
    """#1002 round 3 (maintainer instruction): measure the healthy-path cost
    on a real Windows host -- hook entry to return -- on the hidden route
    versus the plain nohup route, over several runs, so the watchdog bound
    in scripts/lib-detach.sh (a fixed `sleep` call, 5s as of round 3) can
    be sized against a real number instead of a guess. #913 (PostToolUse
    latency) is the open issue this feeds.

    This is a MEASUREMENT, not a correctness assertion -- it has its own
    loose sanity bound (well under the watchdog's own bound) so a genuine
    regression still fails it, but its real output is the printed numbers,
    read from the CI log for this job (see docs/windows.md and
    trap.d/1002.*.md for where the chosen bound and its job-id evidence are
    recorded)."""
    runs = 5

    def _timed_hidden(i: int) -> float:
        marker = tmp_path / f"hidden-marker-{i}.txt"
        out = tmp_path / f"hidden-out-{i}.log"
        pid = tmp_path / f"hidden-pid-{i}"
        save = _save_script(tmp_path, marker)
        script = f'. "{LIB}"; _remember_detach_windows "{out}" "{pid}" "{save.as_posix()}"; echo "RC=$?"'
        t0 = time.monotonic()
        # #1002 round 3 CI finding (job 114204833202 and siblings): without a
        # per-call TMPDIR override, this call shares a real-host-wide
        # $TMPDIR/$PATH with every earlier test in the same pytest process --
        # including test_real_wscript_wsh_disabled_still_saves_via_fallback,
        # which deliberately makes the hidden route fail and so publishes
        # scripts/lib-detach.sh's own "unusable" verdict cache. Without this
        # override that verdict leaks into every later call on the same
        # host, including this one, which would otherwise observe a healthy
        # route as permanently cached-unusable from an unrelated earlier
        # test rather than from its own real outcome.
        r = _run(script, env={"TMPDIR": str(tmp_path)}, cwd=tmp_path)
        assert "RC=0" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
        assert _poll_for_marker(marker), f"hidden route run {i}: marker never appeared"
        return time.monotonic() - t0

    def _timed_nohup(i: int) -> float:
        marker = tmp_path / f"nohup-marker-{i}.txt"
        out = tmp_path / f"nohup-out-{i}.log"
        pid = tmp_path / f"nohup-pid-{i}"
        save = _save_script(tmp_path, marker)
        script = f'nohup "{save.as_posix()}" >> "{out}" 2>&1 & echo $! > "{pid}"'
        t0 = time.monotonic()
        _run(script, cwd=tmp_path)
        assert _poll_for_marker(marker), f"nohup route run {i}: marker never appeared"
        return time.monotonic() - t0

    hidden_times = [_timed_hidden(i) for i in range(runs)]
    nohup_times = [_timed_nohup(i) for i in range(runs)]

    with capsys.disabled():
        print(f"\n#1002 round 3 healthy-path latency (N={runs}, real wscript.exe):")
        print(f"  hidden route : {hidden_times} max={max(hidden_times):.3f}s")
        print(f"  nohup route  : {nohup_times} max={max(nohup_times):.3f}s")

    # Loose sanity bound only, well under the watchdog itself (5s as of
    # round 3 -- reduced from 10s against this very test's own measurement
    # on real windows-latest CI, see scripts/lib-detach.sh's own comment
    # at the watchdog call site). A healthy hidden-route call taking
    # anywhere close to the watchdog bound would mean the "smallest bound
    # the healthy path reliably beats" reasoning is wrong and needs
    # revisiting against these actual numbers, not that this test or the
    # bound should simply be raised to match.
    assert max(hidden_times) < 2.5, (
        f"healthy hidden-route call took {max(hidden_times):.3f}s -- more than half "
        f"the watchdog bound; re-measure before trusting that bound (#1002 round 3)"
    )


def test_real_nohup_path_writes_marker_positive_control(tmp_path):
    """Case (d): positive control for the three assertions above. A 'the
    save must still happen' assertion passes just as readily when the
    whole harness is silently broken as when the fallback genuinely fired
    -- this proves the marker-and-poll mechanism itself detects a real,
    successful save, independent of _remember_detach_windows entirely."""
    marker = tmp_path / "marker.txt"
    out = tmp_path / "out.log"
    pid = tmp_path / "pid"
    save = _save_script(tmp_path, marker)
    script = f'nohup "{save.as_posix()}" >> "{out}" 2>&1 & echo $! > "{pid}"'
    _run(script, cwd=tmp_path)
    assert _poll_for_marker(marker), "the nohup path itself never wrote the marker -- harness is broken"
