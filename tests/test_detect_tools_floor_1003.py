"""detect-tools.sh rejects a Python below the supported floor (#1003).

The reporter's bug: `pipeline/_tz.py` imports `zoneinfo`, stdlib only since
3.9, with no guard -- consolidation crashes on anything older. Nothing in
`detect-tools.sh`'s own candidate loop checked the version it found before
picking it: any interpreter that answered `-V` with exit 0 was accepted,
2.x included. `scripts/doctor.sh` then reported "OK python ... 3.8.10" and
"capture is working" -- capture (PostToolUse) never touches zoneinfo, so it
genuinely was fine, while consolidation failed silently for months.

The fix: `_remember_py_meets_floor` rejects anything below the floor, and
the candidate loop tries versioned fallbacks (python3.9 through python3.13)
before giving up -- never a guarded `zoneinfo` import, since the floor this
repo actually supports is 3.9 (tests/pep604_floor.py's declared_floor(),
read from .github/workflows/tests.yml's own CI matrix).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bash_runner import resolve_bash

REPO_ROOT = Path(__file__).resolve().parent.parent
DETECT = REPO_ROOT / "scripts" / "detect-tools.sh"

sys.path.insert(0, str(REPO_ROOT))
from tests.pep604_floor import declared_floor

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="no usable bash found")


def _stub(bindir: Path, name: str, version_text: str, exit_code: int = 0) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text(
        f'#!/bin/sh\necho "{version_text}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _source(env):
    return subprocess.run(
        [BASH, "-c", f'source "{DETECT.as_posix()}"; echo "PYTHON=$PYTHON"'],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def _real_path_without_other_pythons() -> str:
    """The real PATH, minus any directory that itself holds a `python3*`
    binary -- needed so a test asserting "every candidate is below the
    floor" is not accidentally falsified by a genuinely floor-meeting
    interpreter the host happens to have installed elsewhere on PATH
    (CI images commonly ship several python3.X siblings in the same
    directory).

    Self-review correction: this does NOT reliably preserve coreutils
    (mktemp, sort, grep). On a Linux image where /usr/bin holds both
    python3 and those binaries in the SAME directory (the common case on
    ubuntu-latest), the whole directory is dropped together with it. The
    two tests that pass this filtered PATH do not assert anything about
    the tool-verdict cache, so a silently-failing `mktemp` there (that
    function already degrades to `return 0` with nothing written when
    `mktemp` is missing) costs nothing today -- but a FUTURE test built
    on this same filtered PATH that does need the cache to actually
    publish would fail for an environment reason that looks like a
    detect-tools.sh bug. Not fixed here (the two current call sites do
    not need it); flagged so nobody trusts the coreutils half of this
    docstring if cache-publish ever needs asserting through it."""
    import glob

    kept = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and glob.glob(os.path.join(entry, "python3*")):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


def _isolated_env(tmp_path: Path, bindir_value: str, base_path: str | None = None) -> dict:
    """PREPEND bindir to a real PATH, never replace it (#1003 self-review):
    a replaced PATH loses `mktemp`, used by `_remember_tools_cache_publish`,
    which then fails silently and leaves the cache file unwritten -- a false
    negative that looks exactly like "the cache was never refreshed" without
    ever exercising that code path for real. `base_path` defaults to the
    real PATH; pass `_real_path_without_other_pythons()` when the test's
    own premise requires no OTHER python on PATH."""
    cache_tmpdir = tmp_path / "tmp1"
    cache_tmpdir.mkdir(exist_ok=True)
    if base_path is None:
        base_path = os.environ["PATH"]
    return {
        **os.environ,
        "PATH": f"{bindir_value}{os.pathsep}{base_path}",
        "HOME": str(tmp_path),
        "TMPDIR": str(cache_tmpdir),
    }


def test_the_floor_matches_the_ci_matrix():
    """The bash-side floor is not a second, independently-chosen "3.9" --
    pinned against tests/pep604_floor.py's own derivation from the CI
    matrix so the two cannot silently drift apart."""
    ci_floor = declared_floor(REPO_ROOT)
    assert ci_floor is not None, "could not read the CI matrix floor at all"

    script = (
        f'source "{DETECT.as_posix()}" >/dev/null 2>&1; '
        'echo "$_REMEMBER_PY_FLOOR_MAJOR.$_REMEMBER_PY_FLOOR_MINOR"'
    )
    result = subprocess.run(
        [BASH, "-c", script],
        env={**os.environ, "HOME": "/nonexistent-does-not-matter"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    bash_floor = tuple(int(x) for x in result.stdout.strip().split("."))
    assert bash_floor == ci_floor, (
        f"detect-tools.sh's own floor ({bash_floor}) no longer matches the "
        f"CI matrix's ({ci_floor}) -- update one or the other, not just one"
    )


def test_a_floor_meeting_interpreter_is_accepted(tmp_path):
    """Positive control: a python3 reporting a version AT the floor must
    still be picked -- the fix must not reject the floor itself."""
    bindir = tmp_path / "bin"
    _stub(bindir, "python3", "Python 3.9.0")

    result = _source(_isolated_env(tmp_path, str(bindir)))

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "PYTHON=python3" in result.stdout, result.stdout


def test_a_below_floor_interpreter_is_rejected_in_favor_of_a_versioned_fallback(tmp_path):
    """The reporter's exact shape: a `python3` below the floor is on PATH,
    but a versioned interpreter that meets the floor is ALSO on PATH (as
    `python3.11`, say) -- the fix must skip the broken one and fall
    through to the versioned candidate instead of accepting the first
    thing that merely runs."""
    bindir = tmp_path / "bin"
    _stub(bindir, "python3", "Python 3.8.10")
    _stub(bindir, "python3.11", "Python 3.11.4")

    env = _isolated_env(tmp_path, str(bindir), base_path=_real_path_without_other_pythons())
    result = _source(env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "PYTHON=python3.11" in result.stdout, (
        "a below-floor python3 was accepted instead of falling through to "
        f"the floor-meeting versioned candidate:\n{result.stdout}"
    )


def test_every_candidate_below_floor_is_fatal_and_distinct_from_not_found(tmp_path):
    """When every interpreter on PATH is below the floor, this must FATAL
    with a message distinct from "no working Python found" (#650) -- an
    interpreter IS there and DOES run, it is simply unsupported, and the
    generic message would send a reporter to reinstall Python when one
    is already installed and working."""
    bindir = tmp_path / "bin"
    _stub(bindir, "python3", "Python 3.8.10")

    env = _isolated_env(tmp_path, str(bindir), base_path=_real_path_without_other_pythons())
    result = _source(env)

    assert result.returncode != 0, "a floor-violating-only PATH must still be fatal"
    err = result.stderr
    assert "below the floor this plugin supports" in err, err
    assert "FATAL: No working Python found" not in err, (
        "the below-floor case must not reuse the generic 'not found' "
        f"message -- a working-but-unsupported interpreter is a different "
        f"fact:\n{err}"
    )
    assert "3.8.10" in err, err


def test_a_versioned_fallback_candidate_is_actually_runnable(tmp_path):
    """The reporter's exact shape, carried one step further: picking
    `python3.11` as the fallback is useless if `_remember_run_python`
    cannot dispatch to it. `_remember_run_python` is a literal if/elif
    ladder (the plugin directory's scanner refuses a variable-driven
    dispatch), and each NEW versioned candidate needs its own arm there
    too -- a candidate accepted by `_remember_python` but missing from
    `_remember_run_python` would FATAL on every real use after being
    "successfully" detected."""
    bindir = tmp_path / "bin"
    _stub(bindir, "python3", "Python 3.8.10")
    _stub(bindir, "python3.11", "Python 3.11.4")
    env = _isolated_env(tmp_path, str(bindir), base_path=_real_path_without_other_pythons())

    script = (
        f'source "{DETECT.as_posix()}" >/dev/null 2>&1; '
        '_remember_run_python -V'
    )
    result = subprocess.run(
        [BASH, "-c", script], env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "unrecognized PYTHON value" not in result.stderr, (
        "python3.11 was accepted as PYTHON but _remember_run_python has no "
        f"arm for it:\n{result.stderr}"
    )
    assert "3.11.4" in result.stdout, (result.stdout, result.stderr)


def test_a_stale_pre_1003_cache_is_not_trusted(tmp_path):
    """A cache written by a version of this file from before #1003 has no
    PYFLOOR line at all. Trusting it anyway would let a stale, floor-
    violating PYTHON value cached before this fix outlive the fix for as
    long as PATH does not change -- reopening the exact silent-for-months
    failure #1003 reports. The loader must distrust it and re-probe."""
    bindir = tmp_path / "bin"
    _stub(bindir, "python3", "Python 3.12.0")
    env = _isolated_env(tmp_path, str(bindir))
    cache_tmpdir = Path(env["TMPDIR"])
    cache_file = cache_tmpdir / "remember-detect-tools-cache"
    # The pre-#1003 shape: CACHE_PATH/PYTHON/JQ only, no PYFLOOR. CACHE_PATH
    # is written as the EXACT $PATH this env will carry at load time, so a
    # PATH mismatch cannot be what distrusts it -- PYFLOOR's absence must be
    # the only reason this gets refused and re-probed.
    cache_file.write_text(
        f"CACHE_PATH={env['PATH']}\nPYTHON=python3\nJQ=jq\n", encoding="utf-8"
    )

    result = _source(env)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "PYTHON=python3" in result.stdout
    # Confirm it was actually RE-PROBED (not merely that the stale value
    # happened to still be correct): the loader must have refused the
    # legacy-shaped file and republished it with a PYFLOOR line.
    refreshed = cache_file.read_text(encoding="utf-8")
    assert "PYFLOOR=" in refreshed, (
        "the stale pre-#1003 cache was never refreshed -- the loader "
        f"accepted it as-is instead of distrusting and re-probing:\n{refreshed}"
    )
