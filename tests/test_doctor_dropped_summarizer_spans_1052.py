"""#1052: a reject gate ([validate] REJECTED or [haiku] REJECTED) is a
SUCCESSFUL summarizer call that drops a span of real work -- the model's
reply was a refusal or "not an entry header", so save-session.sh keeps the
rejected text under tmp/rejected-*.md, advances the position, and exits 0.
Nothing about that touches tmp/last-summary-failure (#870's own marker),
because the call itself succeeded; #870's counter was built to catch a
failed CALL, not a successful one that discarded its own output.

The reporter's own observation: three [validate] REJECTED spans over four
days, plus ten more while two plugin versions both ran, and doctor.sh said
"Summarizer failures: OK" and "capture is working" throughout.

This pins the new "Dropped summarizer spans" section: a must-fire /
must-not-fire pair, per this repo's own CLAUDE.md, reusing
test_doctor_summarizer_failure_870.py's own healthy baseline so the new
section's effect is isolated from everything else."""

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

from tests.test_doctor_session_end_370 import _run
from tests.test_doctor_summarizer_failure_870 import _healthy_baseline


def test_positive_control_healthy_baseline_reports_no_dropped_spans(tmp_path):
    """Must-not-fire's twin: with no REJECTED line anywhere in the logs
    present, the new section must say so -- otherwise the section firing
    for everyone would pass the must-fire test below for the wrong reason."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert (
        "OK   No rejections recorded" in result.stdout
    ), result.stdout


def test_a_validate_rejected_line_is_counted_and_warned(tmp_path):
    """Must-fire case, the issue's own repro shape: a [validate] REJECTED
    line in a daily log (a span dropped by the header-time refusal) must be
    counted and WARNed about, even though the call succeeded and #870's own
    marker was never written."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-10-16.log").write_text(
        "09:01:31 [validate] REJECTED (not an entry header): I don't have "
        "the concrete timestamp and location values that the script "
        "computed\n"
        "09:01:31 [validate] position -> 42\n",
        encoding="utf-8",
    )
    assert not (remember / "tmp" / "last-summary-failure").exists(), (
        "precondition: #870's own marker must stay absent -- the call "
        "succeeded, so that counter alone would report nothing"
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "OK   No summarizer failure recorded" in result.stdout, (
        "precondition broke: #870's counter should still read OK here -- "
        "this test is about the NEW section catching what that one cannot"
    )
    assert "WARN 1 rejection(s) hit a reject gate" in result.stdout, (
        "a [validate] REJECTED line in the daily log went uncounted:\n"
        + result.stdout
    )
    assert "REJECTED (not an entry header)" in result.stdout


def test_a_haiku_rejected_line_is_also_counted(tmp_path):
    """Sibling of the validate-gate case: the issue names [haiku] REJECTED
    (a refusal or clarifying question) as the same blind spot, three of the
    ten additional dropped spans it reports."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-10-12.log").write_text(
        "12:00:00 [haiku] REJECTED (provider: claude; not a summary -- "
        "refusal or clarification): I need more context\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "WARN 1 rejection(s) hit a reject gate" in result.stdout, result.stdout


def test_a_summarizer_rejected_line_is_also_counted(tmp_path):
    """Final-review finding: #1085 (merged into this branch) renamed the
    summarize-step's own log tag from the pre-existing [validate]/[haiku]
    pair to [summarizer] (save-session.sh:936, `log "summarizer" "REJECTED
    (provider: ...; not a summary -- refusal or clarification): ..."`) --
    on current code this tag matched neither of doctor.sh's two patterns,
    so a dropped span logged under the new tag reported "OK No dropped
    summarizer spans" while a span was in fact dropped, the exact silent
    regression this whole section exists to prevent."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-10-20.log").write_text(
        "13:10:00 [summarizer] REJECTED (provider: claude; not a summary -- "
        "refusal or clarification): I can't help with that\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "WARN 1 rejection(s) hit a reject gate" in result.stdout, (
        "a [summarizer] REJECTED line (#1085's renamed tag) went uncounted:\n"
        + result.stdout
    )


def test_an_ndc_rejected_line_is_not_counted_as_dropped(tmp_path):
    """Negative control for the fix above, pinned separately from the
    positive one: [ndc] REJECTED (save-session.sh's compression step,
    :1304/:1306) is deliberately excluded from this section's pattern.
    Unlike [validate]/[haiku]/[summarizer], an ndc rejection leaves now.md
    untouched (save-session.sh's own comment at :1229: "now.md is left
    intact so the next round retries") -- the span is retried next round,
    never dropped, so counting it here would be a false positive, not a
    fix. Paired with the must-fire test above per this repo's own
    CLAUDE.md: a check that always reports clean would pass this test for
    the wrong reason without the positive case next to it."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-10-21.log").write_text(
        "14:20:00 [ndc] REJECTED (provider: claude; not a summary -- "
        "refusal or clarification): I can't compress that\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "OK   No rejections recorded" in result.stdout, (
        "an [ndc] REJECTED line was counted as a rejection here, but ndc "
        "rejections are retried (now.md left intact), not dropped:\n"
        + result.stdout
    )


def test_counts_sum_across_multiple_logs_not_only_the_newest(tmp_path):
    """Same cross-file-sum shape #870's own section pins for "call-haiku
    error" lines: a dropped span from an OLDER daily log must still be
    counted, not just the newest-mtime file's own matches."""
    home, project, remember, _session_dir = _healthy_baseline(tmp_path)
    logs = remember / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "memory-2026-10-12.log").write_text(
        "12:00:00 [haiku] REJECTED (provider: claude; not a summary): a\n",
        encoding="utf-8",
    )
    (logs / "memory-2026-10-19.log").write_text(
        "12:45:20 [validate] REJECTED (not an entry header): b\n"
        "12:45:21 [validate] REJECTED (not an entry header): c\n",
        encoding="utf-8",
    )

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "WARN 3 rejection(s) hit a reject gate" in result.stdout, (
        "expected the count to sum across both daily logs (1 + 2 = 3):\n"
        + result.stdout
    )
