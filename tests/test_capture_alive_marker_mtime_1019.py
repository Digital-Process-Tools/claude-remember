"""#1019: the capture-alive marker's mtime stops refreshing when
SESSION_ID is empty (the basename-sanitiser route, scripts/post-tool-hook.sh
around line 852-871, introduced by #1016/78fc841).

When SESSION_ID is empty and the marker already holds "" (empty, from a
prior empty write), the CURRENT == SESSION_ID check was true, so the
in-place `printf '%s' "" 1<> marker` branch ran -- a zero-byte write(2),
which never updates mtime (POSIX). doctor.sh reads this marker's mtime as
"is PostToolUse still firing"; a frozen mtime for a hook that is actively
firing misreports a healthy session as stale. The fix forces the
empty-SESSION_ID case onto the atomic tmp+mv branch instead, which always
touches mtime regardless of content.

Positive control first (must-fire case): a NON-empty SESSION_ID must still
refresh the marker's mtime on every call, or the negative assertion below
(mtime DOES change for empty SESSION_ID too) would pass simply because
nothing about mtime refresh is being tested at all.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from tests.test_post_tool_incremental_scan_913 import _env, _project, _reap, _run

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook subprocess + POSIX semantics -- not portable to Windows runners",
)


def _marker_mtime(remember):
    return (remember / "tmp" / "capture-alive").stat().st_mtime


def test_a_nonempty_session_id_still_refreshes_the_marker_mtime(tmp_path):
    """POSITIVE CONTROL: the real case #1016 was optimizing (same session,
    repeat calls) must keep refreshing mtime, or the test below proves
    nothing -- it would also pass if mtime refresh were broken entirely."""
    home, project, remember, _ = _project(tmp_path, jsonl_lines=5,
                                           session_id="sess-real")
    env = _env(tmp_path, home, project)

    first = _run(env)
    assert first.returncode == 0, first.stderr[:400]
    marker = remember / "tmp" / "capture-alive"
    assert marker.exists(), "capture-alive marker was never created"
    assert marker.read_text(encoding="utf-8") == "sess-real"

    before = time.time() - 120
    os.utime(marker, (before, before))

    second = _run(env)
    assert second.returncode == 0, second.stderr[:400]
    _reap(remember)

    after = _marker_mtime(remember)
    assert after > before, (
        "a repeat call with the SAME non-empty session id never refreshed "
        f"the marker's mtime: before={before}, after={after}"
    )


def test_an_empty_session_id_still_refreshes_the_marker_mtime(tmp_path):
    """#1019 itself. An empty SESSION_ID (the sanitiser-rejected-this-
    basename route) still has to pass through this hook on every tool
    call in a live session -- the marker's mtime is the only signal
    doctor.sh has that it did. Seed the marker directly with the exact
    pre-condition the bug needs (content already "", mtime back-dated),
    then confirm a hook run with an empty SESSION_ID still moves it
    forward rather than leaving it frozen."""
    home, project, remember, transcript = _project(tmp_path, jsonl_lines=5,
                                                     session_id="sess-1")
    env = _env(tmp_path, home, project)

    # A transcript basename the sanitiser rejects -- SESSION_ID comes back
    # empty (scripts/post-tool-hook.sh's own basename allowlist check).
    # No spaces: an unquoted glob a few lines earlier in the hook splits a
    # space-containing filename on IFS, which would make the hook find NO
    # transcript at all rather than exercise the empty-SESSION_ID path this
    # test is about (a different, pre-existing fragility, not #1019).
    bad_transcript = transcript.parent / "bad!id.jsonl"
    transcript.rename(bad_transcript)

    marker = remember / "tmp" / "capture-alive"
    marker.write_text("", encoding="utf-8")
    before = time.time() - 120
    os.utime(marker, (before, before))

    result = _run(env)
    assert result.returncode == 0, result.stderr[:400]
    _reap(remember)

    assert marker.read_text(encoding="utf-8") == "", (
        "an empty-SESSION_ID run wrote non-empty content to the marker"
    )
    after = _marker_mtime(remember)
    assert after > before, (
        "an empty-SESSION_ID call left the marker's mtime frozen -- "
        f"before={before}, after={after}. doctor.sh reads this mtime as "
        "'is PostToolUse still firing'; a frozen mtime misreports an "
        "actively-firing hook as stale (#1019)."
    )
