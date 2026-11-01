"""#920 scheduler review round 2, item 2: session-start-hook.sh:457's own
`tmp/session-started.d` mkdir (added by #1084/#1086, after the #920 gating
pass this issue's own PR made at 806/1200/1630) was never covered by it.

This runs the REAL session-start-hook.sh end to end (REMEMBER_DEFER=0,
synchronous, same env convention as
tests/test_capture_gap_check_env_cache_1079.py's own `_warm_the_cache` --
CLAUDE_PLUGIN_ROOT pointed at the real repo root, not a synthetic copy: an
earlier version of this test built its own fake plugin_root and every
assertion passed VACUOUSLY, because resolve-paths.sh's own plugin-root
validation (`.claude-plugin/plugin.json` must exist) rejected it before the
hook ever reached line 457 at all), rather than only bootstrap-dirs.sh in
isolation the way
tests/test_remember_dir_guard_920.py::TestBootstrapDirsIsGuarded does -- a
full run exercises every mkdir site session-start-hook.sh's own process
reaches, not just the one bootstrap-dirs.sh owns.

The unsafe `data_dir` is set through HOME's own user-global config
(highest-priority source lib-memory-dir.sh reads, #260) rather than the
repo's bundled pipeline/config.json, which this test must not touch.

NOT asserted against cwd alone, deliberately: an absolute-but-newline-
bearing `data_dir` (#695's own unsafe shape) resolves to an absolute
REMEMBER_DIR wherever the newline happens to put it (here, a literal
``/tmp/fake<NEWLINE>23:59:00 evil`` directory on disk) -- NOT under cwd, so
"nothing leaked under cwd" alone would have passed vacuously even with the
#920 gate fully reverted (checked directly: reverting the gate and
re-running left cwd empty but created that literal directory on disk). The
real assertion is that the mkdir never ran at all, which is: that directory
must not exist on disk afterward.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests._bash_runner import resolve_bash

SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

BASH = resolve_bash()
pytestmark = pytest.mark.skipif(
    BASH is None, reason="no usable bash found -- this test drives the real hook script",
)

SESSION_ID = "f0920000-0000-4000-8000-000000000920"
UNSAFE_DATA_DIR = "/tmp/fake\n23:59:00 evil"


def _run(tmp_path: Path, *, data_dir) -> tuple[subprocess.CompletedProcess, Path, Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    cwd_marker = tmp_path / "cwd"
    cwd_marker.mkdir(parents=True, exist_ok=True)
    systmp = tmp_path / "systmp"
    systmp.mkdir(parents=True, exist_ok=True)

    if data_dir is not None:
        user_remember = home / ".remember"
        user_remember.mkdir(parents=True, exist_ok=True)
        cfg = user_remember / "config.json"
        cfg.write_text(json.dumps({"data_dir": data_dir}), encoding="utf-8")
        past = time.time() - 120
        os.utime(cfg, (past, past))

    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "HOME": str(home),
        "TMPDIR": str(systmp),
        "REMEMBER_DEFER": "0",
    }
    payload = json.dumps({
        "session_id": SESSION_ID,
        "transcript_path": "/does/not/matter/0920.jsonl",
        "hook_event_name": "SessionStart",
        "cwd": str(project),
        "source": "startup",
    })
    result = subprocess.run(
        [BASH, SESSION_START.as_posix()], env=env, input=payload,
        capture_output=True, text=True, timeout=60, check=False, cwd=str(cwd_marker),
    )
    return result, project, cwd_marker


def test_must_fire_unsafe_dir_full_session_start_run_creates_nothing_at_the_unsafe_path(tmp_path):
    unsafe_dir = Path(UNSAFE_DATA_DIR)
    assert not unsafe_dir.exists(), f"pre-existing from a previous run -- clean before trusting this test: {unsafe_dir!r}"
    try:
        result, _project, cwd_marker = _run(tmp_path, data_dir=UNSAFE_DATA_DIR)
        assert result.returncode == 0, f"exited {result.returncode}: {result.stderr[-2000:]}"
        leaked_cwd = [p for p in cwd_marker.iterdir()]
        assert not leaked_cwd, (
            f"a full SessionStart run created a directory under cwd: {leaked_cwd}\n{result.stdout}\n{result.stderr}"
        )
        assert not unsafe_dir.exists(), (
            "a full SessionStart run created the unsafe REMEMBER_DIR itself "
            f"(session-start-hook.sh:457's tmp/session-started.d mkdir) at {unsafe_dir!r}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    finally:
        if unsafe_dir.exists():
            import shutil
            shutil.rmtree(unsafe_dir, ignore_errors=True)


def test_must_not_fire_safe_dir_full_run_still_creates_session_started_marker(tmp_path):
    """Positive control: an ordinary, safe (legacy-layout) run must still
    write the session-started.d marker this fix gates -- a version of the
    fix that gated unconditionally would pass the test above for the wrong
    reason."""
    result, project, _cwd_marker = _run(tmp_path, data_dir=None)
    assert result.returncode == 0, f"exited {result.returncode}: {result.stderr[-2000:]}"
    marker = project / ".remember" / "tmp" / "session-started.d" / SESSION_ID
    assert marker.is_file(), (result.stdout, result.stderr)
