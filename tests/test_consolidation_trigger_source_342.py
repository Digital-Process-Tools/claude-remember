"""The consolidation trigger must not re-spawn on every compaction (#342).

`SessionStart` fires on `startup`, `resume`, `clear`, `compact` and `fork`.
Before this fix, whenever past-day staging files existed, the trigger at
`scripts/session-start-hook.sh:~1178` spawned `run-consolidation.sh` on
EVERY one of those fires — including `compact`, which happens mid-session and
changes nothing about whether yesterday's staging files need consolidating.
The #339 report measured `compact` firing 82 times across 50 sessions,
against `startup`'s 180: the same redundant spawn, repeated on a multiplier
nobody had counted.

The fix excludes only `compact` from the trigger, the same boundary #341 drew
for the delivery counter: `compact` is provably not a new entry point into
the session, while `startup`/`resume`/`clear`/`fork`/absent/unrecognised all
plausibly warrant a fresh look and are left triggering exactly as before —
narrowing further would mean yesterday's staging files could wait
indefinitely for a session that never does a bare `startup` again.

Two properties in the same fixture:
  - `compact`, with staging files pending, must NOT spawn consolidation
  - `startup`, with the same staging files pending, must STILL spawn it
    (positive control — without this, a harness that never detects a spawn
    at all would pass the first assertion for the wrong reason)

The spawn is detected the same way the trigger announces itself: the
`=== MEMORY CONSOLIDATION ===` header this hook itself prints before
launching `run-consolidation.sh` in the background. Nothing here waits on or
inspects the background process — #342 is filed as "runs more often than it
needs to", not as a claim about what happens inside that script, which
belongs to a different lane.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics — not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START = REPO_ROOT / "scripts" / "session-start-hook.sh"

sys.path.insert(0, str(REPO_ROOT))
from pipeline.slug import session_dir_slug as _slug

SESSION = "ffffffff-0000-4000-8000-000000000342"
MARKER = "=== MEMORY CONSOLIDATION ==="


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
    # A stale (not-today) staging file is what the trigger looks for.
    (remember / "today-2020-01-01.md").write_text("STALE-STAGING-342\n", encoding="utf-8")
    return home, project, remember


def _env(home, project, remember):
    return {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }


def _payload(source=None):
    p = {
        "session_id": SESSION,
        "transcript_path": "/does/not/matter/" + SESSION + ".jsonl",
        "hook_event_name": "SessionStart",
        "cwd": "/does/not/matter",
    }
    if source is not None:
        p["source"] = source
    return json.dumps(p)


def _fire(home, project, remember, source):
    result = subprocess.run(
        ["bash", str(SESSION_START)],
        env=_env(home, project, remember),
        input=_payload(source),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_compact_with_pending_staging_does_not_trigger_consolidation(tmp_path):
    home, project, remember = _store(tmp_path)
    out = _fire(home, project, remember, "compact")
    assert MARKER not in out, (
        "compact re-spawned consolidation for staging that has not changed.\n"
        "output: " + out[:800]
    )


def test_startup_with_pending_staging_still_triggers_consolidation(tmp_path):
    """Positive control: a genuinely fresh entry point must still trigger it,
    so this is the same fixture proving the gate is source-specific and not a
    change that silently stopped the trigger from ever firing."""
    home, project, remember = _store(tmp_path)
    out = _fire(home, project, remember, "startup")
    assert MARKER in out, "startup failed to trigger consolidation.\noutput: " + out[:800]


def test_no_pending_staging_never_triggers_regardless_of_source(tmp_path):
    """Characterization: the gate is source AND staging-count, not source
    alone — a session with nothing to compress must stay silent either way."""
    home, project, remember = _store(tmp_path)
    (remember / "today-2020-01-01.md").unlink()
    out = _fire(home, project, remember, "startup")
    assert MARKER not in out, out[:800]
