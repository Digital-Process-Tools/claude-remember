"""#920 scheduler review round 2, item 4: doctor.sh's own REMEMBER_DIR line
printed "OK" unconditionally, even when lib-memory-dir.sh's own
REMEMBER_DIR_UNSAFE=1 meant every save site had already refused to touch it
-- the only trace of that refusal was a FATAL line on stderr, which Claude
Code hides for an exit-0 hook and which user-prompt-hook.sh/
capture-gap-check.sh both discard outright. This runs the real doctor.sh
(both the human report and --json) against an absolute-but-newline-bearing
data_dir (#695's own unsafe shape), the same technique
tests/test_session_start_unsafe_dir_full_run_920.py uses for
session-start-hook.sh.

TDD shape: must-fire (both surfaces name the refusal) paired with a
positive control (an ordinary, safe project still reports OK/false), per
this repo's own CLAUDE.md.
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

from pipeline.slug import session_dir_slug as _slug

DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)


def _run(tmp_path: Path, *, data_dir, json_mode: bool) -> subprocess.CompletedProcess:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True, exist_ok=True)

    if data_dir is not None:
        user_remember = home / ".remember"
        user_remember.mkdir(parents=True, exist_ok=True)
        cfg = user_remember / "config.json"
        cfg.write_text(json.dumps({"data_dir": data_dir}), encoding="utf-8")
        past = time.time() - 120
        os.utime(cfg, (past, past))

    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    }
    args = ["bash", str(DOCTOR)]
    if json_mode:
        args.append("--json")
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=180, check=False)


def test_must_fire_unsafe_dir_human_report_says_fail(tmp_path):
    result = _run(tmp_path, data_dir="/tmp/fake\n23:59:00 evil", json_mode=False)
    assert "FAIL REMEMBER_DIR" in result.stdout, (result.stdout, result.stderr)
    assert "refused as unsafe" in result.stdout, (result.stdout, result.stderr)


def test_must_not_fire_safe_dir_human_report_says_ok(tmp_path):
    """Positive control: an ordinary, safe (legacy-layout) project must still
    report OK -- a version of the fix that printed FAIL unconditionally
    would pass the test above for the wrong reason."""
    result = _run(tmp_path, data_dir=None, json_mode=False)
    assert "OK   REMEMBER_DIR" in result.stdout, (result.stdout, result.stderr)
    assert "FAIL REMEMBER_DIR" not in result.stdout, (result.stdout, result.stderr)


def test_must_fire_unsafe_dir_json_reports_true(tmp_path):
    result = _run(tmp_path, data_dir="/tmp/fake\n23:59:00 evil", json_mode=True)
    payload = json.loads(result.stdout)
    assert payload["remember_dir_unsafe"] is True, payload


def test_must_not_fire_safe_dir_json_reports_false(tmp_path):
    """Positive control: an ordinary, safe project's --json output must
    report false, not merely omit the key."""
    result = _run(tmp_path, data_dir=None, json_mode=True)
    payload = json.loads(result.stdout)
    assert payload["remember_dir_unsafe"] is False, payload
