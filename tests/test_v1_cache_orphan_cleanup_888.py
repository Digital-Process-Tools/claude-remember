"""
#888: #864 bumped the config-flatten cache's on-disk name to
`remember-config-cache-v2-<key>` (so an old-format cache is never opened),
but nothing ever removed the file an older build wrote under the OLD name
(`remember-config-cache-<key>`). An install upgrading across #864 kept that
orphan in `$TMPDIR` forever, and README's own disclosure (#857) still named
the pre-#864 filename -- `tests/test_readme_discloses_854.py` could not
catch the drift because its check is a substring match on the shared
`remember-config-cache-` prefix, which both names satisfy.

This file pins the other half of the fix: `_remember_cfg_flatten_cache_publish`
(scripts/log.sh) now best-effort removes a leftover v1-named orphan for the
SAME REMEMBER_DIR every time it writes the v2 cache.

Positive control: `test_publish_leaves_unrelated_files_alone` pins that this
cleanup targets the one specific pre-#864 path for the current REMEMBER_DIR,
not a loose sweep of $TMPDIR -- without it, a cleanup that deleted
everything in sight would also make the main test below pass for the wrong
reason.
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
    # log.sh's own sourcing requires PROJECT_DIR (lib-memory-dir.sh); same
    # convention tests/test_config_flatten_cache_eval_removed_864.py uses.
    return subprocess.run(
        [BASH, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "PROJECT_DIR": str(project_dir)},
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (sys_tmp, remember_dir, remember_config) for a standard-merge
    publish: REMEMBER_CONFIG's basename must start with `remember-config-`
    for _remember_cfg_flatten_cache_is_standard_merge to let publish() run
    at all (same convention tests/test_config_flatten_cache_eval_removed_864.py
    uses)."""
    sys_tmp = tmp_path / "systmp"
    sys_tmp.mkdir()
    remember_dir = tmp_path / "project" / ".remember"
    remember_dir.mkdir(parents=True)
    remember_config = sys_tmp / "remember-config-XXXXXX"
    remember_config.write_text("", encoding="utf-8")
    return sys_tmp, remember_dir, remember_config


def test_publish_removes_pre_864_orphan(tmp_path):
    sys_tmp, remember_dir, remember_config = _setup(tmp_path)
    script = f"""
set -eu
TMPDIR="{sys_tmp.as_posix()}"
export TMPDIR
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
export REMEMBER_DIR="{remember_dir.as_posix()}"
export REMEMBER_CONFIG="{remember_config.as_posix()}"
export REMEMBER_CONFIG_CACHE=1
_v1=$(_remember_cfg_flatten_cache_path_v1)
printf 'orphan-from-before-864\n' > "$_v1"
_remember_cfg_flatten_cache_publish $'name\tvalue'
if [ -f "$_v1" ]; then echo V1_STATE=EXISTS; else echo V1_STATE=GONE; fi
"""
    result = _run(script, tmp_path)
    assert result.returncode == 0, f"harness itself failed: {result.stderr!r}"
    assert "V1_STATE=GONE" in result.stdout, (
        f"publish() did not remove the pre-#864 orphan file: {result.stdout!r} / "
        f"{result.stderr!r}"
    )


def test_publish_leaves_unrelated_files_alone(tmp_path):
    """Positive control for the test above: the cleanup must be scoped to
    the one v1 path for THIS REMEMBER_DIR, not a sweep of $TMPDIR. Without
    this, a bug that deleted every file in $TMPDIR would also satisfy
    test_publish_removes_pre_864_orphan above."""
    sys_tmp, remember_dir, remember_config = _setup(tmp_path)
    unrelated = sys_tmp / "remember-env-deadbeef"
    unrelated.write_text("unrelated", encoding="utf-8")
    script = f"""
set -eu
TMPDIR="{sys_tmp.as_posix()}"
export TMPDIR
source "{LOG_SH.as_posix()}" >/dev/null 2>&1
export REMEMBER_DIR="{remember_dir.as_posix()}"
export REMEMBER_CONFIG="{remember_config.as_posix()}"
export REMEMBER_CONFIG_CACHE=1
_remember_cfg_flatten_cache_publish $'name\tvalue'
"""
    result = _run(script, tmp_path)
    assert result.returncode == 0, f"harness itself failed: {result.stderr!r}"
    assert unrelated.exists(), (
        "publish()'s v1-orphan cleanup removed an unrelated file in $TMPDIR "
        "that never matched the v1 cache path -- the cleanup is too broad"
    )


def test_v1_path_differs_from_v2_only_by_the_v2_segment(tmp_path):
    """Sanity check on the two path functions themselves: v1 and v2 must
    name two different files for the same REMEMBER_DIR, and the only
    difference must be the `-v2-` segment -- otherwise the cleanup above
    could silently target (and delete) the live v2 cache instead of the
    orphan."""
    remember_dir = tmp_path / "project" / ".remember"
    remember_dir.mkdir(parents=True)
    script = f"""
set -eu
TMPDIR="{tmp_path.as_posix()}"
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
    assert v1 != v2, "v1 and v2 cache paths must not collide"
    assert v2 == v1.replace(
        "remember-config-cache-", "remember-config-cache-v2-", 1
    ), f"v1/v2 paths differ by more than the -v2- segment: v1={v1!r} v2={v2!r}"
