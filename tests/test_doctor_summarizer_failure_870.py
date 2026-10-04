"""#870: /remember:doctor said "capture is working" throughout a 10-day
summarizer auth outage, because "Last successful save" is read from
tmp/last-save.json's mtime, and save-position rewrites that file on every
attempt -- a SKIP, a quarantine, or an ordinary append -- not only on a
real one. doctor.sh never consulted tmp/last-summary-failure (written by
save-session.sh's record_summary_failure, scripts/save-session.sh ~L758-784,
and removed only on success/SKIP/give-up), nor the daily log's own
"call-haiku error" lines, so an installation whose every attempt failed for
ten days read as healthy the whole time.

This pins the new "Summarizer failures" section and verdict-ladder arm: the
marker's mere presence must override "capture is working" and surface the
real cause (and, when it matches an auth marker, the REMEMBER_OAUTH_TOKEN
remedy). A must-fire / must-not-fire pair, per this repo's own CLAUDE.md:
the same healthy baseline that reaches "capture is working" without the
marker must still reach it once the marker is removed again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tests.test_doctor as _doctor_144
from tests.test_doctor_session_end_370 import _backdate, _project, _run, _verdict


def _healthy_baseline(tmp_path):
    """Same shape test_doctor_session_end_370's own #392-row-2 healthy-install
    fixture uses: prior CC history old enough to predate the store, PostToolUse
    fired, and a completed save -- the exact fixture that reaches "capture is
    working" today, so the marker's effect is isolated from everything else."""
    home, project, remember, session_dir = _project(tmp_path)
    for name in ("aaaa-old-a.jsonl", "bbbb-old-b.jsonl", "cccc-old-c.jsonl"):
        f = session_dir / name
        f.write_text("{}\n", encoding="utf-8")
        _backdate(f, 3 * 24 * 3600)
    (remember / "tmp" / "capture-alive").write_text("sess-1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")
    return home, project, remember, session_dir


def test_positive_control_healthy_baseline_still_says_capture_is_working(tmp_path):
    """Must-fire's twin: with no failure marker at all, the baseline fixture
    must still reach the plain success verdict -- otherwise the new arm
    firing for everyone would pass the must-fire test below for the wrong
    reason."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "OK   No summarizer failure recorded" in result.stdout
    assert _verdict(result.stdout).startswith("VERDICT: capture is working"), (
        "the healthy baseline, with no failure marker, stopped reaching "
        "the plain success verdict:\n" + result.stdout
    )


def test_summarizer_failure_marker_overrides_capture_is_working(tmp_path):
    """The reported shape: the marker is present (the last attempt failed and
    nothing has succeeded since), so the verdict must say so instead of
    "capture is working", even though PostToolUse fired and a save once
    completed."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    (remember / "tmp" / "last-summary-failure").write_text(
        "sess-2:9001 1", encoding="utf-8")
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-09-22.log").write_text(
        "05:52:37 [haiku] ERROR: call-haiku error: claude exited 1: "
        "Failed to authenticate: OAuth session expired and could not be "
        "refreshed\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL summarizer: last attempt failed" in result.stdout, (
        "the failure marker did not reach the Summarizer failures section:\n"
        + result.stdout
    )
    assert "Failed to authenticate" in result.stdout
    assert "setup-token" not in result.stdout, "#898 round 15: no credential command is named"
    assert "log in again with" in result.stdout, (
        "an auth-shaped failure detail must point at refreshing the CLI's "
        "own login (#860, round 3: the plugin reads no credential of its "
        "own any more, so there is no userConfig setting left to point "
        "at):\n" + result.stdout
    )
    verdict = _verdict(result.stdout)
    assert verdict.startswith(
        "VERDICT: problem -- the summarizer's last attempt failed"
    ), (
        "the summarizer failure marker did not override capture-is-working:\n"
        + result.stdout
    )


def test_non_auth_failure_detail_gets_no_oauth_remedy(tmp_path):
    """Negative control for the remedy line itself: a failure that is NOT
    auth-shaped (a rate limit, say) must still FAIL the verdict, but must NOT
    print the REMEMBER_OAUTH_TOKEN remedy -- otherwise every failure, auth or
    not, would print it, and "points at the right fix" would be
    indistinguishable from "always says the same thing"."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    (remember / "tmp" / "last-summary-failure").write_text(
        "sess-3:42 1", encoding="utf-8")
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-09-22.log").write_text(
        "05:52:37 [haiku] ERROR: call-haiku error: claude exited 1: "
        "API Error: 429 rate_limit_error\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL summarizer: last attempt failed" in result.stdout
    assert "rate_limit_error" in result.stdout
    assert "userConfig" not in result.stdout, (
        "a non-auth failure must not print the auth-specific remedy:\n"
        + result.stdout
    )


def test_detail_survives_a_newer_log_with_no_summarizer_lines_in_it(tmp_path):
    """Self-review regression (auditor): the scan used to pick only the
    single newest-mtime log file. If that file has no "call-haiku error"
    line in it yet (e.g. a new day's log created by an unrelated hook write,
    while the marker from yesterday's failure has not been cleared), the
    real detail sitting in yesterday's log was silently discarded -- FAIL
    summarizer printed with no detail even though one was available."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    (remember / "tmp" / "last-summary-failure").write_text(
        "sess-old:100 1", encoding="utf-8")
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-09-21.log").write_text(
        "16:52:37 [haiku] ERROR: call-haiku error: claude exited 1: "
        "Failed to authenticate: OAuth session expired and could not be "
        "refreshed\n",
        encoding="utf-8",
    )
    # The newer file exists (so it wins any newest-mtime comparison) but has
    # no summarizer line in it at all -- just an unrelated session-start entry.
    (logs / "memory-2026-09-22.log").write_text(
        "08:45:00 [session-start] session-start took 2s\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "Failed to authenticate" in result.stdout, (
        "the real detail, sitting in an older log, was discarded because a "
        f"newer log with no match won the file-level scan:\n{result.stdout}"
    )


def test_detail_is_found_when_the_project_path_contains_a_space(tmp_path):
    """Second-pass self-review regression: the multi-log scan fed a sorted,
    newline-joined list of paths through an UNQUOTED $(...) used directly
    as a `for ... in` list, which IFS-word-splits on spaces too -- a
    project path containing one (common on macOS: a folder named with a
    person's full name, an iCloud/Dropbox sync folder) tore every matched
    path into fragments, none of which passed `[ -f ]`, so the scan
    silently found nothing at all even though a real log with real detail
    existed on disk."""
    home = tmp_path / "home"
    project = tmp_path / "Project With Spaces"
    remember = project / ".remember"
    for name in ("aaaa-old-a.jsonl", "bbbb-old-b.jsonl", "cccc-old-c.jsonl"):
        session_dir = home / ".claude" / "projects" / _doctor_144._slug(str(project))
        session_dir.mkdir(parents=True, exist_ok=True)
        f = session_dir / name
        f.write_text("{}\n", encoding="utf-8")
        _backdate(f, 3 * 24 * 3600)
    (remember / "tmp").mkdir(parents=True)
    (remember / "tmp" / "capture-alive").write_text("sess-1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")
    (remember / "tmp" / "last-summary-failure").write_text(
        "sess-2:9001 1", encoding="utf-8")
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-09-22.log").write_text(
        "05:52:37 [haiku] ERROR: call-haiku error: claude exited 1: "
        "Failed to authenticate: OAuth session expired and could not be "
        "refreshed\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "Failed to authenticate" in result.stdout, (
        "a project path containing a space silently lost the summarizer "
        f"failure detail:\n{result.stdout}"
    )


def test_stale_failure_marker_does_not_mask_a_slug_mismatch(tmp_path):
    """Regression for the new arm's own ordering (#870 self-review): a stale
    tmp/last-summary-failure marker is independent of session-dir/slug state
    -- nothing clears it when the slug changes -- so it can coexist with a
    #144 slug mismatch. Before this fix, the new "summarizer failing" arm
    fired unconditionally and reached the VERDICT line first, so the
    operator never saw "#144" or "restarting will not help" at all --
    exactly the failure mode test_a_slug_mismatch_is_not_answered_with_restart_claude_code
    (tests/test_doctor.py) exists to prevent, just reached through a new,
    different arm instead of the old generic one."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    (remember / "tmp" / "post-tool-ran").write_text("")
    (remember / "tmp" / "last-summary-failure").write_text(
        "sess-old:100 1", encoding="utf-8")

    result = _doctor_144._run(home, project, remember)

    verdict = _doctor_144._verdict(result.stdout)
    assert "#144" in verdict, (
        "a stale summarizer-failure marker masked the #144 slug-mismatch "
        f"verdict:\n{result.stdout}"
    )
    assert "restart claude code" not in verdict.lower(), (
        f"told a slug-mismatch victim to restart Claude Code: {verdict}"
    )


def test_a_stale_last_save_time_does_not_let_the_new_arm_mask_a_slug_mismatch(tmp_path):
    """Second-pass self-review regression, scoped to #870's OWN arm only.

    The first #144 fix above only closed the _LAST_SAVE_TIME-empty half of
    the masking bug: a project that (a) had at least one successful save
    before a rename/move, so tmp/last-save.json still carries an old,
    non-empty mtime, (b) later developed a slug mismatch, and (c) still
    carries a stale tmp/last-summary-failure marker from before the
    mismatch, satisfied this arm's first three conditions even with the
    session-dir guard missing.

    This does NOT assert the overall verdict says "#144" -- that is gated
    by the pre-existing "capture is working" arm immediately below this
    one, which has the SAME missing-session-dir-guard defect and already
    masked #144 before #870 ever touched this file (confirmed by running
    origin/main's own scripts/doctor.sh against this identical fixture: it
    too prints "VERDICT: capture is working", never "#144"). That is a
    separate, pre-existing bug, out of #870's scope, and belongs in its own
    issue/fix rather than being silently absorbed here. What #870 owns is
    narrower and is what this test actually pins: the NEW arm this issue
    added must not ALSO fire and compound the existing problem."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    (remember / "tmp").mkdir(parents=True)
    (home / ".claude" / "projects").mkdir(parents=True)
    (remember / "tmp" / "post-tool-ran").write_text("")
    (remember / "tmp" / "last-summary-failure").write_text(
        "sess-old:100 1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        json.dumps({"session": "sess-old", "line": 100}), encoding="utf-8")

    result = _doctor_144._run(home, project, remember)

    verdict = _doctor_144._verdict(result.stdout)
    assert "the summarizer's last attempt failed" not in verdict, (
        "the #870 arm fired despite the session-dir guard, compounding the "
        f"pre-existing capture-is-working/#144 masking:\n{result.stdout}"
    )
