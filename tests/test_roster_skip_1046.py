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
import subprocess
import sys
from pathlib import Path

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

REPO_ROOT = Path(__file__).resolve().parent.parent

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


def test_a_candidate_vouched_only_by_the_legacy_slot_is_still_rescued(tmp_path):
    """Self-review finding: the guard checked only the capture-alive.d
    marker, one of capture_was_seen()'s three evidence sources -- not the
    legacy single-slot file, kept (per that function's own comment) "so the
    first run after an upgrade -- old file present, new store empty -- is
    not itself a false positive." The roster-only guard reintroduced that
    exact false positive for the WRITE path: a session captured under an
    older plugin version, with only the legacy slot as evidence, would now
    be wrongly treated as possibly belonging to a different store and
    declined. Must still be rescued when ONLY the legacy slot vouches."""
    home, project, remember, _ = _startup_project(tmp_path)
    (remember / "tmp" / "capture-alive").write_text(PREV)
    assert not (remember / "tmp" / "capture-alive.d" / PREV).exists(), (
        "precondition: no .d marker -- only the legacy slot vouches"
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

    argv = _await_record(record)
    assert PREV in argv, (
        f"expected recovery to rescue the legacy-slot-vouched session "
        f"{PREV!r}, got {argv!r}"
    )


# Unit-tested directly against the shipped function, not through the real
# hook: a transcript literally named `..jsonl` (or `.jsonl`) is what WOULD
# strip to the dangerous id "." or ".." -- but bash's glob in
# `_find_previous_transcript` (`"$dir"/*"$_TRANSCRIPT_SUFFIX"`) never
# matches a leading-dot filename without `dotglob` set (confirmed: adding
# such a file to a real SESSIONS_DIR fixture and running the hook end to
# end, it is simply invisible, never reaching the recovery block at all --
# the originally-written version of this test passed with or without the
# sanitization fix below, for that reason, and was rewritten after that
# was discovered). The glob's own default behaviour is therefore a second,
# independent line of defense against this specific route today -- but
# `_remember_recovery_prev_id_vouched` is still worth testing directly,
# the same way this file already pulls functions verbatim out of a real
# hook (test_previous_transcript_no_sort_691.py's own convention): a future
# caller is not guaranteed to reach it only through this one glob (a
# `dotglob`-enabled environment, a future call site that hands it an id
# from somewhere else), and capture_was_seen() -- the function this one
# deliberately mirrors -- already treats the sanitization as load-bearing
# on its own, not merely incidental to how its one caller happens to glob.
_VOUCH_DRIVER = """
%s
REMEMBER_DIR=$1
_remember_recovery_prev_id_vouched "$2"
printf '%%s' "$?"
"""


def _extract_vouched_function() -> str:
    source = (REPO_ROOT / "scripts" / "capture-gap-check.sh").read_text(encoding="utf-8")
    marker = "\n_remember_recovery_prev_id_vouched() {\n"
    start = source.find(marker)
    assert start != -1, "_remember_recovery_prev_id_vouched not found in capture-gap-check.sh"
    end = source.index("\n}\n", start) + len("\n}")
    return source[start + 1 : end]


def test_a_malformed_id_is_never_vouched_for_even_with_a_marker_present(tmp_path):
    """Self-review finding: PREV_ID is a basename off a glob in SESSIONS_DIR
    with no character-class check of its own
    (_find_previous_transcript's `id=${f#"$dir"/}; id=${id%.jsonl}`). An
    unsanitized `-e ".../capture-alive.d/.."` or `.` would be true whenever
    capture-alive.d itself exists as a directory -- which it will in
    exactly the cross-store scenario #1046 is about. `.` and `..` must both
    be refused outright, the same way capture_was_seen() already refuses
    them for its own roster check."""
    remember = tmp_path / "remember"
    (remember / "tmp" / "capture-alive.d").mkdir(parents=True)
    # The condition that would make an unsanitized `-e .../.` or `-e
    # .../..` true: capture-alive.d exists, non-empty.
    (remember / "tmp" / "capture-alive.d" / "some-other-session").write_text("")

    body = _extract_vouched_function()
    script = _VOUCH_DRIVER % body

    for malformed_id in (".", ".."):
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(remember), malformed_id],
            capture_output=True, text=True, timeout=10, check=False,
        )
        assert result.stdout.strip() == "1", (
            f"_remember_recovery_prev_id_vouched({malformed_id!r}) returned "
            f"exit {result.stdout.strip()!r} (0 = vouched) -- a malformed id "
            f"must never vouch for itself: {result.stderr}"
        )


def test_a_well_formed_id_with_a_real_marker_is_vouched_for(tmp_path):
    """Positive control, paired with the must-fire case above: an ordinary
    well-formed session id with a real marker present must still return
    vouched (exit 0) -- proving the sanitization only ever rejects the
    pathological inputs, never a legitimate one."""
    remember = tmp_path / "remember"
    (remember / "tmp" / "capture-alive.d").mkdir(parents=True)
    (remember / "tmp" / "capture-alive.d" / PREV).write_text("")

    body = _extract_vouched_function()
    script = _VOUCH_DRIVER % body

    result = subprocess.run(
        ["bash", "-c", script, "bash", str(remember), PREV],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.stdout.strip() == "0", (
        f"_remember_recovery_prev_id_vouched({PREV!r}) returned exit "
        f"{result.stdout.strip()!r} (0 = vouched) with a real marker "
        f"present: {result.stderr}"
    )
