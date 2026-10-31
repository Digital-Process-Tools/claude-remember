"""#1046: several terminals in the same project directory, each with its own
pinned REMEMBER_DIR, share one SESSIONS_DIR. Recovery's own call site --
`save-session.sh "$PREV_ID" --force`, in scripts/capture-gap-check.sh's
deferred phase -- used to force-save whichever transcript
`_find_previous_transcript` picked, with no regard for which store wrote it.
Since that function only ever looks at mtimes, a terminal pinned to store `b`
starting up right after store `a` was active would pick `a`'s live session
and force-save ITS summary into `b`'s own now.md / daily file.

A per-store roster already existed for a different purpose:
post-tool-hook.sh writes `$REMEMBER_DIR/tmp/capture-alive.d/<session id>` for
every session that ran a tool under that store, and the #200 capture-gap
check already reads it (via capture_was_seen()) to judge whether a session
was ever captured at all.

Fixed at the recovery call site, not inside `_find_previous_transcript`
itself: that function's result (PREV_ID/PREV_JSONL) is shared with the #200
capture-gap check below it, whose entire purpose is to warn about a session
that ran tools but was never captured in THIS store -- precisely the "no
marker present" signal recovery's own new guard uses. Gating resolution
itself would make a genuine capture gap indistinguishable from a foreign
store's session and silence that warning for it (confirmed empirically
against this repo's existing suite: gating resolution broke
tests/test_capture_gap_notice.py's and test_session_start_prev_session_270
.py's own capture-gap pins). Gating only the WRITE -- whether recovery
actually force-saves PREV_ID -- leaves PREV_ID resolution, and the #200
check's own three-source capture_was_seen(), untouched.

These tests exercise the real session-start-hook.sh end to end (not an
extracted function, unlike test_previous_transcript_no_sort_691.py's style)
because the guard under test sits inline in scripts/capture-gap-check.sh's
recovery block, between two other preconditions ([-f LAST_SAVE_FILE],
PREV_WAS_SAVED) that are easiest to drive through the real hook rather than
re-implemented in a sandbox. Reuses this repo's own fixture conventions from
test_session_start_prev_session_270.py (_vouch, _startup_project,
_plugin_root_with_stub_save) via cross-import, the same pattern
test_git_reconcile_mapfile_floor_952.py uses for test_git_reconcile_903.py."""

from __future__ import annotations

import json
import sys

import pytest

from .subprocess_helpers import subprocess_failure_detail
from .test_session_start_prev_session_270 import (
    CURRENT,
    PREV,
    _await_record,
    _env,
    _payload,
    _plugin_root_with_stub_save,
    _run,
    _startup_project,
    _vouch,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)


def test_recovery_declines_a_candidate_with_no_roster_marker_in_this_store(tmp_path):
    """Must-fire case, the issue's own repro shape: two stores share one
    project SESSIONS_DIR. PREV is the newest OTHER transcript
    `_find_previous_transcript` would pick, but it has no marker under THIS
    store's capture-alive.d (it may belong to a different store's live
    session). Recovery must decline to force-save it."""
    home, project, remember, _ = _startup_project(tmp_path)
    assert not (remember / "tmp" / "capture-alive.d" / PREV).exists(), (
        "precondition: no roster marker for PREV under this store"
    )
    (remember / "tmp" / "last-save.json").write_text(json.dumps({"sessions": {}}))
    record = tmp_path / "save-argv.txt"
    root = _plugin_root_with_stub_save(tmp_path, record)

    result = _run(
        _env(home, project, remember, plugin_root=root),
        _payload(CURRENT),
        script=root / "scripts" / "session-start-hook.sh",
    )
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    # Recovery forks (& disown) -- an immediate check here would pass
    # trivially whether or not the fix is present, simply because the fork
    # has not finished yet. _await_record's own full default timeout is the
    # same wait the positive control below uses to confirm a rescue DID
    # happen; used here, it is the only way an absence is a verdict rather
    # than a race won by checking too soon (CLAUDE.md: a negative assertion
    # needs a positive control).
    argv = _await_record(record)
    assert PREV not in argv, (
        f"recovery force-saved {argv!r} despite PREV ({PREV}) having no "
        "roster marker under this store -- it may belong to a different "
        "store sharing this project directory"
    )


def test_positive_control_a_marked_candidate_is_still_rescued(tmp_path):
    """Positive control, paired with the must-fire case above: when PREV IS
    marked under this store's roster, recovery must still force-save it --
    proving the new guard only ever removes an unmarked candidate, never a
    legitimate one."""
    home, project, remember, _ = _startup_project(tmp_path)
    _vouch(remember, PREV)
    (remember / "tmp" / "last-save.json").write_text(json.dumps({"sessions": {}}))
    record = tmp_path / "save-argv.txt"
    root = _plugin_root_with_stub_save(tmp_path, record)

    result = _run(
        _env(home, project, remember, plugin_root=root),
        _payload(CURRENT),
        script=root / "scripts" / "session-start-hook.sh",
    )
    assert result.returncode == 0, subprocess_failure_detail(result, remember)

    argv = _await_record(record)
    assert PREV in argv, (
        f"expected recovery to rescue the roster-marked session {PREV!r}, "
        f"got {argv!r}"
    )
