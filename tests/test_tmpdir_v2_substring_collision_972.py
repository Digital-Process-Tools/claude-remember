"""#972: scripts/log.sh's v1 (pre-#864) cache-path derivation used a bare
`${var/-v2-/-}` substring substitution over the FULL path, not anchored to
the filename's own `-v2-` segment. `${TMPDIR:-/tmp}` is part of that full
path, so a TMPDIR that itself happens to contain the literal substring
`-v2-` (e.g. `/mnt/build-v2-staging/tmp`) made the substitution fire against
TMPDIR's own text instead of the filename segment it was meant to strip --
producing a "v1" path that names neither the real orphan nor anything else
useful.

Positive control: `test_v1_path_still_correct_with_unrelated_tmpdir` pins
the same assertions against a TMPDIR that does NOT contain `-v2-` -- without
it, a fix that broke the basename derivation generally (not just the
TMPDIR-collision case) could still pass the collision test below for the
wrong reason if it happened to produce an unrelated-but-matching path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_SH = REPO_ROOT / "scripts" / "log.sh"

sys.path.insert(0, str(REPO_ROOT))

BASH = shutil.which("bash") or ""
pytestmark = pytest.mark.skipif(not BASH, reason="bash not on PATH")


def _run(script: str, project_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "PROJECT_DIR": str(project_dir)},
    )


def _v1_v2_paths(tmp_path: Path, sys_tmp: Path) -> tuple[str, str]:
    remember_dir = tmp_path / "project" / ".remember"
    remember_dir.mkdir(parents=True)
    script = f"""
set -eu
TMPDIR="{sys_tmp.as_posix()}"
export TMPDIR
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
export REMEMBER_DIR="{remember_dir.as_posix()}"
echo "V1=$(_remember_cfg_flatten_cache_path_v1)"
echo "V2=$(_remember_cfg_flatten_cache_path)"
"""
    result = _run(script, tmp_path)
    assert result.returncode == 0, f"harness itself failed: {result.stderr!r}"
    v1 = v2 = None
    for line in result.stdout.splitlines():
        if line.startswith("V1="):
            v1 = line[len("V1="):]
        elif line.startswith("V2="):
            v2 = line[len("V2="):]
    assert v1 and v2, f"could not read both paths back: {result.stdout!r}"
    return v1, v2


def _assert_v1_anchored_to_basename(v1: str, v2: str) -> None:
    v2_dir, v2_base = v2.rsplit("/", 1)
    v1_dir, v1_base = v1.rsplit("/", 1)
    assert v1_dir == v2_dir, (
        "v1 path's directory must be byte-identical to v2's -- the "
        f"substitution must never touch TMPDIR's own text: "
        f"v1_dir={v1_dir!r} v2_dir={v2_dir!r}"
    )
    assert v1_base == v2_base.replace("-v2-", "-", 1), (
        "v1 basename must differ from v2's basename only by the -v2- "
        f"segment: v1_base={v1_base!r} v2_base={v2_base!r}"
    )


def test_v1_path_not_confused_by_v2_substring_in_tmpdir(tmp_path):
    """The actual bug: TMPDIR itself contains the literal substring
    `-v2-` before the filename's own -v2- segment is ever reached."""
    sys_tmp = tmp_path / "build-v2-staging"
    sys_tmp.mkdir(parents=True)
    v1, v2 = _v1_v2_paths(tmp_path, sys_tmp)
    _assert_v1_anchored_to_basename(v1, v2)


def test_v1_path_still_correct_with_unrelated_tmpdir(tmp_path):
    """Positive control: same assertions, ordinary TMPDIR with no -v2-
    substring anywhere in it."""
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir(parents=True)
    v1, v2 = _v1_v2_paths(tmp_path, sys_tmp)
    _assert_v1_anchored_to_basename(v1, v2)
