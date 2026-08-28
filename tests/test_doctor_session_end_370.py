"""doctor.sh has no SessionEnd liveness check (#370).

`PostToolUse`'s alive-marker check (scripts/doctor.sh, "5. Capture health")
reads a freshness window because that hook fires many times inside a single
live session -- a marker refreshed a few seconds ago means the hook is
running now. `SessionEnd` fires at most once per session, so that reading
does not transfer: "how old is the marker" is not the same question as "did
the hook run last time it had the chance". Filed rather than fixed inside
#368 (the SessionEnd hook itself), because a new check needs its own marker
convention and #345's acceptance criteria only covered README and docs.

No new marker file is introduced by this fix, and scripts/session-end-hook.sh
is not touched at all: that hook already leaves usable evidence of its own
accord, as a side effect of its background flush -- a
logs/autonomous/session-end-<HHMMSS>.log file, written unconditionally once
the hook gets past its SAVE_SCRIPT-missing check (see that hook's own
comments around its `_END_LOG` redirect). doctor.sh's job is reading that
signal, not producing a new one.

Three states, not two, and the fixtures below pin all three:

  * at least one session-end log exists -> fired, OK, and it must not appear
    as a problem (the must-fire case a silence assertion needs a positive
    control for);
  * no such log, but Claude Code's own transcript directory shows more than
    one session has ever existed for this project -- meaning a session other
    than the one running doctor.sh itself finished -- -> the hook had the
    chance to fire and did not, FAIL, and it has to reach the VERDICT line;
  * no such log, and nothing shows a prior session ever finished (0 or 1
    transcript, the "just installed" and "mid-first-session" shapes) -> the
    third state the issue calls out by name: this must render as neither of
    the other two. A check that answered FAIL here would flag every fresh
    install as broken before it had ever had the chance to prove itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash subprocess + POSIX semantics -- not portable to Windows runners",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCTOR = REPO_ROOT / "scripts" / "doctor.sh"

sys.path.insert(0, str(REPO_ROOT))

from pipeline.slug import session_dir_slug as _slug


def _project(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    remember = project / ".remember"
    session_dir = home / ".claude" / "projects" / _slug(str(project))
    session_dir.mkdir(parents=True)
    (remember / "tmp").mkdir(parents=True)
    return home, project, remember, session_dir


def _run(home: Path, project: Path, remember: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "CLAUDE_PROJECT_DIR": str(project),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "REMEMBER_DIR": str(remember),
        "_LIB_MEMORY_DIR_LOADED": "1",
    }
    return subprocess.run(
        ["bash", str(DOCTOR)], env=env,
        capture_output=True, text=True, timeout=180, check=False,
    )


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT:"):
            return line
    raise AssertionError("no VERDICT line in output:\n" + stdout)


def _backdate(path: Path, seconds: int) -> None:
    """Set a file's mtime `seconds` in the past -- doctor.sh's staleness
    check (>900s quiet) is how it tells "a prior session ended" apart from
    "another window on this project is open right now"."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def test_one_fresh_transcript_alone_is_still_the_third_state(tmp_path):
    """Exactly one transcript, un-backdated -- the shape doctor.sh itself
    produces when run mid-session, since a Bash tool call touches the
    current session's own transcript at (or just before) invocation. One
    file existing at all must not, by itself, be read as a prior session
    having ended -- that needs staleness, not mere existence.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "aaaa-this-session.jsonl").write_text("{}\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a single, freshly-touched transcript was read as a session having "
        "ended:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout)


def test_one_stale_transcript_alone_is_enough_to_fail(tmp_path):
    """The other side of that boundary: ONE transcript is sufficient
    evidence once it has gone quiet -- the count never mattered, only
    whether something demonstrably stopped being active.
    """
    home, project, remember, session_dir = _project(tmp_path)
    stale = session_dir / "aaaa-quiet-session.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    _backdate(stale, 3600)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "a single stale transcript was not enough to flag SessionEnd's "
        "silence:\n" + result.stdout
    )
    assert "SessionEnd" in _verdict(result.stdout)


def test_no_marker_and_no_prior_session_is_the_third_state_not_a_failure(tmp_path):
    """Fresh install / still inside the first session: must not read as broken.

    Zero transcripts in the session dir -- the shape a brand-new project or a
    doctor run from inside the very first session both produce. Nothing has
    had the chance to prove SessionEnd works or does not; a FAIL here would
    be the false alarm this test exists to rule out.
    """
    home, project, remember, _session_dir = _project(tmp_path)

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "a store with no prior session was flagged as a SessionEnd failure:\n"
        + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout), (
        "the third state (nothing has ended yet) reached the VERDICT line "
        "as though it were a finding either way:\n" + result.stdout
    )


def test_no_marker_but_prior_sessions_existed_fails_and_reaches_verdict(tmp_path):
    """The hook had its chance and stayed silent -- must fire, and must be FAIL.

    A transcript that has gone quiet for well over 15 minutes -- backdated,
    here, rather than genuinely two windows old -- means a session other
    than the one running doctor.sh right now started and stopped being the
    active one. No end-marker despite that is the exact silent failure #370
    reports: a SessionEnd hook that never fires reads as a healthy install.
    """
    home, project, remember, session_dir = _project(tmp_path)
    earlier = session_dir / "aaaa-earlier-session.jsonl"
    earlier.write_text("{}\n", encoding="utf-8")
    _backdate(earlier, 3600)
    (session_dir / "bbbb-another-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd has never fired" in result.stdout, (
        "doctor did not flag SessionEnd's silence despite a prior session "
        "having demonstrably ended:\n" + result.stdout
    )
    assert "SessionEnd" in _verdict(result.stdout), (
        "SessionEnd's silent failure did not reach the VERDICT line:\n"
        + result.stdout
    )


def test_two_concurrently_open_windows_do_not_false_positive(tmp_path):
    """Positive control for the FAIL case above, from the other direction:
    two LIVE, recently-touched transcripts must not be read as a session
    having ended.

    An earlier version of this fix counted transcripts rather than checking
    whether any had gone quiet, and treated "two or more *.jsonl files"
    alone as proof a session had ended -- which two ordinary, simultaneously
    open Claude Code windows on the same project also produce, with nothing
    broken and no session having ended at all. Without the staleness check,
    this fixture would false-positive exactly like the one above does
    correctly positive.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "aaaa-window-one.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "bbbb-window-two.jsonl").write_text("{}\n", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "FAIL SessionEnd" not in result.stdout, (
        "two concurrently open, recently-touched transcripts were read as "
        "a prior session having ended:\n" + result.stdout
    )
    assert "SessionEnd" not in _verdict(result.stdout), (
        "two live windows on the same project reached a SessionEnd problem "
        "verdict:\n" + result.stdout
    )


def test_marker_present_reports_ok_and_never_a_session_end_problem(tmp_path):
    """Positive control for the FAIL case above: the hook DID fire.

    Without this, a fix that flagged every store with two-or-more
    transcripts as broken -- never checking for the session-end log at all
    -- would still pass the FAIL test above. The fixture writes the exact
    file session-end-hook.sh's own background flush leaves behind
    (logs/autonomous/session-end-<HHMMSS>.log), not a purpose-built marker
    -- this fix reads that file, it does not introduce one.
    """
    home, project, remember, session_dir = _project(tmp_path)
    (session_dir / "aaaa-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "bbbb-another-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")
    (remember / "logs" / "autonomous").mkdir(parents=True)
    (remember / "logs" / "autonomous" / "session-end-093000.log").write_text(
        "", encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert "OK   SessionEnd has fired at least once" in result.stdout, (
        "a store with a genuine session-end log was not reported OK:\n"
        + result.stdout
    )
    assert "problem — SessionEnd" not in _verdict(result.stdout), (
        "a working SessionEnd hook still reached a SessionEnd problem verdict:\n"
        + result.stdout
    )


def test_session_end_failure_outranks_the_generic_capture_is_working_verdict(tmp_path):
    """Ladder placement: SessionEnd's own silent failure must not hide behind
    a healthy-looking PostToolUse verdict.

    doctor.sh's own VERDICT header states the ladder's rule: specific causes
    are named before the general one. PostToolUse capture can be entirely
    healthy while SessionEnd -- a distinct hook, a distinct failure mode --
    has never fired once. Reaching "capture is working" first would be
    exactly the invisibility #370 reports, just moved one line down.
    """
    home, project, remember, session_dir = _project(tmp_path)
    earlier = session_dir / "aaaa-earlier-session.jsonl"
    earlier.write_text("{}\n", encoding="utf-8")
    _backdate(earlier, 3600)
    (session_dir / "bbbb-another-earlier-session.jsonl").write_text("{}\n", encoding="utf-8")
    (remember / "tmp" / "capture-alive").write_text("sess-1", encoding="utf-8")
    (remember / "tmp" / "last-save.json").write_text(
        '{"session": "sess-1", "line": 500}', encoding="utf-8")

    result = _run(home, project, remember)

    assert result.returncode == 0, result.stderr
    assert _verdict(result.stdout).startswith("VERDICT: problem — SessionEnd"), (
        "PostToolUse capture being healthy masked SessionEnd's own silent "
        "failure instead of the specific cause outranking the general "
        "success line:\n" + result.stdout
    )
