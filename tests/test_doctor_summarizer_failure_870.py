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

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

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
    assert "REMEMBER_OAUTH_TOKEN" in result.stdout, (
        "an auth-shaped failure detail must point at the documented remedy:\n"
        + result.stdout
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
    assert "REMEMBER_OAUTH_TOKEN" not in result.stdout, (
        "a non-auth failure must not print the auth-specific remedy:\n"
        + result.stdout
    )
