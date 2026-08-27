"""Regression tests for #362: remember-config-<pid>.json leaked into the OS
temp root on Windows, where the EXIT trap that is supposed to remove it does
not reliably fire for this plugin's short-lived hook processes -- one machine
accumulated 23,908 of them in %TEMP%.

Two changes are under test:

1. The merged-config file now lives under $REMEMBER_DIR/tmp -- a directory
   this plugin already owns and already uses for its own per-invocation
   scratch files -- instead of directly in the shared OS temp root. A stale
   file there is unambiguously this plugin's own leak, never another user's
   or another app's file on a shared machine.
2. Before writing its own file, lib-memory-dir.sh sweeps
   remember-config-*.json entries in that directory whose mtime is older
   than a threshold generous enough that no legitimately-running invocation
   of this plugin's own hook scripts could still be using one. This is the
   backstop for exactly the case the EXIT trap cannot cover: a process that
   never runs its exit path at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVE_PATHS_SH = REPO_ROOT / "scripts" / "resolve-paths.sh"
BOOTSTRAP_DIRS_SH = REPO_ROOT / "scripts" / "bootstrap-dirs.sh"

# Comfortably older than the sweep's own threshold (30 minutes) without
# depending on knowing its exact value.
_STALE_AGE_SECONDS = 60 * 60 * 2
_FRESH_AGE_SECONDS = 5


def _bash_path(p) -> str:
    """`C:\\Users\\x` -> `C:/Users/x`; unchanged on POSIX (see test_security_fixes)."""
    return str(p).replace("\\", "/")


def _find_bash():
    if sys.platform != "win32":
        return "bash"
    import shutil

    candidates = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    resolved = shutil.which("bash")
    if resolved and "git" in resolved.replace("\\", "/").lower():
        return resolved
    return None


_BASH = _find_bash()
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def _make_project(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude" / "remember").mkdir(parents=True)
    (project / ".remember").mkdir(parents=True)
    return project


def _source_bootstrap(project, isolated_tmp, inner_commands: str = ""):
    """Run resolve-paths.sh + bootstrap-dirs.sh (which sources
    lib-memory-dir.sh) as a real subprocess and return the completed process.
    ``inner_commands`` runs afterward, still inside the sourced environment,
    so callers can inspect $REMEMBER_DIR/$_merged_cfg/etc. before the shell
    (and its EXIT trap) exits."""
    script = f"""
set -e
export CLAUDE_PROJECT_DIR="{_bash_path(project)}"
export PIPELINE_DIR="{_bash_path(REPO_ROOT)}"
source "{_bash_path(RESOLVE_PATHS_SH)}"
export TMPDIR="{_bash_path(isolated_tmp)}"
source "{_bash_path(BOOTSTRAP_DIRS_SH)}"
{inner_commands}
"""
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": _bash_path(project.parent)},
        check=False,
    )


def _remember_tmp(project) -> Path:
    return project / ".remember" / "tmp"


def _norm_sep(s: str) -> str:
    """Backslash and forward slash both name the same Windows path, and this
    codebase deliberately produces BOTH in one string on real Windows/Git
    Bash: resolve-paths.sh's OSTYPE=msys/cygwin branch (#263) rewrites
    PROJECT_DIR into native `C:\\...` form so Claude Code's own slug
    computation matches, while every `/tmp/remember-config-$$.json` this
    plugin appends afterward is bash string concatenation using `/`
    literally. The two halves land in one path -- `C:\\Users\\...\\proj` then
    `/.remember/tmp/...` -- and Windows treats that mix as one valid,
    working location; a raw substring compare across it does not. Observed
    on CI's windows-latest leg (#362 follow-up): the un-normalized compare
    failed on exactly this split, at exactly the project-directory boundary,
    with the two sides identical in every character except separator style.
    Comparing after normalizing both sides to `/` makes the check agnostic
    to which half of a path went through which code, which is the only
    thing that changes between platforms -- it does not loosen what counts
    as a match: the fallback ($SYS_TMPDIR, not $REMEMBER_DIR/tmp) is a
    genuinely different string even after normalization, so a real
    regression in the relocation still fails this exactly as before."""
    return s.replace("\\", "/")


def test_merged_config_lives_under_remember_dir_tmp(tmp_path):
    """The per-invocation merged config is written under $REMEMBER_DIR/tmp,
    not directly into the shared OS temp root -- a directory this plugin
    owns rather than one shared with every other app on the machine."""
    project = _make_project(tmp_path)
    isolated_tmp = tmp_path / "systmp"
    isolated_tmp.mkdir()

    result = _source_bootstrap(
        project,
        isolated_tmp,
        'echo "MERGED=$REMEMBER_CONFIG"',
    )
    assert result.returncode == 0, result.stderr

    merged_line = [l for l in result.stdout.splitlines() if l.startswith("MERGED=")]
    assert merged_line, f"no MERGED= line in stdout: {result.stdout!r}"
    merged_path = merged_line[0][len("MERGED="):]

    remember_tmp = _remember_tmp(project)
    assert _norm_sep(str(remember_tmp)) in _norm_sep(merged_path), (
        f"expected the merged config under {remember_tmp}, got {merged_path!r}"
    )
    assert not list(isolated_tmp.glob("remember-config-*.json")), (
        "merged config was written into the shared OS temp root, not "
        "$REMEMBER_DIR/tmp"
    )


def test_norm_sep_tolerates_the_real_windows_ci_mismatch():
    """Pins the exact CI failure this codebase can hit on real Windows/Git
    Bash without needing a Windows box to reproduce it: PR #371's
    windows-latest leg failed test_merged_config_lives_under_remember_dir_tmp
    with

        expected .../proj\\\\.remember\\\\tmp
        got      .../proj/.remember/tmp/remember-config-1307.json

    -- identical except that resolve-paths.sh's OSTYPE=msys/cygwin branch
    (#263) had rewritten the project-directory prefix into native `C:\\\\...`
    form while this plugin's own `/tmp/remember-config-$$.json` suffix stayed
    forward-slashed. That is confirmed, by reading resolve-paths.sh, to be
    deliberate pre-existing behavior unrelated to #362 -- so the relocation
    genuinely happened and the assertion was wrong to fail. This test uses
    the literal strings from that CI failure (with the prefix shortened) so
    a future change to _norm_sep cannot silently stop tolerating this exact
    shape."""
    expected = "C:\\Users\\runneradmin\\proj\\.remember\\tmp"
    got = "C:\\Users\\runneradmin\\proj/.remember/tmp/remember-config-1307.json"
    assert _norm_sep(expected) in _norm_sep(got)


def test_norm_sep_still_catches_a_genuine_fallback_to_systmp():
    """The paired negative control: normalizing separators must not turn the
    check into one that passes when the relocation never happened at all --
    i.e. the file landed in the $SYS_TMPDIR fallback instead of
    $REMEMBER_DIR/tmp. That is a different path, not a different separator,
    and must still fail regardless of which slashes either side uses."""
    expected = "C:\\Users\\runneradmin\\proj\\.remember\\tmp"
    fallback_got = "C:\\Users\\runneradmin\\AppData\\Local\\Temp/remember-config-1307.json"
    assert _norm_sep(expected) not in _norm_sep(fallback_got)


def test_stale_merged_config_is_swept(tmp_path):
    """A remember-config-*.json left behind by a process whose EXIT trap
    never ran (killed rather than exited) does not survive indefinitely --
    the next invocation sweeps it before writing its own file. This is the
    'must fire' half: pairs with test_fresh_concurrent_config_is_not_swept
    below, which is the 'must not fire' half over the same fixture."""
    project = _make_project(tmp_path)
    isolated_tmp = tmp_path / "systmp"
    isolated_tmp.mkdir()

    remember_tmp = _remember_tmp(project)
    remember_tmp.mkdir(parents=True, exist_ok=True)
    stale = remember_tmp / "remember-config-99999.json"
    stale.write_text("{}")
    old = time.time() - _STALE_AGE_SECONDS
    os.utime(stale, (old, old))

    result = _source_bootstrap(project, isolated_tmp)
    assert result.returncode == 0, result.stderr

    assert not stale.exists(), (
        "a remember-config-*.json older than the sweep threshold survived a "
        "later invocation -- this is the leak #362 reports on Windows, where "
        "the EXIT trap that is supposed to remove it does not reliably fire"
    )


def test_fresh_concurrent_config_is_not_swept(tmp_path):
    """A remember-config-*.json that looks like a live concurrent invocation
    (fresh mtime) must survive the sweep -- the positive control for the test
    above. A sweep that is not gated on age would delete a file a concurrent
    hook process is still relying on for its own config reads."""
    project = _make_project(tmp_path)
    isolated_tmp = tmp_path / "systmp"
    isolated_tmp.mkdir()

    remember_tmp = _remember_tmp(project)
    remember_tmp.mkdir(parents=True, exist_ok=True)
    fresh = remember_tmp / "remember-config-88888.json"
    fresh.write_text("{}")
    recent = time.time() - _FRESH_AGE_SECONDS
    os.utime(fresh, (recent, recent))

    result = _source_bootstrap(project, isolated_tmp)
    assert result.returncode == 0, result.stderr

    assert fresh.exists(), (
        "a fresh remember-config-*.json (simulating a live concurrent "
        "invocation) was removed by the sweep -- the age threshold is too "
        "tight or missing entirely"
    )


def test_sweep_does_not_touch_unrelated_stale_files(tmp_path):
    """The sweep is scoped to this plugin's own remember-config-*.json naming
    convention -- an old file of any other name in the same directory (a
    lock, a marker, anything not ours to remove on this pattern alone) must
    not be swept, even though it sits in a directory this plugin owns."""
    project = _make_project(tmp_path)
    isolated_tmp = tmp_path / "systmp"
    isolated_tmp.mkdir()

    remember_tmp = _remember_tmp(project)
    remember_tmp.mkdir(parents=True, exist_ok=True)
    unrelated = remember_tmp / "some-other-marker.json"
    unrelated.write_text("{}")
    old = time.time() - _STALE_AGE_SECONDS
    os.utime(unrelated, (old, old))

    result = _source_bootstrap(project, isolated_tmp)
    assert result.returncode == 0, result.stderr

    assert unrelated.exists(), (
        "the sweep removed a file that does not match the "
        "remember-config-*.json naming convention -- it must be scoped to "
        "this plugin's own files, not anything old in $REMEMBER_DIR/tmp"
    )


def test_killed_process_leak_is_eventually_swept(tmp_path):
    """End-to-end simulation of the actual defect: a hook process whose EXIT
    trap never runs because the process is killed rather than allowed to
    exit (the Windows/Git Bash failure mode #362 reports). The file it wrote
    is left behind; once it is old enough, a later invocation sweeps it."""
    project = _make_project(tmp_path)
    isolated_tmp = tmp_path / "systmp"
    isolated_tmp.mkdir()

    script = f"""
export CLAUDE_PROJECT_DIR="{_bash_path(project)}"
export PIPELINE_DIR="{_bash_path(REPO_ROOT)}"
source "{_bash_path(RESOLVE_PATHS_SH)}"
export TMPDIR="{_bash_path(isolated_tmp)}"
source "{_bash_path(BOOTSTRAP_DIRS_SH)}"
echo "MERGED=$REMEMBER_CONFIG"
sleep 30
"""
    proc = subprocess.Popen(
        [_BASH, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "HOME": _bash_path(project.parent)},
    )
    merged_path = None
    deadline = time.time() + 10
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.startswith("MERGED="):
                merged_path = line[len("MERGED="):].strip()
                break
    finally:
        # kill -9: bypasses the EXIT trap entirely, unlike terminate()/SIGTERM
        # which bash would still be able to trap.
        proc.kill()
        proc.wait(timeout=10)

    assert merged_path, (
        f"never saw a MERGED= line before the deadline; stderr so far: "
        f"{proc.stderr.read() if proc.stderr else ''!r}"
    )
    leaked = Path(merged_path)
    assert leaked.exists(), (
        "the killed process's merged-config file was not created -- test "
        "setup is broken, not the fix"
    )

    # Backdate it past the sweep threshold rather than sleeping for real.
    old = time.time() - _STALE_AGE_SECONDS
    os.utime(leaked, (old, old))

    result = _source_bootstrap(project, isolated_tmp)
    assert result.returncode == 0, result.stderr

    assert not leaked.exists(), (
        "a remember-config-*.json left behind by a killed process (its EXIT "
        "trap never ran) survived indefinitely -- the sweep is the only "
        "mechanism that can ever remove it"
    )
