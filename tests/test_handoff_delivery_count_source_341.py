"""The handoff delivery counter must count sessions, not `SessionStart` fires (#341).

`SessionStart` fires on all five `source` values, including `compact` (every
auto-compaction) and `clear`. Before this fix the "already delivered N times"
counter at `scripts/session-start-hook.sh:~985` incremented on any fire whose
handoff fingerprint matched the previous record — with no read of `source` at
all. A handoff delivered exactly once, in a session that later auto-compacts
four times, read as "already delivered 5 times": a claim about how many
*sessions* had seen the note, made from a count of how many times the hook
merely *fired*.

#206 already settled the general rule for the capture-alive store: `SessionStart`
firing does not mean a new session id exists. This is that rule applied to the
delivery counter.

The fix: `compact` is excluded from the increment. `clear`, `fork`, `resume`,
an absent `source` and an unrecognised value are NOT excluded — each of those
already means something happened that plausibly warrants treating the fire as
a fresh look at the note (a resumed session, a cleared context, an unknown
future payload shape defaulting to today's behaviour, #339's own safe
direction). Only `compact` is provably not a new session.

Two properties, in the same fixture so a broken harness cannot pass by doing
nothing to either:
  - `compact` refires must NOT inflate the count (the bug)
  - `startup` refires must STILL inflate the count (the positive control —
    without it, a harness that fires the hook but reads nothing back, or a fix
    that stops incrementing under every source, would pass the first
    assertion for the wrong reason)
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

SESSION = "eeeeeeee-0000-4000-8000-000000000341"


def _store(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects" / _slug(str(project))).mkdir(parents=True)
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


def test_compact_refires_do_not_inflate_the_delivery_count(tmp_path):
    home, project, remember = _store(tmp_path)
    (remember / "remember.md").write_text("Next: land the parser fix.\n", encoding="utf-8")

    first = _fire(home, project, remember, "startup")
    assert "already delivered" not in first.lower(), (
        f"first delivery must read as fresh.\noutput: {first[:800]}"
    )

    # Four auto-compactions of the same, still-unread handoff — the exact
    # scenario #341 names: "a note delivered once ... already delivered 5
    # times inside one session."
    last = ""
    for _ in range(4):
        last = _fire(home, project, remember, "compact")

    assert "land the parser fix" in last, "content dropped across compact refires"
    assert "already delivered 5 times" not in last.lower(), (
        "compact refires inflated the delivery count exactly the way #341 "
        f"describes.\noutput: {last[:800]}"
    )
    assert "already delivered 1 times" in last.lower(), (
        "a compact refire is not a new session and must not move the count "
        f"off its first-delivery value.\noutput: {last[:800]}"
    )


def test_new_sessions_still_inflate_the_delivery_count(tmp_path):
    """Positive control: the counter must still count genuinely fresh fires.
    Without this, a fix that stops incrementing under every source (not just
    `compact`) would pass the test above for the wrong reason."""
    home, project, remember = _store(tmp_path)
    (remember / "remember.md").write_text("Next: land the parser fix.\n", encoding="utf-8")

    _fire(home, project, remember, "startup")
    second = _fire(home, project, remember, "startup")
    third = _fire(home, project, remember, "startup")

    assert "already delivered 2 times" in second.lower(), second[:800]
    assert "already delivered 3 times" in third.lower(), third[:800]


def test_unrecognised_source_still_inflates_the_delivery_count(tmp_path):
    """The safe direction from #339 carries over here: an absent or unknown
    `source` must not silently join the compact exclusion — that would widen
    the fix into "never count a repeat delivery" for any payload shape this
    heuristic fails to recognise."""
    home, project, remember = _store(tmp_path)
    (remember / "remember.md").write_text("Next: land the parser fix.\n", encoding="utf-8")

    _fire(home, project, remember, None)
    second = _fire(home, project, remember, "some-future-source")

    assert "already delivered 2 times" in second.lower(), second[:800]
