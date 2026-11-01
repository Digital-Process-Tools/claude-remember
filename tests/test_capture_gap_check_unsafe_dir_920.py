"""#920 scheduler review round 2, item 1: capture-gap-check.sh's own fast
path (inherited PROJECT_DIR/PIPELINE_DIR/REMEMBER_DIR, #1069/#1079) never
re-sources lib-memory-dir.sh, so it can only see REMEMBER_DIR_UNSAFE if the
PARENT process (session-start-hook.sh, in production) exported it before
forking this script as a child -- a plain (non-exported) assignment is never
inherited across that boundary. Reproduced directly, same shape as the
scheduler's own repro command:

    REMEMBER_RECOVERY_ENABLED=1 REMEMBER_DIR=relative/store \
        bash scripts/capture-gap-check.sh <id>

created ``relative/store/tmp/{session-slug,case-divergence,roster-epoch}``
under cwd before this fix, because REMEMBER_DIR_UNSAFE was a plain
assignment in lib-memory-dir.sh and this script never read it as 1. A
directly preset ``REMEMBER_DIR`` env var does not survive sourcing
lib-memory-dir.sh (it always recomputes REMEMBER_DIR from PROJECT_DIR plus
the bundled/user config's ``data_dir``, #297), so this test drives the real
producer through the same config-based route
``test_remember_dir_guard_920.py::TestBootstrapDirsIsGuarded`` already uses,
with an absolute-but-newline-bearing ``data_dir`` standing in for an unsafe
value -- the newline is what makes it unsafe (#695), not relativeness.

TDD shape: must-fire (an unsafe value reaches this script only via an
EXPORTED parent variable, the real production shape, and creates nothing
under cwd) paired with a positive control (an ordinary safe value still
creates its tree), per this repo's own CLAUDE.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests._bash_runner import resolve_bash

CAPTURE_GAP_CHECK = REPO_ROOT / "scripts" / "capture-gap-check.sh"
LIB_MEMORY_DIR = REPO_ROOT / "scripts" / "lib-memory-dir.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash found -- these tests drive the real hook scripts",
)


def _run(tmp_path: Path, *, data_dir) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """Set PROJECT_DIR/PIPELINE_DIR/HOME and (optionally) a config.json's
    data_dir, source lib-memory-dir.sh in a PARENT shell -- the real
    mechanism that resolves REMEMBER_DIR and exports REMEMBER_DIR_UNSAFE --
    then exec capture-gap-check.sh as a CHILD of that shell: the same fork
    boundary session-start-hook.sh crosses when it backgrounds (or
    inline-execs) this script."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    if data_dir is not None:
        (pipeline / "config.json").write_text(json.dumps({"data_dir": data_dir}), encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    cwd_marker = tmp_path / "cwd"
    cwd_marker.mkdir(parents=True, exist_ok=True)
    script = f"""
    set +e
    export HOME={str(home)!r}
    export PROJECT_DIR={str(project)!r}
    export PIPELINE_DIR={str(pipeline)!r}
    export REMEMBER_RECOVERY_ENABLED=1
    source {str(LIB_MEMORY_DIR)!r} 2>/dev/null
    bash {str(CAPTURE_GAP_CHECK)!r} test-session-id
    echo "REMEMBER_DIR=$REMEMBER_DIR"
    echo "REMEMBER_DIR_UNSAFE=${{REMEMBER_DIR_UNSAFE:-}}"
    """
    result = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60, check=False, cwd=str(cwd_marker),
    )
    return result, project, cwd_marker


UNSAFE_DATA_DIR = "/tmp/fake-cgc-920\n23:59:00 evil"


def test_must_fire_unsafe_dir_inherited_via_export_creates_nothing_under_cwd(tmp_path):
    # NOT asserted against cwd alone, deliberately: an absolute-but-newline-
    # bearing data_dir resolves to an absolute REMEMBER_DIR wherever the
    # newline happens to put it -- NOT under cwd -- so "nothing leaked under
    # cwd" alone passed VACUOUSLY even with all three gates reverted (checked
    # directly). The real assertion is that the mkdir never ran at all.
    unsafe_dir = Path(UNSAFE_DATA_DIR)
    assert not unsafe_dir.exists(), f"pre-existing from a previous run: {unsafe_dir!r}"
    try:
        result, _project, cwd_marker = _run(tmp_path, data_dir=UNSAFE_DATA_DIR)
        assert "REMEMBER_DIR_UNSAFE=1" in result.stdout, (result.stdout, result.stderr)
        leaked = [p for p in cwd_marker.iterdir()]
        assert not leaked, (
            "capture-gap-check.sh created a directory under cwd from an unsafe "
            f"REMEMBER_DIR inherited via export: {leaked}\n{result.stdout}\n{result.stderr}"
        )
        assert not unsafe_dir.exists(), (
            f"capture-gap-check.sh created the unsafe REMEMBER_DIR itself at {unsafe_dir!r}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    finally:
        if unsafe_dir.exists():
            import shutil
            shutil.rmtree(unsafe_dir, ignore_errors=True)


def test_must_not_fire_safe_dir_still_creates_its_tree(tmp_path):
    """Positive control: an ordinary, safe (legacy-layout) REMEMBER_DIR must
    still get its tmp/ tree -- a version of the fix that gated
    unconditionally (never writing anything, safe value or not) would pass
    the test above for the wrong reason."""
    result, project, _cwd_marker = _run(tmp_path, data_dir=None)
    assert "REMEMBER_DIR_UNSAFE=" in result.stdout and "REMEMBER_DIR_UNSAFE=1" not in result.stdout, (
        result.stdout, result.stderr,
    )
    assert (project / ".remember" / "tmp").is_dir(), (result.stdout, result.stderr)


def test_must_fire_flag_is_exported_not_just_assigned():
    """Structural pin: a plain assignment is invisible to a forked child;
    only `export REMEMBER_DIR_UNSAFE` on its own line makes the #1069 fast
    path above able to see it at all."""
    text = LIB_MEMORY_DIR.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    assert "export REMEMBER_DIR_UNSAFE" in lines, (
        "REMEMBER_DIR_UNSAFE is set in lib-memory-dir.sh but never exported -- "
        "a forked child (capture-gap-check.sh's own fast path) cannot inherit it"
    )


def test_must_fire_every_mkdir_site_is_gated():
    """Structural pin, same shape as #902's and #920's own: every `mkdir -p`
    site this file has must be preceded, within a few lines, by a check of
    REMEMBER_DIR_UNSAFE -- not merely have it exported somewhere unrelated."""
    lines = CAPTURE_GAP_CHECK.read_text(encoding="utf-8").splitlines()
    mkdir_idxs = [
        i for i, line in enumerate(lines)
        if line.strip().startswith('[ -d "$_dir" ] || mkdir -p')
    ]
    assert len(mkdir_idxs) == 3, mkdir_idxs
    for idx in mkdir_idxs:
        preceding = "\n".join(lines[max(0, idx - 6):idx + 1])
        assert "REMEMBER_DIR_UNSAFE" in preceding, (
            f"capture-gap-check.sh:{idx + 1}'s mkdir is not gated on REMEMBER_DIR_UNSAFE\n" + preceding
        )
